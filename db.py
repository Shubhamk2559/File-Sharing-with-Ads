import re
import time
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

from config import DB_NAME, MONGO_URI, RT, SESSION_TTL, STATS_TZ_OFFSET_MIN

# Bounded timeouts: while MongoDB is unreachable a request fails after ~serverSelectionTimeout (5 s) instead of pymongo's 30 s default.
# pymongo's monitors reconnect automatically once the server is back; health.db_monitor_loop makes outage / recovery visible.
_client = AsyncIOMotorClient(
    MONGO_URI,
    serverSelectionTimeoutMS=RT.mongo_selection_timeout_ms,
    connectTimeoutMS=RT.mongo_connect_timeout_ms,
    socketTimeoutMS=RT.mongo_socket_timeout_ms,
    maxPoolSize=RT.mongo_max_pool_size,
    retryReads=True,
    retryWrites=True,
    appname="filebot",
)
_db = _client[DB_NAME]
files = _db.files
sessions = _db.sessions
pending_deletes = _db.pending_deletes
stats = _db.stats


async def ping() -> None:
    """Cheap probe of the database connection (raises on any connectivity problem)."""
    await _client.admin.command("ping")


def close() -> None:
    """Graceful shutdown: release pooled connections."""
    _client.close()


async def init():
    """Compatibility shim: the single source of truth for indexes is migrations/registry.py (BotManager.start runs the full, versioned,
    lock-protected migration set). Calling this directly is still safe and idempotent."""
    from migrations import registry
    from migrator import ensure_index

    for coll, keys, opts in registry.CORE_INDEXES:
        await ensure_index(_db[coll], keys, **opts)
    await ensure_index(sessions, "created", expireAfterSeconds=SESSION_TTL)


# ---------- files ----------
async def save_file(token: str, locations: list, caption: str):
    await files.insert_one(
        {
            "token": token,
            "locations": locations,
            "caption": caption,
            "created_at": time.time(),
        }
    )


async def get_file(token: str):
    return await files.find_one({"token": token})


async def count_files() -> int:
    return await files.count_documents({})


async def list_files(skip: int, limit: int):
    return await files.find().sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def delete_file(token: str):
    await files.delete_one({"token": token})


# ---------- verification sessions ----------
async def create_session(sid, token, user_id, chat_id, bot_id, required, prompt_msg=None):
    await sessions.insert_one(
        {
            "sid": sid,
            "token": token,
            "user_id": user_id,
            "chat_id": chat_id,
            "bot_id": bot_id,
            "ads_done": 0,
            "ads_required": required,
            "ad_started_at": None,
            "delivered": False,
            "prompt_msg": prompt_msg,
            "created": datetime.now(timezone.utc),
        }
    )


async def get_session(sid: str):
    return await sessions.find_one({"sid": sid})


async def start_ad(sid: str, ymid: str | None = None, zone: str | None = None):
    """ymid/zone optional: purana call (sirf sid) pehle jaisa hi kaam karta hai."""
    fields = {"ad_started_at": time.time()}
    if ymid:
        fields["cur_ymid"] = ymid
        fields["cur_zone"] = zone
    await sessions.update_one({"sid": sid, "delivered": False}, {"$set": fields})


async def complete_ad(sid: str, min_seconds: int):
    cutoff = time.time() - min_seconds
    return await sessions.find_one_and_update(
        {
            "sid": sid,
            "delivered": False,
            "ad_started_at": {"$ne": None, "$lte": cutoff},
        },
        {"$inc": {"ads_done": 1}, "$set": {"ad_started_at": None}},
        return_document=ReturnDocument.AFTER,
    )


async def grant_grace(sid: str) -> bool:
    """No-fill grace: session ko 'saare ads ho gaye' mark karo (delivery claim_delivery se hi hogi)."""
    r = await sessions.find_one_and_update(
        {"sid": sid, "delivered": False},
        [{"$set": {"ads_done": "$ads_required", "grace": True}}],
    )
    return r is not None


async def claim_delivery(sid: str) -> bool:
    r = await sessions.find_one_and_update(
        {
            "sid": sid,
            "delivered": False,
            "$expr": {"$gte": ["$ads_done", "$ads_required"]},
        },
        {"$set": {"delivered": True}},
    )
    return r is not None


async def unclaim_delivery(sid: str):
    await sessions.update_one({"sid": sid}, {"$set": {"delivered": False}})


# ---------- auto-delete of delivered files ----------
async def schedule_delete(bot_id: int, chat_id: int, message_id: int, delete_at: float):
    await pending_deletes.insert_one(
        {"bot_id": bot_id, "chat_id": chat_id, "message_id": message_id, "delete_at": delete_at}
    )


async def due_deletes(now: float, limit: int = 100):
    return await pending_deletes.find({"delete_at": {"$lte": now}}).limit(limit).to_list(length=limit)


async def remove_delete(_id):
    await pending_deletes.delete_one({"_id": _id})


# ---------- posts (image + multiple videos behind one link) ----------
async def save_post(
    token: str,
    items: list,
    post_caption: str,
    photo: str,
    thumb_type: str = "photo",
    src_chat=None,
    src_head: int | None = None,
):
    """photo = thumbnail ka file_id (thumb_type: photo | video | animation)."""
    await files.insert_one(
        {
            "token": token,
            "kind": "post",
            "items": items,  # [{"locations": [...], "caption": str}, ...]
            "locations": [],
            "caption": f"📝 Post • {len(items)} video{'s' if len(items) != 1 else ''}",
            "post_caption": post_caption,
            "photo": photo,
            "thumb_type": thumb_type,
            "src_chat": src_chat,
            "src_head": src_head,
            "views": 0,
            "created_at": time.time(),
        }
    )


# ---------- views (jitni baar file/videos user ko mile) ----------
def _day(offset_days: int = 0) -> str:
    now = datetime.now(timezone.utc) + timedelta(minutes=STATS_TZ_OFFSET_MIN)
    return (now - timedelta(days=offset_days)).strftime("%Y-%m-%d")


async def record_view(token: str):
    await stats.update_one({"day": _day()}, {"$inc": {"views": 1}}, upsert=True)
    await files.update_one({"token": token}, {"$inc": {"views": 1}})


async def views_today() -> int:
    d = await stats.find_one({"day": _day()})
    return int(d.get("views", 0)) if d else 0


async def views_total() -> int:
    r = await stats.aggregate([{"$group": {"_id": None, "t": {"$sum": "$views"}}}]).to_list(length=1)
    return int(r[0]["t"]) if r else 0


async def views_last_days(n: int = 7) -> list:
    wanted = [_day(i) for i in range(n)]
    docs = await stats.find({"day": {"$in": wanted}}).to_list(length=n)
    by_day = {d["day"]: int(d.get("views", 0)) for d in docs}
    return [(d, by_day.get(d, 0)) for d in wanted]
