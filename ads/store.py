"""Persistence for the smart ad engine. All collections are NEW; existing ones are only read, or (sessions) get
extra optional fields (smart_active, smart_active_until, smart_chain, smart_idx).

  ad_config        _id 'config' (admin settings) | 'plan' (scores/benched) | 'health' | 'lease:*'
  ad_requests      one doc per ad attempt (_id = ad_id, also used as Monetag ymid)
  ad_events        audit trail, _id = '<ad_id>|<event>' (dedupe key)
  ad_rewards       one doc per VERIFIED reward (_id = ad_id => idempotent)
  ad_revenue       owner-confirmed revenue per (day, network)
  ad_stats_daily   counters per (day, network)
  ad_buckets       10-minute counters per network (circuit breaker + recent window)   TTL 2d
  ad_plan_history  leader changes
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import settings

try:
    from pymongo.errors import DuplicateKeyError
except ImportError:  # tests run without pymongo
    class DuplicateKeyError(Exception):
        pass

try:
    from pymongo import ReturnDocument
    _AFTER = ReturnDocument.AFTER
except ImportError:  # tests run without pymongo
    _AFTER = True

_DB = None
BUCKET_SECONDS = 600
HARD_FAIL = {"no_fill", "timeout", "blocked", "error"}
TERMINAL = {"verified", "denied", "failed", "abandoned"}


def set_database(db) -> None:
    global _DB
    _DB = db


def _c(name: str):
    global _DB
    if _DB is None:
        import db  # lazy so tests never need motor
        _DB = db._db
    return _DB[name]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def day_key(offset_days: int = 0, ts: float | None = None) -> str:
    base = datetime.fromtimestamp(ts, timezone.utc) if ts else now_utc()
    return ((base + timedelta(minutes=settings.STATS_TZ_OFFSET_MIN)) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def window_days(n: int) -> list:
    return [day_key(i) for i in range(n)]


# ------------------------------------------------------------------ indexes / migration
async def init() -> None:
    from .migrate import run
    await run(_c)


# ------------------------------------------------------------------ config / state
async def get_doc(_id: str) -> dict:
    return await _c("ad_config").find_one({"_id": _id}) or {}


async def set_doc(_id: str, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k != "_id"}
    await _c("ad_config").update_one({"_id": _id}, {"$set": fields}, upsert=True)


async def get_config_raw() -> dict:
    return await get_doc("config")


async def save_config(cfg: dict) -> None:
    await set_doc("config", dict(cfg, updated_at=time.time()))


async def try_lease(name: str, ttl: float) -> bool:
    now = time.time()
    try:
        r = await _c("ad_config").find_one_and_update({"_id": f"lease:{name}", "until": {"$lt": now}}, {"$set": {"until": now + ttl}})
        if r:
            return True
        await _c("ad_config").insert_one({"_id": f"lease:{name}", "until": now + ttl})
        return True
    except DuplicateKeyError:
        return False


async def add_history(entry: dict) -> None:
    await _c("ad_plan_history").insert_one(dict(entry, created=now_utc(), t=time.time()))


async def touch_health(network: str, ok: bool, reason: str | None = None) -> None:
    f = {f"{network}.last_success": time.time()} if ok else {f"{network}.last_fail": time.time(), f"{network}.last_fail_reason": reason}
    await _c("ad_config").update_one({"_id": "health"}, {"$set": f}, upsert=True)


# ------------------------------------------------------------------ rollups
async def bump(network: str, inc: dict) -> None:
    d = day_key()
    await _c("ad_stats_daily").update_one({"_id": f"{d}|{network}"}, {"$inc": inc, "$setOnInsert": {"day": d, "network": network}}, upsert=True)
    b = int(time.time() // BUCKET_SECONDS) * BUCKET_SECONDS
    await _c("ad_buckets").update_one(
        {"_id": f"{b}|{network}"},
        {"$inc": inc, "$setOnInsert": {"bucket": b, "network": network, "expire_at": now_utc() + timedelta(days=2)}},
        upsert=True,
    )


# ------------------------------------------------------------------ session slot (one live ad per user request)
async def acquire_slot(sid: str, ad_id: str, ttl: float):
    """Atomically take the session's single 'ad in progress' slot. Returns (acquired, previous_ad_id)."""
    now = time.time()
    old = await _c("sessions").find_one_and_update(
        {"sid": sid, "delivered": False, "$or": [{"smart_active": None}, {"smart_active_until": {"$lt": now}}]},
        {"$set": {"smart_active": ad_id, "smart_active_until": now + ttl}},
    )
    return (old is not None), ((old or {}).get("smart_active"))


async def swap_slot(sid: str, old_ad: str, new_ad: str, ttl: float) -> bool:
    """Fallback hand-over: only the holder of the slot can pass it to the next attempt."""
    r = await _c("sessions").find_one_and_update(
        {"sid": sid, "delivered": False, "smart_active": old_ad},
        {"$set": {"smart_active": new_ad, "smart_active_until": time.time() + ttl}},
    )
    return r is not None


async def extend_slot(sid: str, ad_id: str, ttl: float) -> None:
    await _c("sessions").update_one({"sid": sid, "smart_active": ad_id}, {"$set": {"smart_active_until": time.time() + ttl}})


async def release_slot(sid: str, ad_id: str) -> None:
    await _c("sessions").update_one({"sid": sid, "smart_active": ad_id}, {"$set": {"smart_active": None, "smart_active_until": 0}})


async def set_chain(sid: str, chain: list) -> None:
    await _c("sessions").update_one({"sid": sid}, {"$set": {"smart_chain": chain, "smart_idx": 0}})


async def get_session_chain(sid: str):
    s = await _c("sessions").find_one({"sid": sid}) or {}
    return list(s.get("smart_chain") or []), int(s.get("smart_idx") or 0)


async def advance_chain(sid: str, from_idx: int) -> bool:
    r = await _c("sessions").find_one_and_update({"sid": sid, "smart_idx": from_idx}, {"$set": {"smart_idx": from_idx + 1}})
    return r is not None


async def next_round(sid: str) -> int:
    """Each /api/smart/start is one 'round' (a Monetag->Tads chain). Server-side counter, never client supplied."""
    s = await _c("sessions").find_one_and_update({"sid": sid}, {"$inc": {"smart_round": 1}}, return_document=_AFTER)
    return int((s or {}).get("smart_round") or 1)


async def flag_exhausted(ad_id: str) -> None:
    """Last attempt of a round failed with nothing left to try (the round is over)."""
    await _c("ad_requests").update_one({"_id": ad_id}, {"$set": {"round_exhausted": True}})


async def attempts_for_sid(sid: str) -> list:
    return await _c("ad_requests").find({"sid": sid}).to_list(length=200)


# ---- ad-blocker challenge (server-verified bait fetches) + grace log
async def create_challenge(doc: dict) -> bool:
    try:
        await _c("ad_gate").insert_one(dict(doc, created=now_utc()))
        return True
    except DuplicateKeyError:
        return False


async def get_challenge(nonce: str):
    return await _c("ad_gate").find_one({"_id": nonce})


async def record_bait_hit(nonce: str, key: str) -> None:
    await _c("ad_gate").update_one({"_id": nonce}, {"$set": {"hit_" + key: time.time()}})


async def consume_challenge(nonce: str) -> bool:
    r = await _c("ad_gate").find_one_and_update({"_id": nonce, "used": None}, {"$set": {"used": time.time()}})
    return r is not None


async def log_grace(doc: dict) -> None:
    await _c("ad_grace").insert_one(dict(doc, created=now_utc(), t=time.time()))


async def count_recent_grace(user_id: int, since_ts: float) -> int:
    rows = await _c("ad_grace").find({"user_id": user_id, "t": {"$gte": since_ts}}).to_list(length=1000)
    return len(rows)


# ------------------------------------------------------------------ attempts
async def create_attempt(doc: dict) -> bool:
    doc = dict(doc, created=now_utc(), outcome=None, status="created", shown_at=None, claimed_at=None,
               client_completed=False, client_non_valued=False)
    try:
        await _c("ad_requests").insert_one(doc)
    except DuplicateKeyError:
        return False
    await bump(doc["network"], {"requests": 1})
    return True


async def get_attempt(ad_id: str):
    return await _c("ad_requests").find_one({"_id": ad_id})


async def log_event(ad_id: str, name: str, **data) -> bool:
    try:
        await _c("ad_events").insert_one({"_id": f"{ad_id}|{name}", "ad_id": ad_id, "event": name, "data": data or None,
                                          "t": time.time(), "created": now_utc()})
        return True
    except DuplicateKeyError:
        return False


async def mark_shown(ad_id: str):
    ad = await _c("ad_requests").find_one_and_update({"_id": ad_id, "outcome": None, "shown_at": None},
                                                     {"$set": {"shown_at": time.time(), "status": "shown"}})
    if ad:
        await bump(ad["network"], {"shown": 1})
    return ad


async def record_claim(ad_id: str, non_valued: bool):
    """First completion claim wins; later claims only read. Returns the attempt (post-claim) or None."""
    ad = await _c("ad_requests").find_one_and_update(
        {"_id": ad_id, "outcome": None, "claimed_at": None},
        {"$set": {"claimed_at": time.time(), "client_completed": True, "client_non_valued": bool(non_valued)}},
    )
    if ad:
        await bump(ad["network"], {"completed": 1})
    return await get_attempt(ad_id)


async def finalize(ad_id: str, outcome: str, reason: str, trust: str | None = None, release: bool = True):
    """Exactly-once terminal outcome. Returns the pre-update attempt, or None if it was already terminal."""
    assert outcome in TERMINAL
    ad = await _c("ad_requests").find_one_and_update(
        {"_id": ad_id, "outcome": None},
        {"$set": {"outcome": outcome, "reason": reason, "trust": trust, "status": outcome, "finished_at": time.time()}},
    )
    if not ad:
        return None
    n, inc = ad["network"], {}
    if outcome == "verified":
        inc = {"verified": 1, ("verified_server" if trust and trust != settings.CLIENT_ATTESTED else "verified_client"): 1}
    elif outcome == "denied":
        inc = {"denied": 1}
    elif outcome == "abandoned":
        inc = {"abandoned": 1}
    else:
        inc = {"failed_nofill" if reason == "no_fill" else "failed_error": 1}
        if reason in HARD_FAIL:
            inc["hard_fails"] = 1
    await bump(n, inc)
    if release:
        await release_slot(ad["sid"], ad_id)
    await touch_health(n, outcome == "verified", None if outcome == "verified" else reason)
    return ad


async def insert_reward(doc: dict) -> bool:
    try:
        await _c("ad_rewards").insert_one(dict(doc, _id=doc["ad_id"], created=now_utc(), t=time.time()))
        return True
    except DuplicateKeyError:
        return False


async def expire_stale(older_than_s: float, limit: int = 200) -> int:
    cutoff = time.time() - older_than_s
    docs = await _c("ad_requests").find({"outcome": None, "t": {"$lt": cutoff}}).limit(limit).to_list(length=limit)
    n = 0
    for d in docs:
        if await finalize(d["_id"], "abandoned", "expired"):
            n += 1
    return n


# ------------------------------------------------------------------ revenue (owner-confirmed only)
async def set_confirmed_revenue(day: str, network: str, usd: float) -> None:
    await _c("ad_revenue").update_one(
        {"_id": f"{day}|{network}"},
        {"$set": {"day": day, "network": network, "revenue_usd": float(usd), "source": "manual_confirmed", "updated": time.time()}},
        upsert=True,
    )


# ------------------------------------------------------------------ read side
_COUNTERS = ("requests", "shown", "completed", "verified", "verified_server", "verified_client", "denied",
             "failed_nofill", "failed_error", "abandoned", "hard_fails")


async def daily_rows(days: list) -> list:
    return await _c("ad_stats_daily").find({"day": {"$in": days}}).to_list(length=5000)


async def all_daily_rows() -> list:
    return await _c("ad_stats_daily").find({}).to_list(length=100000)


async def revenue_rows(days: list | None = None) -> list:
    q = {"day": {"$in": days}} if days is not None else {}
    return await _c("ad_revenue").find(q).to_list(length=10000)


async def bucket_rows(since_ts: float) -> list:
    since = int(since_ts // BUCKET_SECONDS) * BUCKET_SECONDS
    return await _c("ad_buckets").find({"bucket": {"$gte": since}}).to_list(length=20000)


async def monetag_estimates(days: int) -> dict:
    """Monetag postback estimated_price per day (ESTIMATE, not payout). {} if Monetag analytics is unavailable."""
    try:
        import monetag_store
        rows = await monetag_store.window_rows(days, "day")
    except Exception:  # noqa: BLE001
        return {}
    return {str(r["_id"]): {"revenue": float(r.get("rev_imp", 0.0)) + float(r.get("rev_click", 0.0)), "impressions": int(r.get("imp", 0))}
            for r in rows}


async def recent_rewards(limit: int = 50) -> list:
    docs = await _c("ad_rewards").find({}).sort("t", -1).limit(limit).to_list(length=limit)
    return [{"ad_id": d["ad_id"], "network": d["network"], "trust": d.get("trust"), "user_id": d.get("user_id"), "t": d.get("t")} for d in docs]
