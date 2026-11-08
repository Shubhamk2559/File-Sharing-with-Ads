"""Smart-ad request flow (framework independent). HTTP wrappers live in ads/routes.py.

  start()   pick a network, take the session's single ad slot, create the attempt
  report()  client events: shown | skip | fail  (fail before display => fallback to the next network)
  done()    completion claim -> server-side verification -> exactly-once reward

Only ONE attempt per session can be live (atomic slot). A fallback is offered ONLY if the previous attempt never
displayed an ad, so a user is never shown two rewarded ads for one request.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

import observability
from . import engine, security, settings, slog, store, verify
from .verify import Facts, Policy

FAIL_REASONS = {"no_fill", "timeout", "blocked", "error"}
_MT_REASON = {"no_fill": "empty_feed", "timeout": "timeout", "blocked": "blocked", "error": "error"}
_MSG = {
    "non_valued": "That ad didn't count. Please try again.",
    "unverified": "We couldn't confirm the ad view. Please try again.",
    "client_attested_not_allowed": "This ad provider can't be verified right now. Please try again.",
    "expired": "This ad session expired. Please try again.",
}
_SESSION_GONE = {"ok": False, "code": "session", "error": "Session expired. Please open the link again."}


def _session_expired(s: dict) -> bool:
    return security.session_expired(s, settings.SESSION_TTL, D.clock())


@dataclass
class Deps:
    sessions: Any                                   # object with async start_ad(sid, ymid, zone) / complete_ad(sid, min_seconds)
    policy: Policy
    monetag: Any = None                             # monetag_store module (optional)
    select_zone: Callable | None = None             # async () -> zone
    sdk_url: Callable | None = None                 # zone -> url
    clock: Callable = time.time
    rng: Any = random
    long_poll: float = 0.0                          # seconds done() may wait for the Monetag postback before answering {wait}


D: Deps | None = None


def configure(deps: Deps) -> None:
    global D
    D = deps


def _public(net: str, cfg: dict) -> dict:
    if net == "tads":
        return {"unit_id": settings.unit_id(cfg, net), "sdk": settings.TADS_SDK_URL, "debug": settings.TADS_DEBUG}
    return {}


async def _launch(s: dict, user_id: int, chain: list, idx: int, country, prev_ad: str | None, rnd: int = 1) -> dict:
    cfg = await engine.get_config()
    net, sid = chain[idx], s["sid"]
    ad_id = f"{sid}.{secrets.token_hex(4)}"
    ttl = settings.ADS_UNSHOWN_LOCK_SECONDS
    if prev_ad is None:
        ok, old = await store.acquire_slot(sid, ad_id, ttl)
        if not ok:
            return {"ok": False, "code": "busy", "error": "An ad is already loading. Please wait a moment and try again."}
        if old and old != ad_id:                    # takeover of a stale, never-displayed attempt
            await store.finalize(old, "abandoned", "superseded")
    elif not await store.swap_slot(sid, prev_ad, ad_id, ttl):
        return {"ok": False, "code": "busy", "error": "Ad session changed. Please try again."}

    zone = None
    if net == "monetag":
        zone = await D.select_zone() if D.select_zone else None
    t = D.clock()
    if not await store.create_attempt({"_id": ad_id, "ad_id": ad_id, "sid": sid, "user_id": user_id, "network": net,
                                       "attempt_no": idx + 1, "zone": zone, "country": country, "t": t,
                                       "round": rnd, "chain_len": len(chain)}):
        await store.release_slot(sid, ad_id)
        return {"ok": False, "error": "Could not start the ad. Please try again."}
    await D.sessions.start_ad(sid, ad_id, zone or net)
    if net == "monetag" and D.monetag:
        try:
            await D.monetag.record_attempt(ad_id, sid, zone, country, user_id=user_id)
        except Exception:  # noqa: BLE001 — analytics must never block delivery
            slog.event("monetag_record_attempt_failed", level="warning", ad_id=ad_id)
    await store.log_event(ad_id, "start", network=net, attempt=idx + 1)
    slog.event("ad_start", ad_id=ad_id, network=net, attempt=idx + 1, user_id=user_id)
    out = {"ok": True, "sid": sid, "ad_id": ad_id, "ymid": ad_id, "network": net, "attempt": idx + 1,
           "ads_done": s.get("ads_done", 0), "ads_required": s.get("ads_required", 1)}
    if net == "monetag":
        out.update(zone=zone, sdk=D.sdk_url(zone) if D.sdk_url and zone else None)
    else:
        out.update(_public(net, cfg))
    return out


async def start(s: dict, user_id: int, country=None) -> dict:
    if _session_expired(s):
        return dict(_SESSION_GONE)
    plan = await engine.plan_request(D.rng)
    if not plan.order:
        return {"ok": False, "code": "no_network", "error": "No ad provider is available right now. Please try again shortly."}
    await store.set_chain(s["sid"], plan.order)
    rnd = await store.next_round(s["sid"])
    slog.event("ad_plan", sid=s["sid"], order=plan.order, mode=plan.mode, reason=plan.reason, round=rnd)
    return await _launch(s, user_id, plan.order, 0, country, None, rnd)


async def _owned(s: dict, ad_id) -> dict | None:
    if not isinstance(ad_id, str):
        return None
    ad = await store.get_attempt(ad_id)
    return ad if ad and ad["sid"] == s["sid"] and ad["user_id"] == s["user_id"] else None


async def report(s: dict, ad_id, event: str, reason=None, country=None) -> dict:
    if _session_expired(s):
        return dict(_SESSION_GONE)
    ad = await _owned(s, ad_id)
    if not ad:
        return {"ok": False, "error": "Unknown ad session."}
    sid = s["sid"]
    if event == "shown":
        if await store.mark_shown(ad_id):
            await store.extend_slot(sid, ad_id, settings.ADS_SHOWN_LOCK_SECONDS)
            await store.log_event(ad_id, "shown")
        return {"ok": True}
    if ad["outcome"] is not None:
        return {"ok": True}                         # idempotent
    if event == "skip":
        await store.finalize(ad_id, "abandoned", "skipped")
        await store.log_event(ad_id, "skip")
        return {"ok": True}
    if event != "fail":
        return {"ok": False, "error": "Bad event."}
    reason = reason if reason in FAIL_REASONS else "error"
    displayed = ad.get("shown_at") is not None
    if await store.finalize(ad_id, "failed", reason, release=False) is None:   # slot is held for a possible hand-over
        return {"ok": True}
    await store.log_event(ad_id, "fail", reason=reason, displayed=displayed)
    slog.event("ad_fail", level="warning", ad_id=ad_id, network=ad["network"], reason=reason, displayed=displayed)
    if ad["network"] == "monetag" and D.monetag:
        try:
            await D.monetag.record_fail(ad_id, sid, _MT_REASON[reason])
        except Exception:  # noqa: BLE001
            pass
    if displayed:
        await store.release_slot(sid, ad_id)
        return {"ok": True, "exhausted": True, "displayed": True}   # an ad was shown: never chain a second one
    cfg = await engine.get_config()
    limit = cfg["max_attempts"] if cfg["fallback_enabled"] else 1
    chain, _ = await store.get_session_chain(sid)
    idx = int(ad["attempt_no"]) - 1
    if idx + 1 < limit and chain and await store.advance_chain(sid, idx):
        st = await engine._plan_state()
        live = set(engine.eligible(cfg, st.get("benched")))
        for j in range(idx + 1, min(len(chain), limit)):
            if chain[j] in live:
                nxt = await _launch(s, ad["user_id"], chain, j, country, ad_id, int(ad.get("round") or 1))
                if nxt.get("ok"):
                    return {"ok": True, "next": nxt}
                break
    await store.flag_exhausted(ad_id)               # this round is over (see ads/gate.py)
    await store.release_slot(sid, ad_id)
    return {"ok": True, "exhausted": True}


_UNVERIFIED_ALERT_AFTER = 3


async def _note_unverified() -> None:
    """The user watched the ad but Monetag's postback never reached us. If NO postback at all has arrived since this instance
    started, the Monetag zone's Postback URL is missing/wrong: say so loudly (log + one owner alert) instead of failing silently."""
    obs = observability
    m = obs.METRICS
    m.inc("ads.unverified")
    if m.get("postback.accepted") or m.get("postback.duplicate"):
        return                                           # valid postbacks do arrive; this one was just late/missing
    # Only ACCEPTED postbacks count: a manual browser test or a rejected one (wrong secret/user/zone) must not hide the problem.
    slog.event("postback_never_received", level="warning", unverified=m.get("ads.unverified"),
               rejected=m.get("postback.rejected"),
               hint="No valid Monetag postback accepted since startup: check the Postback URL is SAVED in Monetag (KOYEB.md Step 1) and look for postback_rejected in the logs")
    if m.get("ads.unverified") == _UNVERIFIED_ALERT_AFTER:
        try:
            await obs.ALERTS.alert("postback_never_received",
                                   "Users watch Monetag ads but NO postback has reached the server since startup, so nobody gets files. "
                                   "Monetag -> your zone -> Postback URL is missing or wrong. Use the URL from KOYEB.md Step 1 "
                                   "(BASE_URL must be this app's public https URL, secret = MONETAG_POSTBACK_SECRET).", severity="warning")
        except Exception:  # noqa: BLE001
            pass


async def done(s: dict, ad_id, completed: bool, non_valued: bool = False) -> dict:
    """Never trusts the client for revenue/validity. Returns {ok, wait} | {ok False,error} | {ok, granted, s2, trust}."""
    if _session_expired(s):
        return dict(_SESSION_GONE)
    ad = await _owned(s, ad_id)
    if not ad:
        return {"ok": False, "error": "This ad session is outdated. Please try again."}
    if ad["outcome"] is not None:
        return {"ok": False, "error": "This ad session is outdated. Please try again."}
    sid, net, pol = s["sid"], ad["network"], D.policy
    if D.clock() - float(ad.get("t") or 0) > settings.ATTEMPT_MAX_AGE:       # a stale attempt can't be redeemed (replay of an old ad_id)
        await store.finalize(ad_id, "denied", "expired")
        slog.event("reward_denied", level="warning", ad_id=ad_id, network=net, reason="expired")
        return {"ok": False, "error": _MSG["expired"]}
    if not completed:
        await store.finalize(ad_id, "abandoned", "not_completed")
        return {"ok": False, "error": "The ad wasn't completed. Please try again."}
    ad = await store.record_claim(ad_id, non_valued) or ad
    started = s.get("ad_started_at")
    if not started:
        return {"ok": False, "error": "The ad didn't start. Please try again."}
    async def _decide():
        now = D.clock()
        facts = Facts(network=net, elapsed=now - started, client_completed=bool(ad.get("client_completed")),
                      client_non_valued=bool(ad.get("client_non_valued")),
                      claim_age=(now - ad["claimed_at"]) if ad.get("claimed_at") else None)
        if net == "monetag" and pol.monetag_postback and D.monetag:
            m = await D.monetag.get_ad(ad_id)
            # the postback record must belong to THIS session, THIS user and the zone we issued the ad for
            if not m or m.get("sid") != sid or m.get("user_id") != ad["user_id"] or str(m.get("zone")) != str(ad.get("zone")):
                return None
            facts.postback_seen, facts.postback_valued = bool(m.get("pb_seen")), bool(m.get("pb_valued"))
        cfg = await engine.get_config()
        return verify.decide(facts, pol, bool(cfg["networks"][net]["allow_client_reward"]))

    dec = await _decide()
    if dec is None:
        await store.finalize(ad_id, "denied", "unverified")
        return {"ok": False, "error": _MSG["unverified"]}
    # Fast path: hold the request open and re-check every 0.4 s, so the file is released right when the postback lands.
    if dec.action == "wait" and dec.reason == "waiting_for_server_signal" and D.long_poll > 0:
        deadline = time.monotonic() + D.long_poll
        while dec.action == "wait" and dec.reason == "waiting_for_server_signal" and time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            nd = await _decide()
            if nd is None:
                await store.finalize(ad_id, "denied", "unverified")
                return {"ok": False, "error": _MSG["unverified"]}
            dec = nd
    if dec.action == "wait":
        return {"ok": True, "wait": dec.wait}
    if dec.action == "deny":
        await store.finalize(ad_id, "denied", dec.reason)
        await store.log_event(ad_id, "deny", reason=dec.reason)
        slog.event("reward_denied", level="warning", ad_id=ad_id, network=net, reason=dec.reason)
        if net == "monetag" and D.monetag and dec.reason in ("unverified", "non_valued"):
            try:
                await D.monetag.record_fail(ad_id, sid, dec.reason)
            except Exception:  # noqa: BLE001
                pass
        if net == "monetag" and dec.reason == "unverified":
            await _note_unverified()
        return {"ok": False, "error": _MSG.get(dec.reason, "Verification failed. Please try again.")}

    # ---- grant: exactly once (finalize + reward doc are both keyed by ad_id) ----
    if await store.finalize(ad_id, "verified", dec.reason, dec.trust) is None:
        return {"ok": False, "error": "Duplicate request."}
    await store.insert_reward({"ad_id": ad_id, "sid": sid, "user_id": ad["user_id"], "network": net, "trust": dec.trust,
                               "file_token": s.get("token"), "bot_id": s.get("bot_id")})
    s2 = await D.sessions.complete_ad(sid, pol.min_seconds)
    if not s2:
        slog.event("reward_state_mismatch", level="error", ad_id=ad_id, sid=sid)
        return {"ok": False, "error": "Duplicate request."}
    if net == "monetag" and D.monetag:
        try:
            await D.monetag.record_completion(ad_id)
        except Exception:  # noqa: BLE001
            pass
    await store.log_event(ad_id, "grant", trust=dec.trust)
    slog.event("reward_granted", ad_id=ad_id, network=net, trust=dec.trust, user_id=ad["user_id"])
    return {"ok": True, "granted": True, "s2": s2, "trust": dec.trust}
