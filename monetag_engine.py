"""Zone selection (hot path, cached) + periodic optimizer (background)."""
from __future__ import annotations

import asyncio
import logging
import time

import monetag_store as store
from config import (
    MONETAG_BENCH_FAIL_RATE,
    MONETAG_BENCH_MIN_ATTEMPTS,
    MONETAG_BENCH_MINUTES,
    MONETAG_DEFAULT_ZONE,
    MONETAG_EXPLORE_SHARE,
    MONETAG_MIN_IMPRESSIONS,
    MONETAG_MIN_SHARE,
    MONETAG_MIN_SWITCH_HOURS,
    MONETAG_OPT_INTERVAL,
    MONETAG_OPTIMIZE,
    MONETAG_WINDOW_DAYS,
    MONETAG_Z_SCORE,
    MONETAG_ZONES,
)
from monetag_opt import Params, compute_plan, find_bench, pick_zone

log = logging.getLogger("monetag.engine")

PARAMS = Params(
    min_imp=MONETAG_MIN_IMPRESSIONS,
    explore=MONETAG_EXPLORE_SHARE,
    min_share=MONETAG_MIN_SHARE,
    min_switch_s=MONETAG_MIN_SWITCH_HOURS * 3600,
    z=MONETAG_Z_SCORE,
)
ZONES = list(MONETAG_ZONES)
CACHE_TTL = 30.0
_cache = {"t": 0.0, "weights": None, "benched": set()}


def sdk_url(zone: str) -> str:
    return MONETAG_ZONES.get(zone) or MONETAG_ZONES[MONETAG_DEFAULT_ZONE]


def _initial_weights() -> dict:
    return compute_plan({}, ZONES, MONETAG_DEFAULT_ZONE, MONETAG_DEFAULT_ZONE, 0.0, time.time(), PARAMS).weights


async def _refresh_cache():
    try:
        st = await store.get_state()
    except Exception:  # noqa: BLE001 — DB hiccup: purana cache chalne do
        log.exception("state read failed")
        _cache["t"] = time.time()
        return
    weights = {z: w for z, w in ((st or {}).get("weights") or {}).items() if z in MONETAG_ZONES}
    now = time.time()
    _cache.update(
        t=now,
        weights=weights or _initial_weights(),
        benched={z for z, u in ((st or {}).get("benched") or {}).items() if u > now and z in MONETAG_ZONES},
    )


async def select_zone() -> str:
    """Hot path: 30s me ek hi DB read. Koi bhi problem ho to default zone."""
    if not MONETAG_OPTIMIZE or len(ZONES) < 2:
        return MONETAG_DEFAULT_ZONE
    if time.time() - _cache["t"] > CACHE_TTL:
        await _refresh_cache()
    try:
        return pick_zone(_cache["weights"] or {}, _cache["benched"], MONETAG_DEFAULT_ZONE)
    except Exception:  # noqa: BLE001
        return MONETAG_DEFAULT_ZONE


async def tick():
    """Ek optimization round. Lease ki wajah se multi-instance me bhi sirf ek chalta hai."""
    if not await store.try_lease("optimizer", MONETAG_OPT_INTERVAL * 0.8):
        return
    now = time.time()
    st = await store.get_state() or {}
    rows = await store.window_rows(MONETAG_WINDOW_DAYS, "zone")
    arms = {str(r["_id"]): store.row_to_arm(r) for r in rows}

    benched = {z: u for z, u in (st.get("benched") or {}).items() if u > now}
    for z in find_bench(await store.recent_bucket_stats(MONETAG_BENCH_MINUTES),
                        MONETAG_BENCH_MIN_ATTEMPTS, MONETAG_BENCH_FAIL_RATE):
        if z in MONETAG_ZONES and z not in benched:
            benched[z] = now + MONETAG_BENCH_MINUTES * 60
            log.warning("zone %s benched for %s min (high SDK failure rate)", z, MONETAG_BENCH_MINUTES)

    prev = st.get("leader")
    plan = compute_plan(arms, ZONES, MONETAG_DEFAULT_ZONE, prev, float(st.get("last_switch_ts") or 0.0), now, PARAMS)
    await store.save_state({
        "leader": plan.leader, "weights": plan.weights, "scores": plan.scores, "benched": benched,
        "reason": plan.reason, "updated_at": now,
        "last_switch_ts": now if plan.switched else float(st.get("last_switch_ts") or 0.0),
    })
    if plan.switched or prev is None:
        await store.add_history({"from": prev, "to": plan.leader, "reason": plan.reason,
                                 "scores": plan.scores, "weights": plan.weights})
    _cache["t"] = 0.0   # is instance par agla select_zone turant naya plan padhe


async def optimizer_loop():
    if not MONETAG_OPTIMIZE or len(ZONES) < 2:
        return
    await asyncio.sleep(20)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("optimizer tick failed")
        await asyncio.sleep(MONETAG_OPT_INTERVAL)
