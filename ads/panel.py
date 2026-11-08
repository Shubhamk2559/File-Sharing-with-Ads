"""Owner-only 'Smart Ads' section for the existing Telegram Control Center (callback prefix 'sa:').
Builders are pure/async functions returning plain data so they can be tested without python-telegram-bot."""
from __future__ import annotations

import logging

from . import engine, report, settings
from .settings import NETWORKS, NET_LABEL

log = logging.getLogger("ads.panel")


async def render_home(dashboard_url: str | None = None):
    """-> (html text, rows) where rows = [[(label, callback_data | None, url | None), ...], ...]"""
    cfg = await engine.get_config(force=True)
    try:
        text = report.summary_html(await report.build(7))
    except Exception:  # noqa: BLE001
        log.exception("ads summary failed")
        text = "🧠 <b>Smart Ads</b>\nStats could not be loaded right now."
    rows = [[(("✅ " if cfg["networks"][n]["enabled"] else "⛔ ") + NET_LABEL[n], f"sa:t:{n}", None) for n in NETWORKS],
            [(("🔘 " if cfg["mode"] == m else "") + lbl, f"sa:m:{m}", None) for m, lbl in (("auto", "Auto"), ("priority", "Priority"), ("manual", "Manual"))]]
    if cfg["mode"] == "manual":
        rows.append([(("🎯 " if cfg["manual_network"] == n else "") + NET_LABEL[n], f"sa:man:{n}", None) for n in NETWORKS])
    risky = [n for n in NETWORKS if settings.trust_for(n) == settings.CLIENT_ATTESTED]
    if risky:
        rows.append([(("⚠️ " if cfg["networks"][n]["allow_client_reward"] else "🔒 ") + NET_LABEL[n] + " client", f"sa:c:{n}", None) for n in risky])
    rows.append([(("🔁 Fallback: on" if cfg["fallback_enabled"] else "🔁 Fallback: off"), "sa:fb", None), ("🔄 Refresh", "sa:home", None)])
    if dashboard_url:
        rows.append([("📊 Open dashboard", None, dashboard_url)])
    rows.append([("🏠 Menu", "m_home", None)])
    foot = ("\n\n<i>🔒/⚠️ = allow rewards that rest on the browser callback alone (weaker). "
            "Only enable if you accept that network's limits.</i>") if risky else ""
    return text + foot, rows


async def apply(data: str) -> str | None:
    """Performs the toggle for a 'sa:...' callback. Returns an alert message on rejection, else None."""
    parts = data.split(":")
    cfg = await engine.get_config(force=True)
    patch = None
    if parts[1:2] == ["t"] and parts[2] in NETWORKS:
        patch = {"networks": {parts[2]: {"enabled": not cfg["networks"][parts[2]]["enabled"]}}}
    elif parts[1:2] == ["m"] and parts[2] in ("auto", "priority", "manual"):
        patch = {"mode": parts[2]}
        if parts[2] == "manual" and not cfg["manual_network"]:
            patch["manual_network"] = next((n for n in cfg["priority"] if cfg["networks"][n]["enabled"]), cfg["priority"][0])
    elif parts[1:2] == ["man"] and parts[2] in NETWORKS:
        patch = {"mode": "manual", "manual_network": parts[2]}
    elif parts[1:2] == ["c"] and parts[2] in NETWORKS:
        patch = {"networks": {parts[2]: {"allow_client_reward": not cfg["networks"][parts[2]]["allow_client_reward"]}}}
    elif parts[1:2] == ["fb"]:
        patch = {"fallback_enabled": not cfg["fallback_enabled"]}
    if patch is None:
        return None
    _new, errors = await engine.update_config(patch)
    return "; ".join(errors) if errors else None


def register(app, owner_id: int, dashboard_url: str | None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.error import BadRequest
    from telegram.ext import CallbackQueryHandler

    async def on_callback(update, context):
        q = update.callback_query
        if q.from_user.id != owner_id:
            await q.answer("🚫 Owner only", show_alert=True)
            return
        err = await apply(q.data) if q.data != "sa:home" else None
        await q.answer(err[:190] if err else None, show_alert=bool(err))
        text, rows = await render_home(dashboard_url)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(l, callback_data=d, url=u) if u else InlineKeyboardButton(l, callback_data=d)
                                    for (l, d, u) in row] for row in rows])
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^sa:"))
