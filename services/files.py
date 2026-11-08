"""File registry operations shared by the Telegram owner menu."""
import asyncio
import logging
import re
import secrets

from telegram.error import TelegramError

import retry
import db
from config import BACKUP_CHANNEL, LINK_BASE_URL, POST_CHANNEL, PRIMARY_CHANNEL

log = logging.getLogger("services.files")

MAX_FILE_CAPTION = 900     # same limit the bot upload uses (channel caption adds "\n\nID: <token>")
MAX_POST_CAPTION = 1024    # Telegram caption limit


def file_link(token: str) -> str:
    return f"{LINK_BASE_URL}/f/{token}"


def channel_caption(caption: str, label: str) -> str:
    return f"{caption}\n\nID: {label}".strip()[:1024]


def all_locations(f: dict) -> list:
    """Every channel message that belongs to this file/post (files, post videos, channel thumb posts, staged thumb)."""
    locs = list(f.get("locations", []))
    for it in f.get("items", []):
        locs.extend(it.get("locations", []))
    locs.extend(f.get("channel_posts", []))   # POST_CHANNEL me gaye thumbnail posts
    if f.get("thumb_loc"):                    # web-created post: thumbnail ka storage message
        locs.append(f["thumb_loc"])
    return locs


async def delete_file_everywhere(bot, token: str) -> bool:
    f = await db.get_file(token)
    if not f:
        return False
    for loc in all_locations(f):
        try:
            await retry.with_retry(lambda: bot.delete_message(loc["channel"], loc["msg"]))
        except TelegramError as e:
            log.warning("channel msg delete failed %s: %s", loc, e)
        await asyncio.sleep(0.05)
    await db.delete_file(token)
    return True


async def delete_file_record(token: str) -> bool:
    """Sirf registry se hatao (link band). Channel copies jyon ki tyon rehti hain."""
    if not await db.get_file(token):
        return False
    await db.delete_file(token)
    return True


def describe(f: dict) -> dict:
    """JSON-safe summary. Never includes anything secret (locations are channel ids / message ids only)."""
    token = f["token"]
    is_post = bool(f.get("items")) or f.get("kind") == "post"
    return {
        "token": token,
        "kind": "post" if is_post else "file",
        "caption": f.get("caption") or "",
        "post_caption": f.get("post_caption") or "",
        "items": len(f.get("items") or []),
        "views": int(f.get("views", 0)),
        "created_at": f.get("created_at"),
        "link": file_link(token),
        "has_thumb": bool(f.get("photo")),
        "thumb_type": f.get("thumb_type"),
        "channel_posts": len(f.get("channel_posts") or []),
        "copies": len(all_locations(f)),
    }


async def list_files(page: int = 0, size: int = 10, q: str = "", kind: str = "") -> dict:
    size = max(1, min(int(size), 100))
    flt: dict = {}
    q = (q or "").strip()[:100]
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"token": rx}, {"caption": rx}, {"post_caption": rx}]
    if kind == "post":
        flt["kind"] = "post"
    elif kind == "file":
        flt["kind"] = {"$ne": "post"}
    if not flt:
        total = await db.count_files()
    else:
        total = await db.files.count_documents(flt)
    pages = max(1, -(-total // size))
    page = max(0, min(int(page), pages - 1))
    if not flt:
        rows = await db.list_files(page * size, size)
    else:
        rows = await db.files.find(flt).sort("created_at", -1).skip(page * size).limit(size).to_list(length=size)
    return {"items": [describe(r) for r in rows], "total": total, "page": page, "pages": pages, "size": size}


async def set_caption(token: str, caption: str) -> bool:
    f = await db.get_file(token)
    if not f:
        return False
    if f.get("kind") == "post" or f.get("items"):
        await db.files.update_one({"token": token}, {"$set": {"post_caption": caption[:MAX_POST_CAPTION]}})
    else:
        await db.files.update_one({"token": token}, {"$set": {"caption": caption[:MAX_FILE_CAPTION]}})
    return True


# ---------------------------------------------------------------- upload (web: raw bytes -> channels)
def _kind_for(content_type: str, filename: str, size: int) -> str:
    ct = (content_type or "").lower().split(";")[0].strip()
    name = (filename or "").lower()
    if ct == "image/gif" or name.endswith(".gif"):
        return "animation"
    if ct.startswith("image/") and size <= 10 * 1024 * 1024 and ct in ("image/jpeg", "image/png", "image/webp"):
        return "photo"
    if ct.startswith("video/") or name.endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v")):
        return "video"
    return "document"


async def send_bytes(bot, chat_id, data: bytes, filename: str, content_type: str, caption: str):
    """Upload raw bytes to a chat with the most fitting Telegram method. Returns (message, kind, file_id)."""
    kind = _kind_for(content_type, filename, len(data))
    fname = filename or "file"
    if kind == "photo":
        m = await retry.with_retry(lambda: bot.send_photo(chat_id, data, caption=caption, filename=fname))
        fid = m.photo[-1].file_id if m.photo else None
    elif kind == "animation":
        m = await retry.with_retry(lambda: bot.send_animation(chat_id, data, caption=caption, filename=fname))
        fid = (m.animation.file_id if m.animation else (m.document.file_id if m.document else None))
    elif kind == "video":
        m = await retry.with_retry(lambda: bot.send_video(chat_id, data, caption=caption, filename=fname, supports_streaming=True))
        fid = m.video.file_id if m.video else (m.document.file_id if m.document else None)
    else:
        m = await retry.with_retry(lambda: bot.send_document(chat_id, data, caption=caption, filename=fname))
        fid = m.document.file_id if m.document else None
    return m, kind, fid


async def upload_to_channels(bot, data: bytes, filename: str, content_type: str, caption: str, label: str):
    """PRIMARY (real upload) + BACKUP (server-side copy). Returns (locations, warn, kind, file_id)."""
    sent, kind, fid = await send_bytes(bot, PRIMARY_CHANNEL, data, filename, content_type, channel_caption(caption, label))
    locations = [{"channel": PRIMARY_CHANNEL, "msg": sent.message_id}]
    warn = ""
    try:
        b = await retry.with_retry(
            lambda: bot.copy_message(chat_id=BACKUP_CHANNEL, from_chat_id=PRIMARY_CHANNEL, message_id=sent.message_id)
        )
        locations.append({"channel": BACKUP_CHANNEL, "msg": b.message_id})
    except TelegramError as e:
        warn = str(e)
    return locations, warn, kind, fid


async def save_uploaded_file(bot, data: bytes, filename: str, content_type: str, caption: str) -> dict:
    caption = (caption or "")[:MAX_FILE_CAPTION]
    token = secrets.token_urlsafe(6)
    locations, warn, kind, _fid = await upload_to_channels(bot, data, filename, content_type, caption, token)
    await db.save_file(token, locations, caption)
    return {"token": token, "link": file_link(token), "warn": warn, "media": kind}


def post_channel_configured() -> bool:
    return POST_CHANNEL is not None
