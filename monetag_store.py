"""Monetag persistence: attempts, postback events (idempotent), O(1) rollups, optimizer state, reports.

Collections (all new; existing collections untouched):
  monetag_ads        one doc per ad show (_id = ymid)            TTL 30d
  monetag_events     one doc per (ymid, event_type) = dedupe key  TTL 30d
  monetag_daily      rollup per (day, zone)
  monetag_buckets    10-min rollup per zone (auto-bench only)     TTL 2d
  monetag_country    rollup per (day, country)  [needs MONETAG_GEO_HEADER]
  monetag_confirmed  revenue the owner typed in from the Monetag dashboard
  monetag_state      optimizer plan + leases
  monetag_history    optimizer decisions
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

from ads.security import session_expired
from ads.store import DuplicateKeyError          # pymongo's class in production, a stand-in where pymongo isn't installed
from config import MONETAG_OPTIMIZE, MONETAG_ZONES, STATS_TZ_OFFSET_MIN

log = logging.getLogger("monetag")

_DB = None
BUCKET_SECONDS = 600
HARD_FAIL_REASONS = {"empty_feed", "timeout", "error"}      # zone-quality failures (bench ke liye)
ALL_FAIL_REASONS = HARD_FAIL_REASONS | {"blocked", "non_valued", "closed", "unverified"}
YMID_RE = re.compile(r"^[A-Za-z0-9._-]{4,80}$")
MAX_PRICE_USD = 2.0   # ek event ka sanity cap


def _database():
    global _DB
    if _DB is None:
        import db  # lazy: tests me motor ki zarurat nahi
        _DB = db._db
    return _DB


def _c(name: str):
    return _database()[name]


def day_key(offset_days: int = 0) -> str:
    now = datetime.now(timezone.utc) + timedelta(minutes=STATS_TZ_OFFSET_MIN)
    return (now - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def window_days(n: int) -> list:
    return [day_key(i) for i in range(n)]


async def init():
    """Compatibility shim: indexes are defined once in migrations/registry.py and applied by the versioned migration set at startup."""
    from migrations import registry
    from migrator import ensure_index

    for coll, keys, opts in registry.MONETAG_INDEXES:
        await ensure_index(_c(coll), keys, **opts)


# ---------------------------------------------------------------- rollups
async def _inc_daily(zone: str, inc: dict):
    d = day_key()
    await _c("monetag_daily").update_one(
        {"_id": f"{d}|{zone}"}, {"$inc": inc, "$setOnInsert": {"day": d, "zone": zone}}, upsert=True
    )


async def _inc_bucket(zone: str, inc: dict):
    b = int(time.time() // BUCKET_SECONDS) * BUCKET_SECONDS
    await _c("monetag_buckets").update_one(
        {"_id": f"{b}|{zone}"},
        {"$inc": inc, "$setOnInsert": {
            "bucket": b, "zone": zone,
            "expire_at": datetime.now(timezone.utc) + timedelta(days=2)}},
        upsert=True,
    )


# ---------------------------------------------------------------- ad lifecycle
async def record_attempt(ymid: str, sid: str, zone: str, country: str | None = None, user_id: int | None = None) -> bool:
    try:
        await _c("monetag_ads").insert_one({
            "_id": ymid, "sid": sid, "user_id": user_id, "zone": zone, "country": country,
            "t": time.time(), "created": datetime.now(timezone.utc),
            "outcome": None, "pb_seen": False, "pb_valued": False, "done_first_at": None,
        })
    except DuplicateKeyError:
        return False
    await _inc_daily(zone, {"attempts": 1})
    if MONETAG_OPTIMIZE:
        await _inc_bucket(zone, {"attempts": 1})
    return True


async def count_empty_fails(sid: str) -> int:
    """Is session me Monetag se kitni baar sach me 'ad nahi hai' (empty_feed) report hua."""
    return await _c("monetag_ads").count_documents({"sid": sid, "outcome": "fail", "reason": "empty_feed"})


async def get_ad(ymid: str):
    return await _c("monetag_ads").find_one({"_id": ymid})


async def get_session(sid: str):
    return await _c("sessions").find_one({"sid": sid})


async def mark_done_first(ymid: str) -> float:
    """Client ne pehli baar 'ad khatam' bola — postback wait ka timer yahin se."""
    now = time.time()
    await _c("monetag_ads").update_one({"_id": ymid, "done_first_at": None}, {"$set": {"done_first_at": now}})
    ad = await get_ad(ymid)
    return float((ad or {}).get("done_first_at") or now)


async def record_fail(ymid: str, sid: str, reason: str) -> bool:
    """Har ad show ka outcome sirf EK baar set hota hai (duplicate/spam reports ignore)."""
    if reason not in ALL_FAIL_REASONS:
        reason = "error"
    ad = await _c("monetag_ads").find_one_and_update(
        {"_id": ymid, "sid": sid, "outcome": None}, {"$set": {"outcome": "fail", "reason": reason}}
    )
    if not ad:
        return False
    field = {"empty_feed": "fail_empty"}.get(reason, "fail_other")
    await _inc_daily(ad["zone"], {field: 1})
    if MONETAG_OPTIMIZE and reason in HARD_FAIL_REASONS:
        await _inc_bucket(ad["zone"], {"hard_fails": 1})
    return True


async def record_completion(ymid: str) -> bool:
    ad = await _c("monetag_ads").find_one_and_update(
        {"_id": ymid, "outcome": None}, {"$set": {"outcome": "ok"}}
    )
    if not ad:
        return False
    await _inc_daily(ad["zone"], {"completions": 1})
    return True


# ---------------------------------------------------------------- Monetag postback
def _num(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if f == f and f >= 0 else default
    except (TypeError, ValueError):
        return default


_DIGITS = re.compile(r"^\d{1,15}$")
EVENTS = ("impression", "click")


async def record_postback(q: dict, *, now: float | None = None, max_age: int = 600, session_ttl: int = 1800,
                          require_telegram_id: bool = True) -> str:
    """The postback's secret was already checked by ads/postback.py. Returns:
        'ok' | 'dup'                                             accepted (dup = exact repeat, nothing is processed twice)
        'malformed' | 'unknown_ad' | 'user_mismatch' | 'zone_mismatch' | 'session_invalid' | 'expired' | 'closed'   rejected
    An impression only counts for verification if it is for an ad we issued, for THAT user (telegram_id and stored user_id
    and the session owner must all agree), for the ad's own zone, while the session is alive, the ad is younger than
    max_age and its outcome is still open. Idempotent per (ymid, event_type): Monetag itself does not dedupe."""
    now = time.time() if now is None else now
    ymid = (q.get("ymid") or "").strip()
    event = (q.get("event_type") or "").strip().lower()
    reward = (q.get("reward_event_type") or "").strip().lower()
    tg = (q.get("telegram_id") or "").strip()
    zone_q = (q.get("zone_id") or "").strip()
    if not YMID_RE.match(ymid) or event not in EVENTS or reward not in ("valued", "non_valued"):
        return "malformed"
    if (tg and not _DIGITS.match(tg)) or (not tg and require_telegram_id) or (zone_q and not _DIGITS.match(zone_q)):
        return "malformed"

    ad = await get_ad(ymid)
    if not ad:
        return "unknown_ad"                                   # not issued by us (other app / forged / purged)
    uid = ad.get("user_id")
    sess = await get_session(ad.get("sid"))
    if uid is None or (tg and int(tg) != int(uid)) or (sess and sess.get("user_id") != uid):
        return "user_mismatch"                                # attempt not bound to the user the postback names
    if zone_q and zone_q != str(ad.get("zone")):
        return "zone_mismatch"
    if await _c("monetag_events").find_one({"_id": f"{ymid}|{event}"}):
        return "dup"                                          # idempotent replay: already processed, change nothing
    if session_expired(sess, session_ttl, now):
        return "session_invalid"
    if now - float(ad.get("t") or 0) > max_age:
        return "expired"
    if event == "impression" and (ad.get("outcome") is not None or sess.get("delivered")):
        return "closed"                                       # reward already settled / abandoned: late postback is stale

    price = min(_num(q.get("estimated_price")), MAX_PRICE_USD) if reward == "valued" else 0.0
    main_zone = zone_q or str(ad.get("zone") or "")
    sub_zone = re.sub(r"\D", "", q.get("sub_zone_id") or "")[:12]

    try:
        await _c("monetag_events").insert_one({
            "_id": f"{ymid}|{event}", "ymid": ymid, "event": event, "reward": reward,
            "price": price, "zone": main_zone, "sub_zone": sub_zone,
            "created": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return "dup"

    update = {"pb_seen": True} if event == "impression" else {}
    if event == "impression" and reward == "valued":
        update["pb_valued"] = True
    if update:
        await _c("monetag_ads").find_one_and_update({"_id": ymid, "outcome": None}, {"$set": update})
    zone = ad.get("zone") or main_zone or "unknown"

    if event == "impression":
        inc = {"imp": 1, "imp_valued": 1 if reward == "valued" else 0, "rev_imp": price, "rev_imp_sq": price * price}
    else:
        inc = {"clicks": 1, "rev_click": price}
    await _inc_daily(zone, inc)

    country = ad.get("country")
    if country and event == "impression":
        d = day_key()
        await _c("monetag_country").update_one(
            {"_id": f"{d}|{country}"},
            {"$inc": {"imp": 1, "imp_valued": 1 if reward == "valued" else 0, "rev": price},
             "$setOnInsert": {"day": d, "country": country}},
            upsert=True,
        )
    elif country and event == "click" and price:
        d = day_key()
        await _c("monetag_country").update_one(
            {"_id": f"{d}|{country}"}, {"$inc": {"rev": price}, "$setOnInsert": {"day": d, "country": country}},
            upsert=True,
        )
    return "ok"


# ---------------------------------------------------------------- optimizer state
async def get_state():
    return await _c("monetag_state").find_one({"_id": "plan"})


async def save_state(doc: dict):
    doc = dict(doc)
    doc.pop("_id", None)
    await _c("monetag_state").update_one({"_id": "plan"}, {"$set": doc}, upsert=True)


async def add_history(entry: dict):
    entry = dict(entry, created=datetime.now(timezone.utc), t=time.time())
    await _c("monetag_history").insert_one(entry)


async def try_lease(name: str, ttl: float) -> bool:
    """Multi-instance safe: sirf ek instance optimizer chalaye."""
    now = time.time()
    try:
        r = await _c("monetag_state").find_one_and_update(
            {"_id": f"lease:{name}", "until": {"$lt": now}}, {"$set": {"until": now + ttl}}
        )
        if r:
            return True
        await _c("monetag_state").insert_one({"_id": f"lease:{name}", "until": now + ttl})
        return True
    except DuplicateKeyError:
        return False


_SUM_FIELDS = ("attempts", "completions", "imp", "imp_valued", "rev_imp", "rev_imp_sq",
               "clicks", "rev_click", "fail_empty", "fail_other", "unmatched")


def _group_stage(key: str) -> dict:
    g = {"_id": key}
    for f in _SUM_FIELDS:
        g[f] = {"$sum": f"${f}"}
    return {"$group": g}


async def window_rows(days: int, by: str = "zone") -> list:
    """Ek hi aggregation, window ke saare (day,zone) docs. by = 'zone' | 'day'."""
    pipeline = [{"$match": {"day": {"$in": window_days(days)}}}, _group_stage("$" + by)]
    return await _c("monetag_daily").aggregate(pipeline).to_list(length=1000)


def row_to_arm(row: dict):
    from monetag_opt import Arm
    return Arm(
        zone=str(row["_id"]),
        **{f: (int(row.get(f, 0)) if f not in ("rev_imp", "rev_imp_sq", "rev_click") else float(row.get(f, 0.0)))
           for f in _SUM_FIELDS if f != "unmatched"},
    )


async def recent_bucket_stats(minutes: int) -> dict:
    since = int((time.time() - minutes * 60) // BUCKET_SECONDS) * BUCKET_SECONDS
    pipeline = [
        {"$match": {"bucket": {"$gte": since}}},
        {"$group": {"_id": "$zone", "attempts": {"$sum": "$attempts"}, "fails": {"$sum": "$hard_fails"}}},
    ]
    rows = await _c("monetag_buckets").aggregate(pipeline).to_list(length=100)
    return {r["_id"]: (int(r.get("attempts", 0)), int(r.get("fails", 0))) for r in rows}


# ---------------------------------------------------------------- confirmed revenue (manual, from dashboard)
async def set_confirmed(day: str, revenue_usd: float, zone: str = "all") -> None:
    await _c("monetag_confirmed").update_one(
        {"_id": f"{day}|{zone}"},
        {"$set": {"day": day, "zone": zone, "revenue": float(revenue_usd), "updated": time.time()}},
        upsert=True,
    )


# ---------------------------------------------------------------- report
def _ratio(a, b):
    return (a / b) if b else None


def _zone_block(r: dict) -> dict:
    imp, attempts = int(r.get("imp", 0)), int(r.get("attempts", 0))
    rev = float(r.get("rev_imp", 0.0)) + float(r.get("rev_click", 0.0))
    imp_valued = int(r.get("imp_valued", 0))
    return {
        "attempts": attempts,
        "impressions": imp,
        "valid_impressions": imp_valued,
        "clicks": int(r.get("clicks", 0)),
        "completions": int(r.get("completions", 0)),
        "failures": int(r.get("fail_empty", 0)) + int(r.get("fail_other", 0)),
        "no_fill_reports": int(r.get("fail_empty", 0)),
        "estimated_revenue_usd": round(rev, 6),
        "ecpm_usd": round(rev / imp * 1000, 4) if imp else None,
        "valid_ecpm_usd": round(rev / imp_valued * 1000, 4) if imp_valued else None,
        "completion_rate": _ratio(int(r.get("completions", 0)), attempts),
        "impression_rate": _ratio(imp, attempts),
        # fill rate sirf 'Empty feed' reports se maapa ja sakta hai (client-reported)
        "fill_rate": (1 - int(r.get("fail_empty", 0)) / attempts) if attempts else None,
        "unmatched_events": int(r.get("unmatched", 0)),
    }


async def build_report(days: int = 7) -> dict:
    zone_rows = await window_rows(days, "zone")
    zones = {str(r["_id"]): _zone_block(r) for r in zone_rows}
    per_day = {str(r["_id"]): _zone_block(r) for r in await window_rows(days, "day")}
    tot = {f: 0 for f in _SUM_FIELDS}
    for r in zone_rows:
        for f in _SUM_FIELDS:
            tot[f] += r.get(f, 0)
    total = _zone_block(tot)

    wanted = window_days(days)
    country = await _c("monetag_country").aggregate([
        {"$match": {"day": {"$in": wanted}}},
        {"$group": {"_id": "$country", "imp": {"$sum": "$imp"}, "rev": {"$sum": "$rev"}}},
    ]).to_list(length=300)
    countries = {
        str(r["_id"]): {"impressions": int(r["imp"]), "estimated_revenue_usd": round(float(r["rev"]), 6),
                        "ecpm_usd": round(r["rev"] / r["imp"] * 1000, 4) if r["imp"] else None}
        for r in country
    }

    conf_docs = await _c("monetag_confirmed").find({"day": {"$in": wanted}}).to_list(length=500)
    confirmed_total = sum(d["revenue"] for d in conf_docs if d.get("zone") == "all")
    confirmed_days = {d["day"] for d in conf_docs if d.get("zone") == "all"}
    est_same_days = sum(per_day[d]["estimated_revenue_usd"] for d in confirmed_days if d in per_day)

    state = await get_state() or {}
    hist = await _c("monetag_history").find({}).sort("t", -1).limit(10).to_list(length=10)
    best = max(((z, b["ecpm_usd"]) for z, b in zones.items() if b["ecpm_usd"] is not None),
               key=lambda x: x[1], default=(None, None))
    return {
        "window_days": days,
        "optimizer": {
            "enabled": MONETAG_OPTIMIZE, "configured_zones": list(MONETAG_ZONES),
            "leader": state.get("leader"), "weights": state.get("weights"),
            "benched": {z: u for z, u in (state.get("benched") or {}).items() if u > time.time()},
            "last_reason": state.get("reason"),
        },
        "total": total,
        "best_zone_by_ecpm": {"zone": best[0], "ecpm_usd": best[1]},
        "zones": zones,
        "countries": countries,
        "revenue": {
            "estimated_usd": total["estimated_revenue_usd"],
            "estimated_note": "Sum of postback estimated_price. An ESTIMATE, not a payout.",
            "confirmed_usd": round(confirmed_total, 6) if conf_docs else None,
            "confirmed_note": "Only what you entered from the Monetag dashboard (POST /admin/monetag/confirmed).",
            "estimate_vs_confirmed_ratio": _ratio(est_same_days, confirmed_total) if confirmed_total else None,
        },
        "history": [{"t": h.get("t"), "from": h.get("from"), "to": h.get("to"), "reason": h.get("reason")} for h in hist],
    }


def _usd(v):
    return "n/a" if v is None else f"${v:,.2f}"


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.0f}%"


def format_report_html(rep: dict) -> str:
    t, o, r = rep["total"], rep["optimizer"], rep["revenue"]
    lines = [
        f"💰 <b>Monetag — last {rep['window_days']} days</b>\n",
        f"Impressions: <b>{t['impressions']}</b> (valid {t['valid_impressions']})   Attempts: {t['attempts']}",
        f"Estimated revenue: <b>{_usd(t['estimated_revenue_usd'])}</b>   eCPM: <b>{_usd(t['ecpm_usd'])}</b>",
        f"Confirmed revenue: <b>{_usd(r['confirmed_usd'])}</b> <i>(manual, from dashboard)</i>",
        f"Completion: {_pct(t['completion_rate'])}   Fill*: {_pct(t['fill_rate'])}\n",
        f"🧠 Optimizer: <b>{'ON' if o['enabled'] else 'OFF'}</b>   Leader: <code>{o['leader'] or '-'}</code>",
    ]
    if o["weights"]:
        lines.append("Traffic: " + ", ".join(f"<code>{z}</code> {w * 100:.0f}%" for z, w in o["weights"].items()))
    if o["benched"]:
        lines.append("⏸ Benched: " + ", ".join(o["benched"]))
    lines.append("\n<b>Per zone</b>")
    for z, b in sorted(rep["zones"].items(), key=lambda x: -(x[1]["ecpm_usd"] or 0)):
        lines.append(f"• <code>{z}</code>: imp {b['impressions']}, eCPM {_usd(b['ecpm_usd'])}, "
                     f"est {_usd(b['estimated_revenue_usd'])}, compl {_pct(b['completion_rate'])}")
    if rep["countries"]:
        lines.append("\n<b>Country</b>")
        for c, b in sorted(rep["countries"].items(), key=lambda x: -x[1]["impressions"])[:5]:
            lines.append(f"• {c}: imp {b['impressions']}, eCPM {_usd(b['ecpm_usd'])}")
    if rep["history"]:
        h = rep["history"][0]
        lines.append(f"\n🕘 Last change: {h.get('from') or '-'} → {h.get('to')}")
    lines.append("\n<i>*Fill = 1 − 'Empty feed' reports / attempts. Estimated ≠ final payout.</i>")
    return "\n".join(lines)
