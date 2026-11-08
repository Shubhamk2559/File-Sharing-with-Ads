"""Runs the REAL handlers.py with telegram / db / services stubbed (subprocess, like main_harness.py) and prints a JSON report.
Verifies the per-user Telegram limits (shared MongoDB limiter) on the delivery handlers."""
import asyncio
import json
import os
import sys
import types

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/tests")
for k, v in {"MAIN_BOT_TOKEN": "1:T", "OWNER_ID": "1", "MONGO_URI": "mongodb://x", "WEBHOOK_SECRET": "test-webhook-secret-0123456789", "BASE_URL": "https://x.example",
             "PRIMARY_CHANNEL": "-1001", "BACKUP_CHANNEL": "-1002", "MONETAG_POSTBACK_SECRET": "unit-test-secret-0123456789"}.items():
    os.environ.setdefault(k, v)
os.environ.update({"RL_BOT_START_USER": "3/60", "RL_BOT_MSG_USER": "4/60"})


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class Magic:
    def __init__(self, *a, **k): pass
    def __and__(self, o): return Magic()
    __or__ = __rand__ = __ror__ = __and__
    def __invert__(self): return Magic()
    def __getattr__(self, n): return Magic()
    def __call__(self, *a, **k): return Magic()


class Err(Exception): pass


mod("telegram", Update=Magic, CopyTextButton=Magic, InlineKeyboardButton=Magic, InlineKeyboardMarkup=Magic, WebAppInfo=Magic)
mod("telegram.constants", ParseMode=types.SimpleNamespace(HTML="HTML"))
mod("telegram.error", BadRequest=type("BadRequest", (Err,), {}), TelegramError=Err)
mod("telegram.ext", Application=Magic, CallbackQueryHandler=Magic, CommandHandler=Magic, ContextTypes=types.SimpleNamespace(DEFAULT_TYPE=None),
    MessageHandler=Magic, filters=Magic())

from tests.fakemongo import FakeDB, FakeSessions  # noqa: E402

fdb = FakeDB()
sess = FakeSessions(fdb)
created = []


async def create_session(**kw): created.append(kw)
async def get_file(token): return {"token": token}

mod("db", get_file=get_file, create_session=create_session, _db=fdb)
mod("monetag_store")
mod("delivery", send_file=None)
mod("services")
mod("services.stats"); mod("services.files", delete_file_everywhere=None)
mod("services.posts", post_button=None, resolve_link=None, send_thumb_post=None, store_to_channels=None, store_videos=None)
mod("ads.panel", register=lambda *a, **k: None)

from ads import ratelimit, store  # noqa: E402

store.set_database(fdb)
import handlers  # noqa: E402

replies = []


class Msg:
    async def reply_text(self, text, **k):
        replies.append(text)
        return Msg()
    async def edit_text(self, *a, **k): pass
    async def set_reaction(self, *a, **k): pass
    message_id = 5


class Chat: type, id = "private", 10


class Ctx:
    def __init__(self, args): self.args, self.bot = args, types.SimpleNamespace(username="bot")
    application = types.SimpleNamespace(bot_data={"manager": None, "bot_id": 1})


def upd(uid):
    return types.SimpleNamespace(effective_message=Msg(), effective_chat=Chat(), effective_user=types.SimpleNamespace(id=uid, first_name="n"))


class Q:
    def __init__(self, uid): self.from_user, self.data, self.message, self.answers = types.SimpleNamespace(id=uid, first_name="n"), "help", Msg(), []
    async def answer(self, text=None, **k): self.answers.append(text)
    async def edit_message_text(self, *a, **k): pass


out = {}


async def scenario():
    # /start TOKEN: 3 allowed per minute per user, then refused BEFORE any session is created
    for i in range(5):
        await handlers.deliver_start(upd(500), Ctx(["tok12345"]))
    out["sessions_created"] = len(created)
    out["limit_message"] = sum(1 for r in replies if "Too many requests" in r)
    # another user unaffected
    before = len(created)
    await handlers.deliver_start(upd(501), Ctx(["tok12345"]))
    out["other_user_ok"] = len(created) == before + 1
    # same user, menu callbacks: 4 per minute, then a quiet "slow down"
    q = [Q(600) for _ in range(6)]
    for x in q:
        await handlers.on_callback(types.SimpleNamespace(callback_query=x), Ctx([]))
    out["callback_slowdowns"] = sum(1 for x in q if x.answers and "Slow down" in (x.answers[0] or ""))
    # spam text: silent after the limit
    n0 = len(replies)
    for _ in range(8):
        await handlers.fallback_text(upd(700), Ctx([]))
    out["text_replies"] = len(replies) - n0
    # store outage: still limited (strict local), not unlimited
    fdb.down = True
    ratelimit.default()._open_until = 0
    n0 = len(created)
    for _ in range(6):
        await handlers.deliver_start(upd(800), Ctx(["tok12345"]))
    out["sessions_during_outage"] = len(created) - n0
    out["degraded"] = ratelimit.default().stats["degraded_decisions"] > 0


asyncio.run(scenario())
print(json.dumps(out, default=str))
