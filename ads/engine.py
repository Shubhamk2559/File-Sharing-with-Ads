"""Config cache, eligibility, plan (hot path) and the periodic optimizer (background)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from . import bandit, scoring, settings, slog, store
from .scoring import NetStats

log = logging.getLogger("ads.engine")
CACHE_TTL = 15.0
_cache: dict = {"cfg": (0.0, None), "plan": (0.0, None)}


@dataclass
class Plan:
    order: list = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    reason: str = ""
    mode: str = "auto"


def invalidate() -> None:
    _cache["cfg"] = (0.0, None)
    _cache["plan"] = (0.0, None)


async def get_config(force: bool = False) -> dict:
    t, cfg = _cache["cfg"]
    if not force and cfg is not None and time.time() - t < CACHE_TTL:
        return cfg
    try:
        cfg = settings.merged(await store.get_config_raw())
    except Exception:  # noqa: BLE001 — DB hiccup: keep serving the last good config
        log.exception("config read failed")
        cfg = cfg or settings.merged(None)
    _cache["cfg"] = (time.time(), cfg)
    return cfg


async def update_config(patch: dict) -> tuple[dict, list]:
    cur = await get_config(force=True)
    new, errors = settings.validate_patch(patch, cur)
    if errors:
        return cur, errors
    await store.save_config(new)
    invalidate()
    slog.event("config_updated", keys=sorted(patch))
    return new, []


async def _plan_state() -> dict:
    t, st = _cache["plan"]
    if st is not None and time.time() - t < CACHE_TTL:
        return st
    try:
        st = await store.get_doc("plan")
    except Exception:  # noqa: BLE001
        log.exception("plan read failed")
        st = st or {}
    _cache["plan"] = (time.time(), st)
    return st


def eligible(cfg: dict, benched: dict | None = None, now: float | None = None) -> list:
    """enabled + configured + reward trust allowed + not cooling down."""
    now = now or time.time()
    out, benched = [], benched or {}
    for n in settings.NETWORKS:
        c = cfg["networks"][n]
        if c["enabled"] and settings.is_configured(cfg, n) and settings.trust_allowed(cfg, n):
            out.append((n, benched.get(n, 0) > now))
    live = [n for n, b in out if not b]
    return live or [n for n, _ in out]            # everything benched -> still serve (least bad) instead of nothing


BLOCKER_TEXT = {
    "disabled": "switched off",
    "not_configured": "no block/widget ID set",
    "client_attested_not_allowed": "reward can't be server-verified and client-attested rewards are not allowed",
    "benched": "cooling down after failures",
}


def blockers(cfg: dict, benched: dict | None = None, now: float | None = None) -> dict:
    """{network: [reason codes]} for every network that will NOT receive traffic (empty list = eligible).
    This is the first thing to look at when rotation 'does nothing': by default only Monetag is eligible."""
    now = now or time.time()
    benched = benched or {}
    out = {}
    for n in settings.NETWORKS:
        c, r = cfg["networks"][n], []
        if not c["enabled"]:
            r.append("disabled")
        if not settings.is_configured(cfg, n):
            r.append("not_configured")
        if not settings.trust_allowed(cfg, n):
            r.append("client_attested_not_allowed")
        if benched.get(n, 0) > now:
            r.append("benched")
        out[n] = r
    return out


async def plan_request(rng=random) -> Plan:
    cfg = await get_config()
    st = await _plan_state()
    elig = eligible(cfg, st.get("benched"))
    prio = cfg["priority"]
    limit = cfg["max_attempts"] if cfg["fallback_enabled"] else 1
    if not elig:
        return Plan([], {}, "no eligible network", cfg["mode"])
    mode = cfg["mode"]
    if mode == "manual" and cfg["manual_network"] in elig:
        first = cfg["manual_network"]
        rest = [n for n in prio if n in elig and n != first]
        return Plan(([first] + rest)[:limit], {first: 1.0}, f"manual: {first}", mode)
    if mode == "priority" or mode == "manual":      # manual target not eligible -> degrade to priority order
        order = [n for n in prio if n in elig]
        return Plan(order[:limit], {order[0]: 1.0}, "static priority order", mode)
    opt = cfg["optimization"]
    if cfg.get("strategy", "thompson") == "thompson" and st.get("model"):
        prob = {n for n, u in (st.get("probation") or {}).items() if u > time.time()}
        order, reason = bandit.pick_order(elig, st["model"], prio, opt["min_share"], prob, opt["probation_share"], rng)
        shares = st.get("shares") or {}
        return Plan(order[:limit], {n: shares.get(n, 0.0) for n in elig}, reason, mode)
    order, weights, reason = scoring.choose_order(elig, st.get("scores") or {}, prio, opt["explore_share"], rng)
    return Plan(order[:limit], weights, reason, mode)


# ------------------------------------------------------------------ optimizer
def _fold(rows: list, nets=settings.NETWORKS) -> dict:
    out = {n: NetStats(n) for n in nets}
    for r in rows:
        s = out.get(r.get("network"))
        if not s:
            continue
        s.requests += int(r.get("requests", 0)); s.shown += int(r.get("shown", 0)); s.completed += int(r.get("completed", 0))
        s.verified += int(r.get("verified", 0)); s.denied += int(r.get("denied", 0))
        s.failed_nofill += int(r.get("failed_nofill", 0)); s.failed_error += int(r.get("failed_error", 0))
        s.abandoned += int(r.get("abandoned", 0))
    return out


async def collect_stats(cfg: dict, now: float | None = None):
    """(window stats with confirmed-revenue join, recent stats, per-network (attempts, hard_fails) for the cooldown window)."""
    now = now or time.time()
    opt = cfg["optimization"]
    days = store.window_days(opt["window_days"])
    drows = await store.daily_rows(days)
    window = _fold(drows)
    by_key: dict = {}
    for dr in drows:
        by_key.setdefault((dr.get("network"), dr.get("day")), []).append(dr)
    for r in await store.revenue_rows(days):
        n, d = r.get("network"), r.get("day")
        if n not in window:
            continue
        for dr in by_key.get((n, d), ()):
            window[n].conf_requests += int(dr.get("requests", 0))
            window[n].conf_verified += int(dr.get("verified", 0))
        window[n].conf_revenue += float(r.get("revenue_usd", 0.0))
    recent = _fold([r for r in await store.bucket_rows(now - opt["recent_hours"] * 3600)])
    cool = {n: [0, 0] for n in settings.NETWORKS}
    for r in await store.bucket_rows(now - opt["cooldown_minutes"] * 60):
        if r.get("network") in cool:
            cool[r["network"]][0] += int(r.get("requests", 0)); cool[r["network"]][1] += int(r.get("hard_fails", 0))
    return window, recent, {n: tuple(v) for n, v in cool.items()}


async def build_model(cfg: dict, window: dict) -> dict:
    """Thompson model from time-decayed evidence + the best real revenue source per network."""
    opt = cfg["optimization"]
    days = store.window_days(opt["window_days"])
    evidence = bandit.decay_fold(await store.daily_rows(days), {d: i for i, d in enumerate(days)}, opt["half_life_days"], settings.NETWORKS)
    est = await store.monetag_estimates(opt["window_days"]) if opt.get("use_monetag_estimate") else {}
    inputs = {n: {"conf_revenue": window[n].conf_revenue, "conf_verified": window[n].conf_verified,
                  "owner_ecpm": cfg["networks"][n].get("ecpm_usd")} for n in settings.NETWORKS}
    if est:
        inputs["monetag"]["est_revenue"] = sum(v["revenue"] for v in est.values())
        inputs["monetag"]["est_impressions"] = sum(v["impressions"] for v in est.values())
    values = bandit.estimate_values(inputs, opt["min_revenue_impressions"], bool(opt.get("use_monetag_estimate")))
    return bandit.build_model(evidence, values, opt["prior_strength"])


async def tick(now: float | None = None) -> bool:
    if not await store.try_lease("optimizer", settings.ADS_OPT_INTERVAL * 0.8):
        return False
    now = now or time.time()
    cfg = await get_config(force=True)
    opt = cfg["optimization"]
    window, recent, cool = await collect_stats(cfg, now)
    scores = scoring.compute_scores(window, recent, scoring.Params(opt["min_requests"], opt["min_revenue_impressions"],
                                                                    opt["explore_share"], opt["weights"]))
    prev = await store.get_doc("plan")
    prev_benched = prev.get("benched") or {}
    benched = {n: u for n, u in prev_benched.items() if u > now}
    levels = dict(prev.get("bench_level") or {})
    probation = {n: u for n, u in (prev.get("probation") or {}).items() if u > now}
    # circuit breaker with exponential back-off: every repeat offence doubles the cool-down (x2, x4, x8 max)
    for n in scoring.find_bench(cool, opt["fail_min_attempts"], opt["fail_threshold"]):
        if n not in benched:
            lvl = int(levels.get(n, 0))
            minutes = opt["cooldown_minutes"] * min(2 ** lvl, 8)
            benched[n] = now + minutes * 60
            levels[n] = lvl + 1
            probation.pop(n, None)
            slog.event("network_benched", level="warning", network=n, minutes=minutes, strikes=lvl + 1)
    # half-open: a network whose cool-down just ended gets a capped share until it proves itself
    for n, until in prev_benched.items():
        if until <= now and n not in benched:
            probation[n] = now + max(600, opt["cooldown_minutes"] * 60)
            slog.event("network_probation", network=n)
    # a clean, busy window forgives earlier strikes
    for n, (a, f) in cool.items():
        if n not in benched and levels.get(n) and a >= opt["fail_min_attempts"] and f / a < opt["fail_threshold"] / 2:
            levels[n] = 0
    model = await build_model(cfg, window)
    live = eligible(cfg, benched, now)
    shares = bandit.expected_shares(live, model, cfg["priority"], opt["min_share"], set(probation), opt["probation_share"])
    ranked = sorted((n for n in scores if scores[n]["measured"]), key=lambda n: -scores[n]["score"])
    leader = ranked[0] if ranked else None
    if cfg.get("strategy", "thompson") == "thompson" and shares:
        leader = max(shares, key=lambda n: (shares[n], -cfg["priority"].index(n)))
    await store.set_doc("plan", {"scores": scores, "benched": benched, "leader": leader, "updated_at": now, "model": model,
                                 "shares": shares, "probation": probation, "bench_level": levels,
                                 "ev_per_1000": bandit.expected_value_per_1000(shares, model)})
    if leader != prev.get("leader"):
        await store.add_history({"from": prev.get("leader"), "to": leader, "scores": {n: scores[n]["score"] for n in scores}})
    expired = await store.expire_stale(settings.ADS_SHOWN_LOCK_SECONDS + 60)
    if expired:
        slog.event("expired_attempts", count=expired)
    _cache["plan"] = (0.0, None)
    return True


async def loop() -> None:
    if not settings.SMART_ADS_ENABLED:
        return
    await asyncio.sleep(25)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("ads optimizer tick failed")
        await asyncio.sleep(settings.ADS_OPT_INTERVAL)
