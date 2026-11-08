"""Batch 3: Koyeb reliability + monitoring. Pure-module unit tests plus subprocess harnesses that execute the REAL main.py / manager.py
(stubbed Telegram, FastAPI and MongoDB, see main_harness.py / manager_harness.py). No live Telegram, MongoDB Atlas, Monetag or Koyeb.
Also pins the Batch 1 security and Batch 2 rate-limiter / migration guarantees against regressions from this batch."""
import asyncio
import io
import json
import logging
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

SECRET = "s3cr3t-Postback_0123456789abcdef"
GOOD_ENV = {"MAIN_BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "OWNER_ID": "42", "MONGO_URI": "mongodb://user:dbpass-9876@h/x",
            "WEBHOOK_SECRET": "test-webhook-secret-0123456789", "BASE_URL": "https://example.test", "PRIMARY_CHANNEL": "-1001",
            "BACKUP_CHANNEL": "-1002", "MONETAG_POSTBACK_SECRET": SECRET}
BASE = {"PATH": os.environ.get("PATH", ""), **GOOD_ENV, "TADS_WIDGET_ID": "13070"}

import envcheck  # noqa: E402
import health  # noqa: E402
import observability as obs  # noqa: E402
from tests.fakemongo import ConnectionFailure, FakeDB  # noqa: E402


def run_harness(name, extra=None):
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tests" / name), str(ROOT)], env={**BASE, **(extra or {})},
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1]), r


# =================================================================== environment validation
class EnvTests(unittest.TestCase):
    def test_all_missing_variables_are_reported_at_once_without_values(self):
        with self.assertRaises(envcheck.EnvError) as cm:
            envcheck.validate_required({"MONGO_URI": "mongodb://u:pw-secret-1@h/x"})
        msg = str(cm.exception)
        for n in ("MAIN_BOT_TOKEN", "OWNER_ID", "WEBHOOK_SECRET", "BASE_URL", "PRIMARY_CHANNEL", "BACKUP_CHANNEL"):
            self.assertIn(n, msg)
        self.assertNotIn("pw-secret-1", msg)

    def test_production_requires_https_and_a_long_webhook_secret(self):
        for patch, needle in (({"BASE_URL": "http://x.test"}, "BASE_URL"), ({"WEBHOOK_SECRET": "short"}, "WEBHOOK_SECRET"),
                              ({"LINK_BASE_URL": "http://x"}, "LINK_BASE_URL"), ({"OWNER_ID": "abc"}, "OWNER_ID")):
            with self.assertRaises(envcheck.EnvError) as cm:
                envcheck.validate_required({**GOOD_ENV, **patch})
            self.assertIn(needle, str(cm.exception))
        envcheck.validate_required(GOOD_ENV)
        envcheck.validate_required({**GOOD_ENV, "APP_ENV": "development", "BASE_URL": "http://localhost:8000", "WEBHOOK_SECRET": "w"})

    def test_runtime_defaults_and_invalid_values(self):
        rt = envcheck.parse_runtime({})
        self.assertEqual((rt.log_format, rt.log_pseudonymize, rt.alerts_enabled, rt.alert_max_per_hour), ("json", True, True, 10))
        self.assertLess(rt.http_graceful_timeout + rt.shutdown_timeout, 30)                    # fits a 30 s termination window
        for bad in ({"LOG_LEVEL": "LOUD"}, {"LOG_FORMAT": "xml"}, {"ALERT_MAX_PER_HOUR": "0"}, {"DB_HEALTH_INTERVAL": "x"}, {"ALERTS_ENABLED": "maybe"}):
            with self.assertRaises(envcheck.EnvError, msg=str(bad)):
                envcheck.parse_runtime(bad)

    def test_port(self):
        self.assertEqual(envcheck.parse_port({}), 8000)
        self.assertEqual(envcheck.parse_port({"PORT": "8123"}), 8123)
        for bad in ("abc", "0", "70000"):
            with self.assertRaises(envcheck.EnvError):
                envcheck.parse_port({"PORT": bad})

    def test_serve_exits_2_with_names_only_when_env_is_broken(self):
        env = {"PATH": os.environ["PATH"], "MONGO_URI": "mongodb://user:dbpass-9876@h/x"}
        r = subprocess.run([sys.executable, "-I", str(ROOT / "serve.py")], env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("MAIN_BOT_TOKEN", r.stderr)
        self.assertNotIn("dbpass-9876", r.stdout + r.stderr)

    def test_serve_rejects_invalid_port(self):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "serve.py")], env={**BASE, "PORT": "http"}, capture_output=True, text=True,
                           cwd=str(ROOT), timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("PORT", r.stderr)


# =================================================================== log secret redaction / privacy
class _Capture:
    """A standalone logger wired exactly like production (JsonFormatter + filters) without touching global logging state."""

    def __init__(self, fmt=None, dedup=0.0, clock=None):
        self.stream = io.StringIO()
        h = logging.StreamHandler(self.stream)
        h.setFormatter(fmt or obs.JsonFormatter())
        h.addFilter(obs.DedupFilter(dedup, clock=clock or (lambda: 0.0)))
        self.log = logging.getLogger(f"batch3.test.{id(self)}")
        self.log.handlers[:] = [h]
        self.log.propagate = False
        self.log.setLevel(logging.DEBUG)

    def lines(self):
        return [json.loads(x) for x in self.stream.getvalue().splitlines() if x.strip()]

    def text(self):
        return self.stream.getvalue()


class RedactionTests(unittest.TestCase):
    TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"
    ADMIN = "admin-token-value-XYZ987"
    WH = "test-webhook-secret-0123456789"

    @classmethod
    def setUpClass(cls):
        cls._old = (obs.PSEUDO,)
        obs.configure(secrets=[cls.TOKEN, SECRET, cls.ADMIN, cls.WH], uris=["mongodb+srv://dbuser:Sup3rPass99@cluster0.example.net/db"],
                      pseudonym_key="k", pseudonymize=True)

    @classmethod
    def tearDownClass(cls):
        obs.PSEUDO = cls._old[0]

    def test_every_configured_secret_is_masked_in_messages_args_and_tracebacks(self):
        c = _Capture()
        c.log.error("token %s and %s and %s", self.TOKEN, SECRET, self.ADMIN)
        c.log.error("url https://api.telegram.org/bot%s/getMe failed", self.TOKEN)
        c.log.error("mongodb+srv://dbuser:Sup3rPass99@cluster0.example.net/db unreachable; password Sup3rPass99")
        c.log.info("encoded %s", "s3cr3t-Postback_0123456789abcdef".replace("_", "%5F"))
        try:
            raise RuntimeError(f"boom {SECRET} {self.TOKEN}")
        except RuntimeError:
            c.log.exception("handler crashed")
        text = c.text()
        for s in (self.TOKEN, SECRET, self.ADMIN, "Sup3rPass99", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"):
            self.assertNotIn(s, text, s)
        self.assertIn("***", text)
        rec = c.lines()[-1]
        self.assertEqual(rec["exc_type"], "RuntimeError")
        self.assertEqual(rec["severity"], "ERROR")

    def test_pattern_rules_catch_secrets_that_were_never_registered(self):
        r = obs.redact
        self.assertNotIn("abc123def456", r("GET /api/monetag/postback?secret=abc123def456&ymid=1"))
        self.assertNotIn("zzzTOKENzzz", r("Authorization: Bearer zzzTOKENzzz"))
        self.assertNotIn("987654321:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", r("bot987654321:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/send"))
        self.assertNotIn("hunter22", r("mysql://root:hunter22@db/x"))
        self.assertNotIn("sig999", r('{"hash": "sig999", "init_data": "query_id=AAH"}'))
        self.assertEqual(r("plain message about a file link abcd1234"), "plain message about a file link abcd1234")

    def test_event_fields_mask_sensitive_keys_and_pseudonymise_private_ids(self):
        from ads import slog
        lg = logging.getLogger("ads")
        c = _Capture()
        lg.addHandler(c.log.handlers[0])
        old_level, old_prop = lg.level, lg.propagate
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        try:
            slog.event("probe", user_id=777123, telegram_id="777123", chat_id=777123, ip="203.0.113.77", secret="topsecretvalue1",
                       init_data="query_id=AAHsecret", token="abcdefghij", reason="bad", nested={"authorization": "Bearer xyzxyzxyz", "ok": 1})
        finally:
            lg.removeHandler(c.log.handlers[0])
            lg.setLevel(old_level)
            lg.propagate = old_prop
        text, rec = c.text(), c.lines()[0]
        for leak in ("777123", "203.0.113.77", "topsecretvalue1", "AAHsecret", "abcdefghij", "xyzxyzxyz"):
            self.assertNotIn(leak, text, leak)
        self.assertEqual(rec["event"], "probe")
        self.assertEqual(rec["reason"], "bad")
        self.assertTrue(rec["user_id"].startswith("h:"))
        self.assertEqual(rec["user_id"], rec["chat_id"])                  # same person, same pseudonym: still correlatable
        self.assertEqual(rec["secret"], "***")
        self.assertRegex(rec["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_pseudonyms_differ_per_deployment_key(self):
        a, b = obs.Pseudonymizer(b"k1" * 16), obs.Pseudonymizer(b"k2" * 16)
        self.assertNotEqual(a.tag("tg", 5), b.tag("tg", 5))
        self.assertEqual(a.tag("tg", 5), a.tag("tg", 5))
        self.assertEqual(obs.Pseudonymizer(enabled=False).tag("tg", 5), "5")

    def test_text_formatter_redacts_too(self):
        c = _Capture(obs.TextFormatter())
        c.log.warning("leak %s", self.TOKEN)
        self.assertNotIn(self.TOKEN, c.text())
        self.assertIn("WARNING", c.text())

    def test_access_log_hides_health_probes_masks_query_secrets_and_ips(self):
        f = obs.AccessLogFilter()

        def rec(path, status=200):
            return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("203.0.113.9:5000", "GET", path, "1.1", status), None)
        self.assertFalse(f.filter(rec("/")))
        self.assertFalse(f.filter(rec("/readyz")))
        r = rec("/readyz", 503)
        self.assertTrue(f.filter(r))                                       # failing probes ARE logged
        r = rec("/api/monetag/postback?secret=abcSECRET123&ymid=a.b")
        self.assertTrue(f.filter(r))
        out = r.getMessage()
        self.assertNotIn("abcSECRET123", out)
        self.assertNotIn("203.0.113.9", out)
        self.assertIn("h:", out)

    def test_real_logging_setup_routes_every_logger_through_redaction(self):
        code = r'''
import sys, logging
sys.path.insert(0, sys.argv[1])
import envcheck, observability as obs
env = dict(__import__("os").environ)
obs.bootstrap(env, envcheck.parse_runtime(env))
tok = env["MAIN_BOT_TOKEN"]
logging.getLogger("telegram.ext").error("failed with %s", tok)
logging.getLogger("httpx").info("POST https://api.telegram.org/bot%s/getMe", tok)
logging.getLogger("uvicorn.error").error("secret=%s", env["MONETAG_POSTBACK_SECRET"])
logging.getLogger("pymongo").error("cannot connect to %s", env["MONGO_URI"])
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "198.51.100.4:1", "GET", "/api/monetag/postback?secret=" + env["MONETAG_POSTBACK_SECRET"], "1.1", 200)
logging.getLogger("app").info("hello")
'''
        r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], env=BASE, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        for s in (GOOD_ENV["MAIN_BOT_TOKEN"], SECRET, "dbpass-9876", "198.51.100.4", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"):
            self.assertNotIn(s, out, s)
        recs = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        self.assertTrue(all({"ts", "severity", "logger"} <= set(x) for x in recs))
        self.assertNotIn("httpx", {x["logger"] for x in recs})            # httpx request lines (token URLs) are not emitted at INFO
        self.assertIn("hello", [x.get("msg") for x in recs])


class LogFloodTests(unittest.TestCase):
    def test_identical_warnings_are_collapsed_and_counted(self):
        t = [0.0]
        c = _Capture(dedup=60, clock=lambda: t[0])
        for _ in range(500):
            c.log.warning("database hiccup")
        t[0] = 61
        c.log.warning("database hiccup")
        lines = c.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1].get("suppressed"), 499)

    def test_info_is_never_suppressed_and_distinct_events_are_independent(self):
        c = _Capture(dedup=60)
        for _ in range(5):
            c.log.info("same")
        c.log.warning("a")
        c.log.warning("b")
        self.assertEqual(len(c.lines()), 7)


# =================================================================== alerts (duplicate prevention) + postback monitoring
class AlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.t = [1000.0]
        self.sent = []

        async def send(text): self.sent.append(text)
        self.send = send
        self.a = obs.AlertManager(cooldown=100, max_per_hour=3, clock=lambda: self.t[0])
        self.a.set_sender(send)

    async def test_same_key_alerts_once_per_cooldown_and_reports_suppressed_count(self):
        self.assertTrue(await self.a.alert("db", "down"))
        for _ in range(20):
            self.assertFalse(await self.a.alert("db", "down"))
        self.assertEqual(len(self.sent), 1)
        self.t[0] += 101
        self.assertTrue(await self.a.alert("db", "down again"))
        self.assertIn("+20 similar alerts suppressed", self.sent[-1])

    async def test_concurrent_identical_alerts_send_once(self):
        res = await asyncio.gather(*[self.a.alert("burst", "x") for _ in range(30)])
        self.assertEqual(sum(res), 1)
        self.assertEqual(len(self.sent), 1)

    async def test_global_hourly_cap_with_a_single_notice(self):
        for i in range(10):
            await self.a.alert(f"k{i}", "x")
        self.assertEqual(len(self.sent), 4)                                # 3 alerts + one "limit reached" notice
        self.assertIn("limit reached", self.sent[-1].lower())
        self.t[0] += 3601
        self.assertTrue(await self.a.alert("fresh", "x"))

    async def test_queue_until_sender_exists_then_flush(self):
        a = obs.AlertManager(cooldown=100)
        await a.alert("early", "before the bot")
        self.assertEqual(a.snapshot()["queued"], 1)
        got = []

        async def late(t): got.append(t)
        a.set_sender(late)
        self.assertEqual(await a.flush(), 1)
        self.assertIn("before the bot", got[0])

    async def test_failing_sender_never_raises_and_does_not_burn_the_cooldown(self):
        async def broken(_t): raise RuntimeError("telegram down")
        a = obs.AlertManager(cooldown=3600, clock=lambda: self.t[0])
        a.set_sender(broken)
        self.assertFalse(await a.alert("k", "x"))
        self.assertEqual(a.stats["failed"], 1)
        a.set_sender(self.send)
        self.t[0] += 61                                                    # retried after 60 s, not after the full hour
        self.assertTrue(await a.alert("k", "x"))

    async def test_recovered_is_sent_only_after_an_alert_and_only_once(self):
        self.assertFalse(await self.a.recover("db", "back"))
        await self.a.alert("db", "down")
        self.assertTrue(await self.a.recover("db", "back"))
        self.assertFalse(await self.a.recover("db", "back"))
        self.assertIn("RECOVERED", self.sent[-1])

    async def test_alert_text_is_redacted(self):
        obs.REDACTOR.add("alert-secret-value-123")
        await self.a.alert("r", "failed with alert-secret-value-123 token=abcdef123")
        self.assertNotIn("alert-secret-value-123", self.sent[0])
        self.assertNotIn("abcdef123", self.sent[0])

    async def test_disabled_alerts_send_nothing(self):
        a = obs.AlertManager(enabled=False)
        a.set_sender(self.send)
        self.assertFalse(await a.alert("k", "x"))
        self.assertEqual(self.sent, [])


class PostbackMonitoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        self._saved = (obs.ALERTS._sender, obs.ALERTS.enabled, obs.ALERTS._keys.copy(), list(obs.ALERTS._sent), obs.POSTBACK_WATCH)

        async def send(t): self.sent.append(t)
        obs.ALERTS._keys.clear()
        obs.ALERTS._sent.clear()
        obs.ALERTS.enabled = True
        obs.ALERTS.set_sender(send)
        obs.POSTBACK_WATCH = obs.RateWatch(5, 300)
        obs.METRICS.reset()

    def tearDown(self):
        obs.ALERTS._sender, obs.ALERTS.enabled = self._saved[0], self._saved[1]
        obs.ALERTS._keys.clear()
        obs.ALERTS._keys.update(self._saved[2])
        obs.ALERTS._sent.clear()
        obs.ALERTS._sent.extend(self._saved[3])
        obs.POSTBACK_WATCH = self._saved[4]

    async def test_burst_of_rejections_raises_exactly_one_alert(self):
        for _ in range(40):
            await obs.note_postback_rejected("bad_secret")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("bad_secret=", self.sent[0])
        self.assertEqual(obs.METRICS.get("postback.rejected"), 40)
        self.assertEqual(obs.METRICS.get("postback.rejected.bad_secret"), 40)

    async def test_few_rejections_do_not_alert(self):
        for _ in range(3):
            await obs.note_postback_rejected("unknown_ad")
        self.assertEqual(self.sent, [])

    async def test_unknown_reason_cannot_create_unbounded_metric_names(self):
        for i in range(50):
            await obs.note_postback_rejected(f"attacker-{i}")
        self.assertEqual(obs.METRICS.get("postback.rejected.other"), 50)
        self.assertTrue(all("attacker" not in k for k in obs.METRICS.snapshot()["counters"]))

    async def test_real_postback_handler_counts_accept_duplicate_and_reject(self):
        from ads import postback

        class Store:
            def __init__(self): self.results = ["ok", "dup", "unknown_ad"]
            async def record_postback(self, q, **kw): return self.results.pop(0)
        store = Store()
        q = {"secret": SECRET, "ymid": "a.b", "telegram_id": "1"}
        for _ in range(3):
            await postback.handle(dict(q), secret=SECRET, store=store, ip="1.2.3.4")
        await postback.handle({**q, "secret": "wrong"}, secret=SECRET, store=store, ip="1.2.3.4")
        c = obs.METRICS.snapshot()["counters"]
        self.assertEqual((c["postback.accepted"], c["postback.duplicate"], c["postback.rejected.unknown_ad"], c["postback.rejected.bad_secret"]), (1, 1, 1, 1))


# =================================================================== dependency state + MongoDB recovery
class DependencyTests(unittest.TestCase):
    def test_down_and_recovered_are_reported_once_each(self):
        t = [0.0]
        d = obs.Dependency("mongodb", 3, clock=lambda: t[0])
        self.assertEqual([d.failure("x"), d.failure("x"), d.failure("x"), d.failure("x"), d.failure("x")], [None, None, "down", None, None])
        t[0] = 42
        self.assertEqual(d.success(), "recovered")
        self.assertIsNone(d.success())
        self.assertEqual(d.snapshot()["last_downtime_s"], 42.0)
        self.assertEqual(d.status, "ok")

    def test_a_blip_below_the_threshold_is_not_an_outage(self):
        d = obs.Dependency("x", 3)
        d.failure("a"); d.failure("a"); self.assertIsNone(d.success())
        self.assertEqual(d.consecutive_failures, 0)


class MongoRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_alerts_once_when_down_and_once_when_back(self):
        fdb = FakeDB()
        sent = []

        async def send(t): sent.append(t)
        alerts = obs.AlertManager(cooldown=3600)
        alerts.set_sender(send)
        dep = obs.Dependency("mongodb", 2)

        async def ping():
            if fdb.down:
                raise ConnectionFailure("down")
        steps = []

        async def sleep(dt):
            steps.append(dt)
            n = len(steps)
            if n == 1:
                fdb.down = True
            if n == 7:
                fdb.down = False
            if n == 10:
                raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await health.db_monitor_loop(ping, dep, alerts, interval=15, sleep=sleep)
        self.assertEqual(len([s for s in sent if "unreachable" in s]), 1)
        self.assertEqual(len([s for s in sent if "restored" in s]), 1)
        self.assertEqual(dep.status, "ok")
        self.assertIn(5.0, steps)                                          # polls faster while failing (interval/3)

    async def test_rate_limiter_degrades_then_recovers_and_reports_both_once(self):
        from ads import ratelimit
        fdb = FakeDB()
        t = [10_000.0]
        lim = ratelimit.SharedLimiter(lambda: fdb["rate_limits"], ratelimit.load_settings({}, "salt"), clock=lambda: t[0])
        events = []
        lim.on_state = lambda s, i: events.append(s)
        self.assertTrue((await lim.hit("api_done_user", 1)).allowed)
        fdb.down = True
        for i in range(30):
            await lim.hit("api_done_user", i)
        self.assertTrue(lim.degraded)
        self.assertEqual(events, ["degraded"])                             # one transition, not one per request
        self.assertFalse((await lim.hit("postback_fail_ip", "1.1.1.1")).allowed)          # brute-force counters still fail CLOSED (Batch 2)
        fdb.down = False
        t[0] += 6                                                          # circuit breaker cooldown
        self.assertTrue((await lim.hit("api_done_user", 99)).allowed)
        self.assertFalse(lim.degraded)
        self.assertEqual(events, ["degraded", "recovered"])

    async def test_broken_monitoring_callback_never_breaks_rate_limiting(self):
        from ads import ratelimit
        fdb = FakeDB()
        lim = ratelimit.SharedLimiter(lambda: fdb["rate_limits"], ratelimit.load_settings({}, "salt"))

        def boom(*_a): raise RuntimeError("monitor bug")
        lim.on_state = boom
        fdb.down = True
        d = await lim.hit("api_done_user", 1)
        self.assertTrue(d.allowed and d.degraded)


# =================================================================== readiness / traffic gate / retries / lifecycle
class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def mk(self, ping=None, complete=None, **kw):
        lc = health.Lifecycle()
        lc.begin_start()
        calls = {"ping": 0, "mig": 0}

        async def p():
            calls["ping"] += 1
            if ping:
                await ping()

        async def m():
            calls["mig"] += 1
            return True if complete is None else await complete()
        return lc, health.Readiness(lc, p, m, **kw), calls

    async def test_not_ready_while_starting_failed_or_stopping_and_the_database_is_not_touched(self):
        lc, r, calls = self.mk()
        for state in (health.State.STARTING, health.State.FAILED, health.State.STOPPING, health.State.STOPPED):
            lc.state = state
            ok, body = await r.check()
            self.assertFalse(ok)
            self.assertEqual(body["checks"]["startup"], state.value)
        self.assertEqual(calls, {"ping": 0, "mig": 0})

    async def test_ready_when_started_database_up_and_migrations_complete(self):
        lc, r, _ = self.mk(cache_seconds=0)
        lc.mark_ready()
        self.assertEqual(await r.check(), (True, {"status": "ready", "checks": {"startup": "ready", "database": "ok", "migrations": "ok"}}))

    async def test_database_unavailable_and_recovery(self):
        down = {"v": False}

        async def ping():
            if down["v"]:
                raise ConnectionFailure("x")
        lc, r, _ = self.mk(ping=ping, cache_seconds=0)
        lc.mark_ready()
        self.assertTrue((await r.check())[0])
        down["v"] = True
        ok, body = await r.check()
        self.assertFalse(ok)
        self.assertEqual((body["checks"]["database"], body["checks"]["migrations"]), ("unavailable", "unknown"))
        down["v"] = False
        self.assertTrue((await r.check())[0])

    async def test_pending_migration_is_not_ready(self):
        async def incomplete(): return False
        lc, r, _ = self.mk(complete=incomplete, cache_seconds=0)
        lc.mark_ready()
        ok, body = await r.check()
        self.assertEqual((ok, body["checks"]["migrations"]), (False, "pending"))

    async def test_migration_check_error_is_database_unavailable_not_a_crash(self):
        async def err(): raise ConnectionFailure("x")
        lc, r, _ = self.mk(complete=err, cache_seconds=0)
        lc.mark_ready()
        self.assertFalse((await r.check())[0])

    async def test_hanging_database_times_out(self):
        async def hang(): await asyncio.sleep(30)
        lc, r, _ = self.mk(ping=hang, cache_seconds=0, timeout=0.05)
        lc.mark_ready()
        ok, body = await r.check()
        self.assertEqual((ok, body["checks"]["database"]), (False, "unavailable"))

    async def test_endpoint_does_real_work_at_most_once_per_cache_window_even_under_concurrency(self):
        lc, r, calls = self.mk(cache_seconds=60)
        lc.mark_ready()
        await asyncio.gather(*[r.check() for _ in range(50)])
        self.assertEqual(calls["ping"], 1)

    async def test_body_never_contains_error_text(self):
        async def ping(): raise ConnectionFailure("mongodb://user:dbpass-9876@h refused")
        lc, r, _ = self.mk(ping=ping, cache_seconds=0)
        lc.mark_ready()
        self.assertNotIn("dbpass", json.dumps((await r.check())[1]))


class HealthGateTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, gate, path):
        sent = []

        async def send(m): sent.append(m)
        await gate({"type": "http", "path": path}, None, send)
        return sent

    async def test_only_health_probes_pass_while_not_ready_and_everything_passes_when_ready(self):
        lc = health.Lifecycle()
        reached = []

        async def app(scope, receive, send): reached.append(scope["path"]); await send({"type": "http.response.start", "status": 200, "headers": []})
        gate = health.HealthGate(app, lc)
        lc.begin_start()
        for state in (health.State.STARTING, health.State.FAILED, health.State.STOPPING):
            lc.state = state
            for path in ("/api/smart/start", "/f/x", "/webhook", "/app", "/admin/ads"):
                m = await self.call(gate, path)
                self.assertEqual(m[0]["status"], 503, (state, path))
                self.assertIn((b"retry-after", b"5"), m[0]["headers"])
            for path in ("/", "/readyz"):
                self.assertEqual((await self.call(gate, path))[0]["status"], 200)
        self.assertEqual(reached, ["/", "/readyz"] * 3)
        lc.state = health.State.READY
        self.assertEqual((await self.call(gate, "/api/smart/start"))[0]["status"], 200)

    async def test_non_http_scopes_are_untouched(self):
        lc = health.Lifecycle()
        seen = []

        async def app(scope, receive, send): seen.append(scope["type"])
        await health.HealthGate(app, lc)({"type": "lifespan"}, None, None)
        self.assertEqual(seen, ["lifespan"])


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_errors_retry_with_backoff_then_succeed(self):
        class NetworkError(Exception): pass
        n, sleeps = {"n": 0}, []

        async def fn():
            n["n"] += 1
            if n["n"] < 4:
                raise NetworkError("x")
            return "ok"

        async def sleep(d): sleeps.append(d)
        self.assertEqual(await health.retry_until(fn, timeout=100, what="t", sleep=sleep), "ok")
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])

    async def test_permanent_and_unknown_errors_are_not_retried(self):
        class InvalidToken(Exception): pass
        n = {"n": 0}

        async def bad():
            n["n"] += 1
            raise InvalidToken("x")
        with self.assertRaises(InvalidToken):
            await health.retry_until(bad, timeout=100, what="t", sleep=lambda d: asyncio.sleep(0))
        self.assertEqual(n["n"], 1)

        async def other(): n["n"] += 1; raise ValueError("x")
        with self.assertRaises(ValueError):
            await health.retry_until(other, timeout=100, what="t", sleep=lambda d: asyncio.sleep(0))
        self.assertEqual(n["n"], 2)

    async def test_gives_up_at_the_deadline_and_honours_flood_wait(self):
        class RetryAfter(Exception):
            retry_after = 7
        t, sleeps = [0.0], []

        async def sleep(d): sleeps.append(d); t[0] += d

        async def fn(): raise RetryAfter()
        with self.assertRaises(RetryAfter):
            await health.retry_until(fn, timeout=20, what="t", sleep=sleep, clock=lambda: t[0])
        self.assertEqual(sleeps[0], 7)
        self.assertLessEqual(t[0], 28)


class LifecycleTests(unittest.TestCase):
    def test_transitions(self):
        lc = health.Lifecycle()
        self.assertTrue(lc.begin_start())
        self.assertFalse(lc.begin_start())                                 # a second start is refused
        lc.mark_ready()
        self.assertTrue(lc.accepting)
        self.assertTrue(lc.begin_stopping())
        self.assertFalse(lc.begin_stopping())
        lc.fail("x", "E")                                                  # a late failure never un-stops a stopping process
        self.assertEqual(lc.state, health.State.STOPPING)
        lc.mark_stopped()
        self.assertFalse(lc.accepting)

    def test_fail_then_exit_callback(self):
        lc = health.Lifecycle()
        codes = []
        lc.set_exit_callback(codes.append)
        lc.begin_start()
        lc.fail("migrations", "MigrationError")
        lc.request_exit(1)
        self.assertEqual((codes, lc.exit_code, lc.failure["phase"], lc.accepting), ([1], 1, "migrations", False))


# =================================================================== the REAL manager.py (startup / shutdown / failures / tasks)
class ManagerHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.o, cls.r = run_harness("manager_harness.py")

    def test_startup_is_non_blocking_then_ready_and_registers_the_webhook_once(self):
        o = self.o
        self.assertEqual(o["state_right_after_start"], "starting")
        self.assertTrue(o["ready"])
        self.assertEqual(o["state"], "ready")
        count, args = o["webhook"]
        self.assertEqual(count, 1)
        self.assertEqual(args, ["https://example.test/webhook", True, ["message", "callback_query"]])    # url, derived secret_token, allowed updates
        self.assertEqual(o["delete_my_commands"], 1)
        self.assertTrue(o["alert_sender_ready"])

    def test_background_tasks_started_once_each(self):
        self.assertEqual(self.o["tasks"], ["ads_optimizer", "cleanup", "db_monitor", "monetag_optimizer", "startup", "webhook_monitor"])

    def test_graceful_shutdown_order_and_idempotence(self):
        o = self.o
        self.assertEqual(o["shutdown_order"], ["app.start", "app.stop", "app.shutdown", "db.close"])
        self.assertEqual(o["stopped_state"], "stopped")
        self.assertTrue(o["second_stop_noop"])
        self.assertEqual(o["exits_normal"], [])                            # a clean stop does not request a failure exit
        self.assertEqual({v for k, v in o["tasks_after_stop"].items() if k in ("cleanup", "db_monitor", "webhook_monitor")}, {"cancelled"})

    def test_duplicate_start_runs_nothing_twice(self):
        o = self.o
        self.assertEqual((o["dup_set_webhook"], o["dup_build"]), (1, 1))
        self.assertEqual(o["dup_noop_loops"], 2)                            # monetag + ads loops started exactly once each
        self.assertTrue(o["dup_task_refused"])
        self.assertTrue(o["dup_running_task_refused"])

    def test_failed_migration_prevents_serving_alerts_and_exits_non_zero(self):
        o = self.o
        self.assertFalse(o["mig_ready"])
        self.assertEqual(o["mig_state"], ["failed", "migrations", "migrations", "MigrationError"])
        self.assertEqual((o["mig_app_built"], o["mig_webhook_set"]), (0, 0))   # no Telegram application, no webhook => nothing can be served
        self.assertEqual(o["mig_exit"], [1])
        self.assertEqual(len(o["mig_alert"]), 1)
        self.assertIn("phase: migrations", o["mig_alert"][0])
        self.assertNotIn("dbpass-9876", o["mig_alert"][0])                  # the connection string inside the error text is redacted
        self.assertEqual(o["mig_tasks"], ["startup"])                       # no background loop was started on a half-migrated database
        self.assertEqual(o["mig_stop_after_fail"], "stopped")

    def test_invalid_bot_token_is_fatal_without_retries(self):
        self.assertEqual(self.o["tok"], ["failed", "telegram_init", 1, [1], None])

    def test_transient_telegram_errors_at_boot_are_retried(self):
        self.assertTrue(self.o["net_ready"])
        self.assertEqual(self.o["net"], [3, 3, []])

    def test_telegram_unreachable_for_the_whole_startup_budget_is_fatal(self):
        self.assertFalse(self.o["down_ready"])
        self.assertEqual(self.o["down"], ["failed", "telegram_init", [1]])

    def test_webhook_registration_failure_still_serves_and_alerts_once_after_repeated_failures(self):
        o = self.o
        self.assertTrue(o["wh_ready"])
        self.assertEqual(o["wh_state"][:1], ["ready"])
        self.assertEqual(o["wh_alert_count"], 1)
        self.assertIn("webhook", o["wh_alerts"][0][1].lower())

    def test_bad_webhook_url_is_a_fatal_configuration_error(self):
        self.assertEqual(self.o["bad_url"], ["failed", "webhook", [1]])

    def test_telegram_reported_webhook_errors_alert_once_redacted_and_url_drift_is_repaired(self):
        o = self.o
        self.assertEqual(len(o["wh_err_alerts"]), 1)                         # same Telegram error seen twice => one alert
        text = o["wh_err_alerts"][0][1]
        self.assertIn("503", text)
        self.assertNotIn("SECRETVALUE123456", text)
        self.assertEqual(o["wh_info"]["pending_updates"], 12)
        self.assertEqual(o["wh_reregistered"], 1)

    def test_alert_dedup_cap_queue_and_sender_failure_on_real_manager_wiring(self):
        o = self.o
        self.assertEqual(o["alert_dedup"][0], [True, False, False, True])
        self.assertEqual(o["alert_dedup"][1][0], "first")
        self.assertTrue(o["alert_suppressed_note"])
        self.assertEqual(o["alert_cap"], [4, 5])
        self.assertEqual(o["alert_queue"], [1, 1])
        self.assertEqual(o["alert_sender_failure"], [False, 1])

    def test_background_task_lifecycle_restart_backoff_and_stop(self):
        o = self.o
        runs, sleeps, crashes, n_crashes = o["task_backoff"]
        self.assertEqual((runs, sleeps, n_crashes), (6, [1, 2, 4, 8, 8], 5))   # exponential backoff capped at max_backoff
        self.assertEqual(crashes, [["flaky", 3]])                              # crash alert hook at the 3rd consecutive crash only
        self.assertEqual(o["task_finished"][0], "finished")                    # a loop that returns is not restarted
        self.assertEqual(o["task_stop"], [[], False, False])                   # stop() cancels, nothing stuck, and start() is refused afterwards

    def test_nothing_in_the_harness_output_leaks_secrets(self):
        out = self.r.stdout + self.r.stderr
        for s in ("dbpass-9876", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", SECRET, "SECRETVALUE123456"):
            self.assertNotIn(s, out, s)


# =================================================================== the REAL main.py (webhook failures, traffic gate, diagnostics)
class MainBatch3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.o, cls.r = run_harness("main_harness.py", {"MONETAG_ADMIN_TOKEN": "admintok-Value-123", "RL_MONETAG_ADMIN_FAIL_IP": "2/600"})

    def test_readiness_endpoint_states(self):
        o = self.o
        self.assertEqual((o["readyz_starting"], o["readyz_before"], o["readyz_after"], o["readyz_down"], o["readyz_recovered"]),
                         (503, 503, 200, 503, 200))

    def test_webhook_failures(self):
        o = self.o
        self.assertEqual(o["wh_ok"], [{"ok": True}, 1])
        self.assertEqual(o["wh_bad_payload"], 400)
        self.assertEqual(o["wh_empty_update"], 400)
        self.assertEqual(o["wh_not_ready"], [503, "5", 1])                      # authentic update while not ready: 503 so Telegram retries; nothing queued
        c = o["wh_counters"]
        self.assertEqual((c["webhook.accepted"], c["webhook.bad_payload"], c["webhook.not_ready"]), (1, 2, 1))
        self.assertGreaterEqual(c["webhook.rejected"], 1)                       # wrong secrets from the Batch 2 scenario

    def test_traffic_gate_in_the_real_app(self):
        g = self.o["gate"]
        for state in ("starting", "failed", "stopping"):
            self.assertEqual(g[state], [503, 503, 503, 503, 200, 200], state)  # everything but / and /readyz refused
        self.assertEqual(g["ready"], [200] * 6)

    def test_admin_health_requires_the_token_and_never_leaks(self):
        o = self.o
        self.assertEqual(o["health_bad_token"][:2], [403, 403])
        self.assertEqual(o["health_bad_token"][2], 429)                          # brute-force counter (RL_MONETAG_ADMIN_FAIL_IP=2/600)
        self.assertEqual(o["health_status"], 200)
        self.assertEqual(o["health_leaks"], [])
        for k in ("service", "profile", "dependencies", "background_tasks", "metrics", "alerts", "configured"):
            self.assertIn(k, o["health_keys"])

    def test_unhandled_and_database_errors_return_generic_bodies(self):
        o = self.o
        self.assertEqual(o["unhandled"][0], 500)
        self.assertNotIn("123456", json.dumps(o["unhandled"][1]))
        self.assertNotIn("secret detail", json.dumps(o["unhandled"][1]))
        self.assertEqual(o["db_error"][0:2], [503, "5"])
        self.assertNotIn("db down", json.dumps(o["db_error"][2]))


# =================================================================== Docker / deployment configuration
class DeploymentConfigTests(unittest.TestCase):
    def read(self, name): return (ROOT / name).read_text(encoding="utf-8")

    def test_dockerfile_has_no_baked_secrets_and_a_signal_safe_cmd(self):
        d = self.read("Dockerfile")
        self.assertFalse(re.search(r"(?im)^\s*(ENV|ARG)\b[^\n]*(TOKEN|SECRET|PASSWORD|MONGO_URI|API_KEY|WEBHOOK|OWNER_ID|BASE_URL)", d))
        self.assertNotRegex(d, r"(?im)^\s*(COPY|ADD)\b[^\n]*\.env")
        self.assertIn('CMD ["python", "serve.py"]', d)                          # exec form: PID 1 receives SIGTERM
        self.assertFalse([l for l in d.splitlines() if re.match(r"\s*(CMD|ENTRYPOINT)\b", l) and re.search(r"\bsh\b|bash", l)])
        self.assertRegex(d, r"(?m)^USER\s+\S+")
        self.assertIn("EXPOSE 8000", d)
        self.assertIn("HEALTHCHECK", d)
        self.assertTrue(d.startswith("FROM python:3.12-slim"))

    def test_dockerignore_keeps_secrets_and_junk_out_of_the_image(self):
        ig = self.read(".dockerignore").split()
        for pat in (".env", ".env.*", ".git", "tests", "__pycache__"):
            self.assertIn(pat, ig)

    def test_every_file_the_app_reads_at_runtime_is_in_the_image(self):
        ig = self.read(".dockerignore").split()
        for f in ("redirect.html", "miniapp.html", "ads/adapters.js", "ads/guard.js", "ads/admin_ads.html", "serve.py", "requirements.txt"):
            self.assertTrue((ROOT / f).exists(), f)
            self.assertNotIn(f, ig)
        self.assertNotIn("*.html", ig)
        self.assertNotIn("*.js", ig)

    def test_procfile_and_dockerfile_use_the_same_entry_point_and_port_variable(self):
        self.assertEqual(self.read("Procfile").strip(), "web: python serve.py")
        self.assertIn("PORT", self.read("serve.py"))
        self.assertNotIn("--port 8000", self.read("Procfile"))

    def test_requirements_are_pinned(self):
        for line in self.read("requirements.txt").splitlines():
            if line.strip() and not line.startswith("tzdata"):
                self.assertRegex(line, r"==\d")

    def test_docs_do_not_contradict_each_other_about_health_checks(self):
        k, r, e = self.read("KOYEB.md"), self.read("README.md"), self.read("_.env.example")
        self.assertNotRegex(k, r"(?i)health check\s*`/`")                       # the old "Health check `/`" instruction
        self.assertNotRegex(k + r + e, r"(?i)point the koyeb health check at `/readyz` \(503 until[^\n]*`/` stays")
        self.assertIn("/readyz", k)
        self.assertIn("liveness", k.lower())
        self.assertIn("readiness", k.lower())
        self.assertIn("KOYEB.md", r)
        self.assertNotRegex(r + k, r"(?i)mistral")
        self.assertNotIn("onrender.com", e)
        self.assertNotIn("uvicorn main:app", e)
        for doc in (k, r):
            self.assertNotRegex(doc, r"(?i)(has been|was|is)\s+(successfully\s+)?(deployed|verified)\s+(on|in)\s+(a\s+)?(real\s+)?koyeb(?!\s+account by)")
        self.assertIn("not been verified", r)

    def test_env_example_documents_every_variable_the_code_reads(self):
        src = ""
        for p in list(ROOT.glob("*.py")) + list((ROOT / "ads").glob("*.py")) + list((ROOT / "services").glob("*.py")):
            if p.name.startswith("test"):
                continue
            src += p.read_text(encoding="utf-8") + "\n"
        names = set(re.findall(r'(?:getenv|environ\.get|env\.get|_req|_bool|_int|_float|num)\(\s*"([A-Z][A-Z0-9_]{2,})"', src))
        names |= set(re.findall(r'\b_(?:str|bool|int|float|num)\(\s*(?:env\s*,\s*)?"([A-Z][A-Z0-9_]{2,})"', src))
        env_example = self.read("_.env.example") + self.read("KOYEB.md")
        optional_internal = {"KOYEB_GIT_SHA", "GIT_SHA", "RL_", "PATH", "PORT"}
        missing = sorted(n for n in names if n not in optional_internal and n not in env_example)
        self.assertEqual(missing, [], "undocumented environment variables")

    def test_required_variables_are_listed_uncommented_in_the_env_example(self):
        e = self.read("_.env.example")
        for n in envcheck.REQUIRED:
            self.assertRegex(e, rf"(?m)^{n}=", n)

    def test_no_secret_is_ever_sent_to_the_frontend(self):
        for f in ("miniapp.html", "redirect.html", "ads/adapters.js", "ads/guard.js", "ads/admin_ads.html"):
            t = self.read(f)
            for name in ("MONETAG_POSTBACK_SECRET", "MAIN_BOT_TOKEN", "WEBHOOK_SECRET", "ADS_ADMIN_TOKEN", "MONETAG_ADMIN_TOKEN", "MONGO_URI", "RATE_LIMIT_SALT"):
                if f == "ads/admin_ads.html" and name == "ADS_ADMIN_TOKEN":
                    continue                                                # only named in a "set ADS_ADMIN_TOKEN" help message; the value is typed by the owner
                self.assertNotIn(name, t, (f, name))


# =================================================================== Batch 1 security regression (through the Batch 3 wiring)
class Batch1RegressionTests(unittest.TestCase):
    def boot(self, extra):
        env = {**BASE, **extra}
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run([sys.executable, "-I", str(ROOT / "tests" / "main_harness.py"), str(ROOT)], env=env, capture_output=True, text=True, timeout=120)

    def test_production_is_the_default_and_still_fail_closed(self):
        r = self.boot({"MONETAG_POSTBACK_SECRET": None})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MONETAG_POSTBACK_SECRET is required", r.stderr)
        r = self.boot({"AD_VERIFY_MODE": "legacy"})
        self.assertNotEqual(r.returncode, 0)
        r = self.boot({"MONETAG_POSTBACK_SECRET": "short"})
        self.assertNotEqual(r.returncode, 0)

    def test_tads_cannot_unlock_files_in_production_even_if_enabled_by_env(self):
        r = self.boot({"TADS_ALLOW_CLIENT_REWARD": "true"})
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        o = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(o["strict"])
        self.assertTrue(all("tads" not in u for u in o["probe"]))

    def test_legacy_client_attested_no_fill_grace_is_off_in_production_and_on_in_development(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);import config;print(config.NOFILL_GRACE, config.STRICT_ADS)"
        for extra, want in (({}, "False True"), ({"NOFILL_GRACE": "true"}, "False True"),
                            ({"APP_ENV": "development", "MONETAG_POSTBACK_SECRET": None, "BASE_URL": "http://localhost:8000"}, "True False")):
            env = {k: v for k, v in {**BASE, **extra}.items() if v is not None}
            r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip().splitlines()[-1], want, r.stderr[-500:])

    def test_postback_secret_never_reaches_logs_or_the_served_page(self):
        r = self.boot({})
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_postback_rejection_log_lines_have_no_secret_or_ymid(self):
        from ads import postback

        class Store:
            async def record_postback(self, q, **kw): return "user_mismatch"
        lg = logging.getLogger("ads")
        c = _Capture()
        lg.addHandler(c.log.handlers[0])
        old = (lg.level, lg.propagate)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        try:
            asyncio.run(postback.handle({"secret": SECRET, "ymid": "sid1.cafe0001", "telegram_id": "777"}, secret=SECRET, store=Store(), ip="198.51.100.4"))
            asyncio.run(postback.handle({"secret": "guess-guess-guess-guess"}, secret=SECRET, store=Store(), ip="198.51.100.4"))
        finally:
            lg.removeHandler(c.log.handlers[0])
            lg.setLevel(old[0])
            lg.propagate = old[1]
        t = c.text()
        for leak in (SECRET, "guess-guess", "cafe0001", "198.51.100.4", "777"):
            self.assertNotIn(leak, t, leak)
        self.assertIn("postback_rejected", t)


if __name__ == "__main__":
    unittest.main()
