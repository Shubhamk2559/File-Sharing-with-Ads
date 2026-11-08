import asyncio
import html
import logging
import random
import secrets

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import monetag_store
from config import (
    MONETAG_WINDOW_DAYS,
    ADS_REQUIRED,
    BACKUP_CHANNEL,
    BASE_URL,
    CHANNEL_URL,
    LINK_BASE_URL,
    OWNER_ID,
    POST_CAPTION,
    POST_CHANNEL,
    POST_MAX_VIDEOS,
    PRIMARY_CHANNEL,
    SUPPORT_URL,
    VERIFY_REQUIRED,
)
from delivery import send_file
from services import stats as stats_svc
from services.files import delete_file_everywhere  # noqa: F401
from services.posts import post_button, resolve_link, send_thumb_post, store_to_channels, store_videos  # noqa: F401
from ads import panel as ads_panel, ratelimit as ads_ratelimit, settings as ads_settings

log = logging.getLogger("handlers")
HTML = ParseMode.HTML

owner_only = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE

# Reactions the bot is allowed to use on messages
REACTIONS = ["🔥", "⚡", "🎉", "🤩", "💯", "🏆", "😎", "👀"]

_tasks: set = set()


def bg(coro):
    """Fire-and-forget so replies never wait on side effects."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def react(msg, emoji: str | None = None):
    try:
        await msg.set_reaction(emoji or random.choice(REACTIONS))
    except TelegramError:
        pass


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


async def edit(message, text: str, kb=None):
    try:
        await message.edit_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)
    except TelegramError as e:
        log.warning("edit failed: %s", e)


# ===================== USER-FACING (delivery) HANDLERS =====================
def home_text(user) -> str:
    name = html.escape((user.first_name if user else None) or "friend")
    return (
        f"🎉🔥 <b>Welcome, {name}!</b> 🔥🎉\n\n"
        "📦 Tap any <b>permanent file link</b> and I'll deliver your file right here ⚡️🚀\n"
        "💸 100% free • 🛡 Safe • 🏎 Super fast\n\n"
        "👇✨ Pick an option below ✨👇"
    )


HOW_TEXT = (
    "📖✨ <b>How it works</b> ✨📖\n\n"
    "1️⃣ Open a permanent link 🔗\n"
    "2️⃣ Tap <b>Verify & Get Video</b> 🔐\n"
    "3️⃣ Watch one short ad 📺\n"
    "4️⃣ Your file lands in this chat 🎁🚀💎"
)
FAQ_TEXT = (
    "❓🤔 <b>FAQ</b> 🤔❓\n\n"
    "💸 <b>Is it free?</b> Yes, totally! 🎉\n"
    "📺 <b>Why an ad?</b> It keeps this service running 🛠\n"
    "😕 <b>Ad not showing?</b> Turn off AdBlock / Private DNS 🛡 and retry 🔄\n"
    "⏳ <b>File didn't arrive?</b> Wait a few seconds, then tap your link again ⚡️"
)
HELP_TEXT = (
    "🆘🛟 <b>Need help?</b> 🛟🆘\n\n"
    "1️⃣ Open your file link again 🔗\n"
    "2️⃣ Tap <b>Verify & Get Video</b> and finish the ad 📺\n"
    "3️⃣ Still stuck? Reach out below 👇💬"
)
WHY_ALERT = "📺 One short ad keeps this service free for everyone!\n🎁 After it, your file drops straight into this chat ⚡"


def kb_home() -> InlineKeyboardMarkup:
    rows = [[btn("📖 How it works", "how"), btn("❓ FAQ", "faq")], [btn("🆘 Help", "help")]]
    extra = []
    if CHANNEL_URL:
        extra.append(InlineKeyboardButton("📢 Updates", url=CHANNEL_URL))
    if SUPPORT_URL:
        extra.append(InlineKeyboardButton("💬 Support", url=SUPPORT_URL))
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(rows)


def kb_back(*more: InlineKeyboardButton) -> InlineKeyboardMarkup:
    rows = []
    if more:
        rows.append(list(more))
    rows.append([btn("🏠 Home", "home")])
    return InlineKeyboardMarkup(rows)


def kb_help() -> InlineKeyboardMarkup:
    more = [InlineKeyboardButton("💬 Contact support", url=SUPPORT_URL)] if SUPPORT_URL else []
    return kb_back(*more)


async def _bot_limited(user, category: str) -> bool:
    """Per-Telegram-user limit shared by all instances (MongoDB). True = over the limit. Never raises on store problems:
    the limiter degrades to a strict local window instead of switching itself off."""
    uid = getattr(user, "id", None)
    if uid is None:
        return False
    return not await ads_ratelimit.default().allow(category, uid)


async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    # Opening a link creates a session + messages; the plain welcome is cheaper. Both are limited per user BEFORE any DB/Telegram work.
    if await _bot_limited(user, "bot_start_user" if context.args else "bot_msg_user"):
        await msg.reply_text("⏳ Too many requests. Please wait a minute and try again.")
        return
    bg(react(msg))

    if not context.args:
        await msg.reply_text(home_text(user), parse_mode=HTML, reply_markup=kb_home())
        return

    token = context.args[0]
    if chat.type != "private":
        link = f"https://t.me/{context.bot.username}?start={token}"
        await msg.reply_text(
            "🔒💬 Please open this link in a <b>private chat</b> with me 👇",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open private chat", url=link)]]),
        )
        return

    status = await msg.reply_text("🔎⚡ <b>Checking your link…</b>", parse_mode=HTML)

    doc = await db.get_file(token)
    if not doc:
        await edit(
            status,
            "❌😵 <b>Invalid or expired link</b>\n\nDouble-check the link and try again 🔗",
            kb_back(btn("🆘 Help", "help")),
        )
        return

    manager = context.application.bot_data["manager"]
    retry = InlineKeyboardButton("🔄 Try again", url=f"https://t.me/{context.bot.username}?start={token}")

    if not VERIFY_REQUIRED:
        await edit(status, "🎁🚀 <b>Sending your file…</b>")
        ok = await send_file(context.bot, chat.id, doc, manager)
        if ok:
            await edit(status, "✅🎉 <b>Delivered!</b> Enjoy 🍿🔥", kb_back())
        else:
            await edit(
                status,
                "😕💤 <b>File temporarily unavailable</b>\n\nPlease try again in a few seconds ⏳",
                kb_back(retry),
            )
        return

    sid = secrets.token_urlsafe(12)
    await db.create_session(
        sid=sid,
        token=token,
        user_id=user.id,
        chat_id=chat.id,
        bot_id=context.application.bot_data["bot_id"],
        required=ADS_REQUIRED,
        prompt_msg=status.message_id,
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Verify & Get Video 🎬", web_app=WebAppInfo(url=f"{BASE_URL}/app?sid={sid}"))],
            [btn("❓ Why an ad?", "why"), btn("🆘 Help", "help")],
        ]
    )
    await edit(
        status,
        "🔐🎬 <b>Almost there!</b> 🎬🔐\n\n"
        "📺 Tap the button and watch one short ad ⚡️\n"
        "🎁 Your file drops right into this chat 🚀💎",
        kb,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if await _bot_limited(q.from_user, "bot_msg_user"):
        await q.answer("⏳ Slow down a little.")
        return
    if data == "why":
        await q.answer(WHY_ALERT, show_alert=True)
        return
    await q.answer()  # instant feedback, no spinner

    screens = {
        "home": (home_text(q.from_user), kb_home()),
        "how": (HOW_TEXT, kb_back(btn("❓ FAQ", "faq"))),
        "faq": (FAQ_TEXT, kb_back(btn("📖 How it works", "how"))),
        "help": (HELP_TEXT, kb_help()),
    }
    screen = screens.get(data)
    if not screen:
        return
    text, kb = screen
    m = q.message
    try:
        if getattr(m, "text", None):  # text message → edit in place (smooth)
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
        else:  # e.g. buttons under a delivered video → send a fresh message
            await m.reply_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("callback edit failed: %s", e)
    except TelegramError as e:
        log.warning("callback failed: %s", e)


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if await _bot_limited(update.effective_user, "bot_msg_user"):
        return                                    # stay silent: a reply to spam would only amplify it
    bg(react(msg, "👀"))
    await msg.reply_text(
        "🤔💭 I only understand <b>file links</b>!\n\nTap a permanent link to get your file 🔗🎁",
        parse_mode=HTML,
        reply_markup=kb_home(),
    )


def register_delivery_handlers(app: Application):
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(home|how|faq|help|why)$"))
    app.add_handler(MessageHandler((filters.TEXT | filters.COMMAND) & filters.ChatType.PRIVATE, fallback_text))


# ===================== OWNER MENU — 100% buttons =====================
# Owner koi bhi /command type nahi karta. Sab kuch inline buttons se hota hai.
# Text/file bhejna sirf Upload aur Create Post me padta hai.
PAGE_SIZE = 5


def menu_text(stats: str = "") -> str:
    return (
        "🛠⚡ <b>Control Center</b> ⚡🛠\n\n"
        f"{stats}"
        "📤 Koi bhi file/video/photo bhejo → upload + backup + permanent link 🔗🔥\n\n"
        "👇 Neeche ke buttons se sab manage karo\n🔗 Link kholne wale users ko video isi bot se milega"
    )


def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("📁 My Files", "f_list:0"), btn("📤 Upload", "m_up")],
            [btn("📝 Create Post", "m_post"), btn("📊 Stats", "m_stats")],
            [btn("💰 Monetag", "m_monetag")],
        ]
        + ([[btn("🧠 Smart Ads", "sa:home")]] if ads_settings.SMART_ADS_ENABLED else [])
    )


def kb_menu_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🏠 Menu", "m_home")]])


def kb_after(*more: InlineKeyboardButton) -> InlineKeyboardMarkup:
    rows = [list(more)] if more else []
    rows.append([btn("📁 My Files", "f_list:0"), btn("🏠 Menu", "m_home")])
    return InlineKeyboardMarkup(rows)


async def stats_line() -> str:
    n_files = await db.count_files()
    today = await db.views_today()
    return f"📁 Files: <b>{n_files}</b>   •   👁 Aaj ke views: <b>{today}</b>\n\n"


async def stats_text() -> str:
    st = await stats_svc.collect()      # same numbers the web dashboard shows
    n_files, total = st["files"], st["views_total"]
    days = [(d["day"], d["views"]) for d in st["last_days"]]
    lines = "\n".join(f"{'👉' if i == 0 else '▫️'} {d}: <b>{v}</b>" for i, (d, v) in enumerate(days))
    return (
        "📊 <b>Stats</b>\n\n"
        f"👁 <b>Aaj ke views: {days[0][1]}</b>\n"
        f"📈 Total views: <b>{total}</b>\n"
        f"📁 Files / posts: <b>{n_files}</b>\n\n"
        f"🗓 <b>Last 7 days</b>\n{lines}\n\n"
        "<i>View = jab user ad dekh kar file/videos receive kare.</i>"
    )


# ---------- files ----------
async def files_screen(page: int):
    total = await db.count_files()
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    items = await db.list_files(page * PAGE_SIZE, PAGE_SIZE)
    if not items:
        return "📁💤 <b>Abhi koi file nahi hai</b>\n\nKoi file/video bhejo, wo yahan aa jayegi.", kb_menu_only()
    rows = []
    for f in items:
        cap = (f.get("caption") or "").strip().replace("\n", " ")[:28] or "(no caption)"
        rows.append([btn(f"📄 {cap} • {f['token']}", f"f:{f['token']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"f_list:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"f_list:{page + 1}"))
    rows.append(nav)
    rows.append([btn("🏠 Menu", "m_home")])
    return f"📁 <b>My Files</b> ({total})\n\nFile par tap karo → link / delete 👇", InlineKeyboardMarkup(rows)


async def file_screen(token: str):
    f = await db.get_file(token)
    if not f:
        return "❌🔍 File nahi mili (shayad delete ho chuki).", InlineKeyboardMarkup([[btn("📁 Files", "f_list:0")]])
    link = f"{LINK_BASE_URL}/f/{token}"
    cap = html.escape((f.get("caption") or "").strip()[:200]) or "<i>(no caption)</i>"
    views = int(f.get("views", 0))
    text = (
        f"📄 <b>{'Post' if f.get('items') else 'File'}</b> <code>{token}</code>\n\n"
        f"📝 {cap}\n👁 Views: <b>{views}</b>\n\n🔗 <code>{html.escape(link)}</code>"
    )
    rows = [[InlineKeyboardButton("🔗 Open", url=link), InlineKeyboardButton("📋 Copy", copy_text=CopyTextButton(text=link))]]
    if f.get("items") and POST_CHANNEL:
        rows.append([btn("📢 Post to channel", f"pc:{token}")])
    rows.append([btn("🗑 Delete", f"fd:{token}")])
    rows.append([btn("⬅️ Files", "f_list:0"), btn("🏠 Menu", "m_home")])
    return text, InlineKeyboardMarkup(rows)


# ---------- screens / navigation ----------
async def show(q, text: str, kb: InlineKeyboardMarkup):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("owner callback failed: %s", e)
    except TelegramError as e:
        log.warning("owner callback failed: %s", e)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(POST_KEY, None)
    await update.effective_message.reply_text(menu_text(await stats_line()), parse_mode=HTML, reply_markup=kb_menu())


async def owner_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bg(react(update.effective_message, "🔥"))
    await send_menu(update, context)


async def start_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ek hi bot: link (/start TOKEN) = file delivery sabke liye; bina token = owner ko panel, baaki ko welcome."""
    user = update.effective_user
    if context.args or not user or user.id != OWNER_ID:
        await deliver_start(update, context)
    else:
        await owner_start(update, context)


async def on_owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("🚫 Owner only", show_alert=True)
        return
    data = q.data
    manager = context.application.bot_data["manager"]

    if data == "noop":
        await q.answer()
        return
    if data not in ("m_post", "p_done"):
        context.user_data.pop(POST_KEY, None)   # koi bhi aur button = post banana cancel

    if data == "m_home":
        await q.answer()
        await show(q, menu_text(await stats_line()), kb_menu())
    elif data == "m_stats":
        await q.answer()
        await show(q, await stats_text(), kb_menu_only())
    elif data == "m_monetag":
        await q.answer()
        try:
            text = monetag_store.format_report_html(await monetag_store.build_report(MONETAG_WINDOW_DAYS))
        except Exception:  # noqa: BLE001
            log.exception("monetag report failed")
            text = "💰 Monetag stats abhi load nahi ho paye. Thodi der baad try karo."
        await show(q, text, kb_menu_only())
    elif data == "m_up":
        await q.answer()
        await show(
            q,
            "📤💾 <b>Upload a file</b>\n\n"
            "Bas koi bhi video, photo ya document yahan bhej do 🎬📸📄\n"
            "Main dono channels me save karke permanent link de dunga 🔗🔥",
            kb_menu_only(),
        )
    elif data == "m_post":
        await q.answer()
        post = {"photo": None, "videos": [], "caption": None, "panel": (q.message.chat_id, q.message.message_id)}
        context.user_data[POST_KEY] = post
        await show(q, post_panel_text(post), kb_post())
    elif data == "p_done":
        await create_post(q, context)
    elif data.startswith("pc:"):
        f = await db.get_file(data[3:])
        if not f or not f.get("photo"):
            await q.answer("❌ Post nahi mila", show_alert=True)
        elif not POST_CHANNEL:
            await q.answer("POST_CHANNEL env set nahi hai", show_alert=True)
        else:
            try:
                sent_msg = await send_thumb_post(context.bot, POST_CHANNEL, f, manager)
                await db.add_channel_post(f["token"], POST_CHANNEL, sent_msg.message_id)
                await q.answer("📢 Channel me post ho gaya ✅", show_alert=True)
            except TelegramError as e:
                await q.answer(f"❌ {str(e)[:150]}", show_alert=True)
    elif data.startswith("f_list:"):
        await q.answer()
        await show(q, *await files_screen(int(data.split(":")[1])))
    elif data.startswith("f:"):
        await q.answer()
        await show(q, *await file_screen(data[2:]))
    elif data.startswith("fd:"):  # delete file -> confirm
        token = data[3:]
        await q.answer()
        await show(
            q,
            f"🗑⚠️ File <code>{token}</code> delete karni hai?\n\n"
            "Channels (primary + backup) se bhi hat jayegi aur link kaam karna band kar dega.",
            InlineKeyboardMarkup([[btn("✅ Haan, delete", f"fdc:{token}"), btn("❌ Nahi", f"f:{token}")]]),
        )
    elif data.startswith("fdc:"):
        await delete_file_everywhere(context.bot, data[4:])
        await q.answer("🗑 Deleted")
        await show(q, *await files_screen(0))
    else:
        await q.answer()


# ---------- text input ----------
async def owner_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if context.user_data.get(POST_KEY) is not None:
        bg(react(msg, "🤔"))
        await msg.reply_text("📝 Post banane ke liye <b>image / video</b> bhejo, phir ✅ Create Post dabao.", parse_mode=HTML)
        return
    bg(react(msg, "🤔"))
    await send_menu(update, context)


async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if context.user_data.get(POST_KEY) is not None:   # Create Post mode: bas collect karo
        await post_collect(update, context)
        return
    status = await msg.reply_text("⏳💾 <b>Uploading & backing up…</b>", parse_mode=HTML)

    token = secrets.token_urlsafe(6)
    original_caption = (msg.caption or "")[:900]
    channel_caption = f"{original_caption}\n\nID: {token}".strip()

    try:
        a = await context.bot.copy_message(
            chat_id=PRIMARY_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
            caption=channel_caption,
        )
    except TelegramError as e:
        bg(react(msg, "😢"))
        await edit(
            status,
            f"❌😢 <b>Primary channel upload failed</b>\n<code>{html.escape(str(e))}</code>\n\n👑 Make the bot an admin of the channel.",
            kb_after(),
        )
        return

    locations = [{"channel": PRIMARY_CHANNEL, "msg": a.message_id}]
    warn = ""
    try:
        b = await context.bot.copy_message(
            chat_id=BACKUP_CHANNEL,
            from_chat_id=PRIMARY_CHANNEL,
            message_id=a.message_id,
        )
        locations.append({"channel": BACKUP_CHANNEL, "msg": b.message_id})
    except TelegramError as e:
        warn = f"\n\n⚠️ Backup copy failed: <code>{html.escape(str(e))}</code>"

    await db.save_file(token, locations, original_caption)
    link = f"{LINK_BASE_URL}/f/{token}"
    bg(react(msg, "🔥"))
    await edit(
        status,
        f"✅🎉 <b>Saved & backed up!</b> 🔥💾\n\n🔗 <code>{html.escape(link)}</code>{warn}",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Open", url=link), InlineKeyboardButton("📋 Copy", copy_text=CopyTextButton(text=link))],
                [btn("📄 Manage file", f"f:{token}"), btn("🏠 Menu", "m_home")],
            ]
        ),
    )


# ---------- Create Post: 1 image + kai videos -> ek link -> ek button ----------
POST_KEY = "post"   # context.user_data[POST_KEY] = {"photo", "videos", "caption", "panel"}


def post_panel_text(post: dict) -> str:
    return (
        "📝🎬 <b>Create Post</b>\n\n"
        f"📸 Image: {'✅' if post.get('photo') else '❌ (bhejo)'}\n"
        f"🎬 Videos: <b>{len(post['videos'])}</b> / {POST_MAX_VIDEOS}\n"
        f"✍️ Caption: {'custom ✅' if post.get('caption') else 'default (POST_CAPTION)'}\n\n"
        "1 image + video(s) bhejo (ek saath ya ek-ek karke). Image ke saath caption likhoge to wahi caption banega.\n"
        "Phir ✅ Create Post dabao 👇"
    )


def kb_post() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("✅ Create Post", "p_done")], [btn("❌ Cancel", "m_home")]])


async def refresh_post_panel(context: ContextTypes.DEFAULT_TYPE, post: dict):
    panel = post.get("panel")
    if not panel:
        return
    try:
        await context.bot.edit_message_text(
            post_panel_text(post), chat_id=panel[0], message_id=panel[1], parse_mode=HTML, reply_markup=kb_post()
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("post panel edit failed: %s", e)
    except TelegramError as e:
        log.warning("post panel edit failed: %s", e)


async def post_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    post = context.user_data[POST_KEY]
    if msg.photo:
        post["photo"] = msg.photo[-1].file_id
        if msg.caption:
            post["caption"] = msg.caption[:1000]
    elif msg.video or msg.animation or msg.document:
        if len(post["videos"]) >= POST_MAX_VIDEOS:
            bg(react(msg, "😢"))
            return
        post["videos"].append({"msg": msg.message_id, "caption": (msg.caption or "")[:900]})
    else:
        bg(react(msg, "🤔"))
        return
    bg(react(msg, "👍"))
    await refresh_post_panel(context, post)


async def create_post(q, context: ContextTypes.DEFAULT_TYPE):
    post = context.user_data.get(POST_KEY)
    if not post:
        await q.answer("Pehle 📝 Create Post dabao", show_alert=True)
        return
    if not post.get("photo") or not post["videos"]:
        await q.answer("Image aur kam se kam 1 video bhejo", show_alert=True)
        await refresh_post_panel(context, post)
        return
    await q.answer()
    context.user_data.pop(POST_KEY, None)
    chat_id = q.message.chat_id
    n = len(post["videos"])
    await show(q, f"⏳💾 <b>Uploading & backing up {n} video{'s' if n != 1 else ''}…</b>", None)

    gtoken = secrets.token_urlsafe(6)
    try:
        items, warns = await store_videos(context.bot, chat_id, post["videos"], gtoken)   # rolls back on failure
    except TelegramError as e:
        post["panel"] = (chat_id, q.message.message_id)
        context.user_data[POST_KEY] = post
        await show(
            q,
            f"❌😢 <b>Upload failed</b>\n<code>{html.escape(str(e))}</code>\n\n"
            "👑 Bot ko dono channels ka admin banao, phir ✅ Create Post dobara dabao.\n\n" + post_panel_text(post),
            kb_post(),
        )
        return

    caption = post.get("caption") or POST_CAPTION
    link = f"{LINK_BASE_URL}/f/{gtoken}"
    await db.save_post(gtoken, items, caption, post["photo"])

    try:
        await q.message.delete()
    except TelegramError:
        pass
    try:  # ye wahi post hai jo channel me jayega
        await context.bot.send_photo(chat_id, post["photo"], caption=caption[:1024],
            reply_markup=post_button(await resolve_link(context.application.bot_data["manager"], gtoken)),
        )
    except TelegramError as e:
        await context.bot.send_message(chat_id, f"⚠️ Preview nahi ban paya: {html.escape(str(e))}", parse_mode=HTML)

    note = f"\n\n⚠️ {warns} video ka backup copy fail hua" if warns else ""
    rows = []
    if POST_CHANNEL:
        rows.append([btn("📢 Post to channel", f"pc:{gtoken}")])
    rows.append(
        [InlineKeyboardButton("🔗 Open", url=link), InlineKeyboardButton("📋 Copy", copy_text=CopyTextButton(text=link))]
    )
    rows.append([btn("📝 New post", "m_post"), btn("📄 Manage", f"f:{gtoken}")])
    rows.append([btn("🏠 Menu", "m_home")])
    await context.bot.send_message(
        chat_id,
        f"✅🎉 <b>Post ready!</b> 🔥\n\n🎬 {n} video{'s' if n != 1 else ''} • ek link, ek ad\n"
        f"🔗 <code>{html.escape(link)}</code>\n\n👆 Upar wala post forward karo"
        + (" ya 📢 se channel me daalo." if POST_CHANNEL else ".")
        + note,
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def register_main_handlers(app: Application):
    # /start Telegram ka built-in entry hai (link kholne par khud lagta hai). Baaki sab buttons.
    app.add_handler(CommandHandler("start", start_router))
    # --- Smart Ads (owner only, checked inside) ---
    if ads_settings.SMART_ADS_ENABLED:
        ads_panel.register(app, OWNER_ID, f"{BASE_URL}/admin/ads" if ads_settings.ADS_ADMIN_TOKEN else None)
    # --- owner panel (sirf owner) ---
    app.add_handler(
        CallbackQueryHandler(on_owner_callback, pattern=r"^(m_|f_list:|f:|fd:|fdc:|pc:|p_done$|noop$)")
    )
    app.add_handler(MessageHandler(owner_only & filters.ATTACHMENT & ~filters.COMMAND, upload))
    app.add_handler(MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, owner_text))
    app.add_handler(MessageHandler(owner_only & filters.COMMAND, owner_start))
    # --- delivery (sab users): Home / How / FAQ / Help / Why + random text ---
    register_delivery_handlers(app)
