"""Shared post helpers used by BOTH the Telegram owner menu (handlers.py).

Moved here unchanged from handlers.py so there is a single source of truth:
  post_button / resolve_link / send_thumb_post  - how a "thumbnail + button" post is built and sent
  store_to_channels                             - copy one message into PRIMARY + BACKUP channel
  store_videos / rollback_items                 - store many videos for one post, undo on failure
"""
import logging
import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import retry
from config import (
    BACKUP_CHANNEL,
    DIRECT_MINIAPP,
    LINK_BASE_URL,
    MINIAPP_SHORT_NAME,
    POST_BUTTON_TEXT,
    POST_CAPTION,
    POST_DIRECT_LINK,
    PRIMARY_CHANNEL,
)

log = logging.getLogger("services.posts")


def post_button(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(POST_BUTTON_TEXT, url=link)]])


async def resolve_link(manager, token: str) -> str:
    """Post button ka link. POST_DIRECT_LINK=True: seedha t.me link (confirm box nahi), random active bot."""
    if POST_DIRECT_LINK and manager:
        names = await manager.active_usernames()
        if names:
            u = random.choice(names)
            if DIRECT_MINIAPP:
                return f"https://t.me/{u}/{MINIAPP_SHORT_NAME}?startapp={token}" if MINIAPP_SHORT_NAME else f"https://t.me/{u}?startapp={token}"
            return f"https://t.me/{u}?start={token}"
    return f"{LINK_BASE_URL}/f/{token}"


async def send_thumb_post(bot, chat_id, f: dict, manager=None):
    """Thumbnail (photo / video / gif) + caption + button. Channel post aur preview dono yahi use karte hain."""
    link = await resolve_link(manager, f["token"])
    caption = (f.get("post_caption") or POST_CAPTION)[:1024]
    kb = post_button(link)
    fid, kind = f["photo"], f.get("thumb_type") or "photo"
    if kind == "video":
        return await retry.with_retry(lambda: bot.send_video(chat_id, fid, caption=caption, reply_markup=kb))
    if kind == "animation":
        return await retry.with_retry(lambda: bot.send_animation(chat_id, fid, caption=caption, reply_markup=kb))
    return await retry.with_retry(lambda: bot.send_photo(chat_id, fid, caption=caption, reply_markup=kb))


async def store_to_channels(bot, from_chat: int, msg_id: int, caption: str, label: str):
    """Primary + backup channel me save. Primary fail = TelegramError. Return (locations, warn)."""
    a = await retry.with_retry(
        lambda: bot.copy_message(
            chat_id=PRIMARY_CHANNEL,
            from_chat_id=from_chat,
            message_id=msg_id,
            caption=f"{caption}\n\nID: {label}".strip(),
        )
    )
    locations = [{"channel": PRIMARY_CHANNEL, "msg": a.message_id}]
    warn = ""
    try:
        b = await retry.with_retry(
            lambda: bot.copy_message(chat_id=BACKUP_CHANNEL, from_chat_id=PRIMARY_CHANNEL, message_id=a.message_id)
        )
        locations.append({"channel": BACKUP_CHANNEL, "msg": b.message_id})
    except TelegramError as e:
        warn = str(e)
    return locations, warn


async def rollback_items(bot, items: list) -> None:
    """Adhura upload wapas hata do (channel messages delete)."""
    for it in items:
        for loc in it["locations"]:
            try:
                await bot.delete_message(loc["channel"], loc["msg"])
            except TelegramError:
                pass


async def store_videos(bot, from_chat: int, videos: list, gtoken: str):
    """videos = [{"msg": message_id, "caption": str}, ...] -> (items, warns). TelegramError par rollback + re-raise."""
    items: list = []
    warns = 0
    try:
        for i, v in enumerate(videos, 1):
            locs, warn = await store_to_channels(bot, from_chat, v["msg"], v["caption"], f"{gtoken} #{i}")
            items.append({"locations": locs, "caption": v["caption"]})
            warns += 1 if warn else 0
    except TelegramError:
        await rollback_items(bot, items)
        raise
    return items, warns
