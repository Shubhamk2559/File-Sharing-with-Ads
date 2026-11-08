import asyncio
import logging
import time
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

import db
import observability
from ads import slog
from config import AUTO_DELETE_SECONDS, LINK_BASE_URL

log = logging.getLogger("delivery")


def _after_kb(token: str) -> InlineKeyboardMarkup:
    link = f"{LINK_BASE_URL}/f/{token}"
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('🔥🎁 Grab this file!', safe='')}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 Share with friends", url=share)],
            [InlineKeyboardButton("🏠 Home", callback_data="home"), InlineKeyboardButton("🆘 Help", callback_data="help")],
        ]
    )


def _warn_text() -> str:
    secs = AUTO_DELETE_SECONDS
    when = f"{secs // 60} minute{'s' if secs // 60 != 1 else ''}" if secs % 60 == 0 else f"{secs} seconds"
    return f"⚠️ This file will be deleted in {when}. Forward it to your Saved Messages now to keep it."


def _with_warning(caption: str) -> str:
    if AUTO_DELETE_SECONDS <= 0:
        return caption
    warn = _warn_text()
    room = 1024 - len(warn) - 2          # Telegram caption limit 1024
    if caption and room > 0:
        return caption[:room] + "\n\n" + warn
    return warn


async def _send_item(bot, chat_id: int, item: dict, token: str, kb, manager) -> bool:
    """Ek video/file (item) chat me bhejo: primary channel, na chale to backup."""
    caption = _with_warning(item.get("caption") or "")
    for loc in item["locations"]:
        for _attempt in range(2):
            try:
                sent = await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=loc["channel"],
                    message_id=loc["msg"],
                    caption=caption,
                    reply_markup=kb,
                )
                if AUTO_DELETE_SECONDS > 0:
                    try:
                        await db.schedule_delete(
                            bot.id, chat_id, sent.message_id, time.time() + AUTO_DELETE_SECONDS
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("schedule_delete failed file=%s", token)
                return True
            except RetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 5))
            except TelegramError as e:
                observability.METRICS.inc("delivery.copy_failed")
                log.warning("copy failed file=%s channel=%s: %s", token, loc["channel"], type(e).__name__)
                await manager.alert_once(
                    f"{token}:{loc['channel']}",
                    f"⚠️ Delivery failed: file {token}, channel {loc['channel']}\n{type(e).__name__}: {e}\n"
                    f"Trying the backup channel.",
                )
                break
    return False


async def send_file(bot, chat_id: int, doc: dict, manager) -> bool:
    token = doc["token"]
    kb = _after_kb(token)
    items = doc.get("items") or [doc]   # post = kai videos, normal file = ek
    delivered = 0
    for i, item in enumerate(items):
        last = i == len(items) - 1
        if await _send_item(bot, chat_id, item, token, kb if last else None, manager):
            delivered += 1
        if not last:
            await asyncio.sleep(0.4)
    if delivered:
        observability.METRICS.inc("delivery.ok")
        try:
            await db.record_view(token)
        except Exception:  # noqa: BLE001
            log.exception("record_view failed file=%s", token)
    if delivered < len(items):
        observability.METRICS.inc("delivery.failed" if not delivered else "delivery.partial")
        slog.event("delivery_failed", level="error" if not delivered else "warning", file=token, items=len(items), delivered=delivered)
        if not delivered:
            await manager.alert_once(f"delivery_all_failed:{token}",
                                     f"❌ File {token}: every location failed, nothing was delivered to the user "
                                     "(primary and backup). Check the channels / bot permissions.")
    return delivered > 0
