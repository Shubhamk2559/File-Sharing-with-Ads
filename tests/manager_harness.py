"""Runs the REAL manager.py / health.py / observability.py with telegram, motor, handlers and the optimiser loops stubbed (subprocess, like
main_harness.py) and prints one JSON report. Covers startup, shutdown, migration failure, Telegram failures, duplicate start, task lifecycle
and owner-alert de-duplication. Not a test module itself."""
import asyncio
import dataclasses
import json
import os
import sys
import time
import types

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/tests")
for k, v in {"MAIN_BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "OWNER_ID": "42", "MONGO_URI": "mongodb://user:dbpass-9876@h/x",
             "WEBHOOK_SECRET": "test-webhook-secret-0123456789", "BASE_URL": "https://example.test", "PRIMARY_CHANNEL": "-1001",
             "BACKUP_CHANNEL": "-1002", "MONETAG_POSTBACK_SECRET": "s3cr3t-Postback_0123456789abcdef"}.items():
    os.environ[k] = v


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class TelegramError(Exception): pass
class NetworkError(TelegramError): pass
class BadRequest(TelegramError): pass
class Forbidden(TelegramError): pass
class InvalidToken(TelegramError): pass
class RetryAfter(TelegramError):
    def __init__(self, n): super().__init__("flood"); self.retry_after = n


CALLS: dict = {}
PLAN: dict = {}                      # per-scenario behaviour switches


def count(name): CALLS[name] = CALLS.get(name, 0) + 1


class FakeBot:
    id = 7
    username = "bot"
    token = os.environ["MAIN_BOT_TOKEN"]

    async def delete_my_commands(self): count("delete_my_commands")

    async def set_webhook(self, url, secret_token, allowed_updates):
        count("set_webhook")
        CALLS["webhook_args"] = [url, secret_token == __import__("hashlib").sha256(f"{os.environ['WEBHOOK_SECRET']}:main".encode()).hexdigest(), allowed_updates]
        exc = PLAN.get("set_webhook_exc")
        if exc is not None:
            raise exc

    async def get_webhook_info(self):
        count("get_webhook_info")
        return PLAN["webhook_info"]

    async def send_message(self, chat_id, text):
        CALLS.setdefault("sent", []).append([chat_id, text])


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}
        self.update_queue = asyncio.Queue()
        self.order = CALLS.setdefault("app_order", [])
        self.error_handlers = []

    def add_error_handler(self, h): self.error_handlers.append(h)

    async def initialize(self):
        count("initialize")
        seq = PLAN.get("initialize_excs")
        if seq:
            raise seq.pop(0)

    async def start(self): self.order.append("app.start")
    async def stop(self): self.order.append("app.stop")
    async def shutdown(self): self.order.append("app.shutdown")


class Builder:
    def token(self, t): return self
    def updater(self, u): return self
    def concurrent_updates(self, b): return self
    def build(self):
        count("build")
        return FakeApp()


class Application:
    builder = staticmethod(lambda: Builder())


class StandaloneBot:
    def __init__(self, token): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def send_message(self, chat_id, text):
        CALLS.setdefault("standalone_sent", []).append([chat_id, text])


mod("telegram", Bot=StandaloneBot, Update=object)
mod("telegram.error", BadRequest=BadRequest, Forbidden=Forbidden, RetryAfter=RetryAfter, TelegramError=TelegramError)
mod("telegram.ext", Application=Application)

from tests.fakemongo import FakeDB  # noqa: E402

fdb = FakeDB()


async def _ping():
    if fdb.down:
        raise type("ConnectionFailure", (Exception,), {})("down")


def _close(): CALLS.setdefault("app_order", []).append("db.close")


async def _due(now, limit=100): return []


mod("db", _db=fdb, ping=_ping, close=_close, due_deletes=_due, remove_delete=None)
mod("handlers", register_main_handlers=lambda app: count("register_handlers"))


async def _noop_loop(): count("noop_loop_started")


mod("monetag_engine", optimizer_loop=_noop_loop)

import health  # noqa: E402
import manager as mgr  # noqa: E402
import migrator  # noqa: E402
import observability as obs  # noqa: E402
from ads import engine as ads_engine  # noqa: E402

ads_engine.loop = _noop_loop
orig_retry = health.retry_until


async def _fast_sleep(dt): await asyncio.sleep(0)


def _retry(fn, **kw):
    kw["sleep"] = _fast_sleep
    return orig_retry(fn, **kw)


health.retry_until = _retry
mgr.RT = dataclasses.replace(mgr.RT, webhook_register_timeout=0.2, startup_timeout=0.5, shutdown_timeout=3.0)
obs.bootstrap(os.environ, mgr.RT)


def fresh():
    CALLS.clear()
    PLAN.clear()
    obs.METRICS.reset()
    lc = health.Lifecycle()
    alerts = obs.AlertManager(cooldown=3600, max_per_hour=10)
    exits = []
    lc.set_exit_callback(exits.append)
    m = mgr.BotManager(lifecycle=lc, alerts=alerts, tasks=health.TaskSupervisor(sleep=_fast_sleep))

    async def standalone(text): CALLS.setdefault("standalone_sent", []).append(text)
    m._standalone_send = standalone
    return m, lc, exits


out = {}


async def ok_migrations():
    return None


async def main_scenarios():
    mgr.run_startup_migrations = ok_migrations

    # ---------- 1. normal startup, readiness, graceful shutdown
    m, lc, exits = fresh()
    await m.start()
    out["state_right_after_start"] = lc.state.value               # start() is non-blocking
    out["ready"] = await m.wait_ready(5)
    out["state"] = lc.state.value
    out["webhook"] = [CALLS.get("set_webhook"), CALLS.get("webhook_args")]
    out["tasks"] = sorted(m.tasks.snapshot())
    out["delete_my_commands"] = CALLS.get("delete_my_commands")
    out["alert_sender_ready"] = m.alerts._sender is not None
    await asyncio.sleep(0.05)
    await m.stop()
    out["stopped_state"] = lc.state.value
    out["shutdown_order"] = CALLS["app_order"]
    out["tasks_after_stop"] = {n: v["status"] for n, v in m.tasks.snapshot().items()}
    await m.stop()                                                  # idempotent
    out["second_stop_noop"] = CALLS["app_order"].count("db.close") == 1
    out["exits_normal"] = exits

    # ---------- 2. duplicate start: nothing is started twice
    m, lc, exits = fresh()
    await m.start()
    await m.start()
    await m.wait_ready(5)
    await m.start()
    out["dup_set_webhook"] = CALLS.get("set_webhook")
    out["dup_build"] = CALLS.get("build")
    out["dup_noop_loops"] = CALLS.get("noop_loop_started")          # cleanup returns at once when disabled? counted loops: monetag + ads only
    started = m.tasks.start("cleanup", m.cleanup_loop)              # direct duplicate of a RUNNING task is refused
    out["dup_task_refused"] = started is False or not m.tasks.running("cleanup")
    ok_task = m.tasks.running("db_monitor")
    out["dup_running_task_refused"] = (m.tasks.start("db_monitor", m._db_monitor) is False) if ok_task else None
    await m.stop()

    # ---------- 3. failed migration: nothing is served, owner alerted once, process exits non-zero
    m, lc, exits = fresh()

    async def bad_migration():
        raise migrator.MigrationError("migration 0003 failed: index conflict mongodb://user:dbpass-9876@h/x")
    mgr.run_startup_migrations = bad_migration
    await m.start()
    out["mig_ready"] = await m.wait_ready(5)
    out["mig_state"] = [lc.state.value, lc.phase, lc.failure["phase"], lc.failure["error"]]
    out["mig_app_built"] = CALLS.get("build", 0)
    out["mig_webhook_set"] = CALLS.get("set_webhook", 0)
    out["mig_exit"] = exits
    out["mig_alert"] = CALLS.get("standalone_sent")
    out["mig_tasks"] = sorted(m.tasks.snapshot())
    mgr.run_startup_migrations = ok_migrations
    await m.stop()
    out["mig_stop_after_fail"] = lc.state.value

    # ---------- 4. invalid bot token: permanent, no retry, no alert attempt
    m, lc, exits = fresh()
    PLAN["initialize_excs"] = [InvalidToken("bad token")]
    await m.start()
    out["tok_ready"] = await m.wait_ready(5)
    out["tok"] = [lc.state.value, lc.failure["phase"], CALLS.get("initialize"), exits, CALLS.get("standalone_sent")]
    await m.stop()

    # ---------- 5. transient Telegram error at init: retried, then ready
    m, lc, exits = fresh()
    PLAN["initialize_excs"] = [NetworkError("reset"), NetworkError("reset")]
    await m.start()
    out["net_ready"] = await m.wait_ready(5)
    out["net"] = [CALLS.get("initialize"), CALLS.get("build"), exits]
    await m.stop()

    # ---------- 6. Telegram unreachable for the whole STARTUP_TIMEOUT: fatal
    m, lc, exits = fresh()
    PLAN["initialize_excs"] = [NetworkError("down")] * 100000
    await m.start()
    out["down_ready"] = await m.wait_ready(5)
    out["down"] = [lc.state.value, lc.failure["phase"], exits]
    await m.stop()

    # ---------- 7. set_webhook transient failure: still serves, monitor keeps trying, alert only after repeated failures
    m, lc, exits = fresh()
    PLAN["set_webhook_exc"] = NetworkError("tg down")
    await m.start()
    out["wh_ready"] = await m.wait_ready(5)
    out["wh_state"] = [lc.state.value, m.webhook_dep.status, m.webhook_dep.consecutive_failures]
    sent_before = len(CALLS.get("sent", []))
    for _ in range(4):
        await m._webhook_failed("set_webhook", NetworkError("tg down"))
    out["wh_alerts"] = [m for m in CALLS.get("sent", [])[sent_before:]]
    out["wh_alert_count"] = len(CALLS.get("sent", [])) - sent_before
    PLAN["set_webhook_exc"] = None
    await m.stop()

    # ---------- 8. bad webhook URL (BadRequest) is a configuration error: fatal
    m, lc, exits = fresh()
    PLAN["set_webhook_exc"] = BadRequest("Bad webhook: HTTPS url must be provided")
    await m.start()
    out["bad_url_ready"] = await m.wait_ready(5)
    out["bad_url"] = [lc.state.value, lc.failure["phase"], exits]
    await m.stop()

    # ---------- 9. webhook monitor: Telegram reports delivery errors -> ONE alert, secrets redacted; URL mismatch is re-registered
    m, lc, exits = fresh()
    await m.start()
    await m.wait_ready(5)
    now = time.time()
    info = types.SimpleNamespace(url="https://example.test/webhook", pending_update_count=12, last_error_date=now - 5,
                                 last_error_message="Wrong response from the webhook: 503 Service Unavailable token=SECRETVALUE123456")
    PLAN["webhook_info"] = info
    sent_before = len(CALLS.get("sent", []))
    await m._check_webhook()
    await m._check_webhook()                                          # same Telegram error again: no second alert
    out["wh_err_alerts"] = CALLS.get("sent", [])[sent_before:]
    out["wh_info"] = m.webhook_info
    info.url = "https://old.example/webhook"                          # someone else's URL registered: we re-register ours
    before = CALLS.get("set_webhook")
    await m._check_webhook()
    out["wh_reregistered"] = CALLS.get("set_webhook") - before
    await m.stop()

    # ---------- 10. owner alert de-duplication / caps / queue / sender failure
    clock = [1000.0]
    a = obs.AlertManager(cooldown=100, max_per_hour=3, clock=lambda: clock[0])
    sent = []

    async def snd(t): sent.append(t)
    a.set_sender(snd)
    r = [await a.alert("k", "first"), await a.alert("k", "again"), await a.alert("k", "again2")]
    clock[0] += 101
    r.append(await a.alert("k", "after cooldown"))
    out["alert_dedup"] = [r, [t.split(": ", 1)[1][:40] for t in sent]]
    out["alert_suppressed_note"] = "suppressed" in sent[-1]
    for i in range(6):
        await a.alert(f"other{i}", "x")
    out["alert_cap"] = [len(sent), a.stats["capped"]]

    q = obs.AlertManager(cooldown=100)
    await q.alert("early", "before the bot exists")
    got = []

    async def late(t): got.append(t)
    q.set_sender(late)
    out["alert_queue"] = [await q.flush(), len(got)]

    async def broken(t): raise RuntimeError("telegram down")
    b = obs.AlertManager(cooldown=100, clock=lambda: clock[0])
    b.set_sender(broken)
    r1 = await b.alert("bk", "x")
    out["alert_sender_failure"] = [r1, b.stats["failed"]]

    # ---------- 11. supervised task lifecycle: crash -> restart with backoff, finished tasks are not restarted, crash alert once
    sup_sleeps = []

    async def rec_sleep(dt): sup_sleeps.append(dt)
    crashes = []

    async def on_crash(name, n, e): crashes.append([name, n])
    sup = health.TaskSupervisor(sleep=rec_sleep, on_crash=on_crash, base_backoff=1, max_backoff=8)
    runs = {"n": 0}

    async def flaky():
        runs["n"] += 1
        if runs["n"] < 6:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    sup.start("flaky", flaky)
    for _ in range(50):
        await asyncio.sleep(0)
        if runs["n"] >= 6:
            break
    out["task_backoff"] = [runs["n"], sup_sleeps[:5], crashes, sup.snapshot()["flaky"]["crashes"]]

    async def once(): runs["done"] = True
    sup.start("once", once)
    await asyncio.sleep(0.01)
    out["task_finished"] = [sup.snapshot()["once"]["status"], sup.start("once", once)]
    stuck = await sup.stop(2)
    out["task_stop"] = [stuck, sup.running("flaky"), sup.start("late", once)]


asyncio.run(main_scenarios())
print(json.dumps(out, default=str))
