"""Batch 2: the REAL main.py / handlers.py with Telegram, FastAPI and MongoDB stubbed (subprocess harnesses), under production settings.
Also pins the Batch 1 security guarantees that the rate limiter / migration changes must not weaken."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "s3cr3t-Postback_0123456789abcdef"
BASE = {"PATH": os.environ.get("PATH", ""), "MAIN_BOT_TOKEN": "123456:TEST-TOKEN", "OWNER_ID": "1", "MONGO_URI": "mongodb://unused",
        "WEBHOOK_SECRET": "test-webhook-secret-0123456789", "BASE_URL": "https://example.test", "PRIMARY_CHANNEL": "-1001", "BACKUP_CHANNEL": "-1002",
        "TADS_WIDGET_ID": "13070", "MONETAG_POSTBACK_SECRET": SECRET}


def run(harness, extra=None):
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tests" / harness), str(ROOT)], env={**BASE, **(extra or {})},
                       capture_output=True, text=True, timeout=120)
    return r, (json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None)


class MainRateLimitAndReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r, cls.out = run("main_harness.py", {"RL_LEGACY_DONE_IP": "2/60", "RL_LINK_IP": "3/60", "RL_WEBHOOK_FAIL_IP": "2/600",
                                                 "MONETAG_ADMIN_TOKEN": "admintok", "RL_MONETAG_ADMIN_FAIL_IP": "2/600"})
        assert cls.r.returncode == 0, cls.r.stderr[-3000:]

    def test_readiness_reflects_migrations_and_database(self):
        o = self.out
        self.assertEqual((o["readyz_before"], o["readyz_after"], o["readyz_down"], o["readyz_recovered"]), (503, 200, 503, 200))

    def test_forged_x_forwarded_for_cannot_dodge_limits(self):
        self.assertEqual(self.out["ip_spoof"], ["203.0.113.5"])
        self.assertEqual(self.out["postback_codes"], [20, 10])           # 20 failures then 429, although every request claimed a new IP
        self.assertEqual(self.out["postback_429_retry_after"], "60")

    def test_real_postback_secret_is_never_locked_out(self):
        self.assertTrue(self.out["postback_real_secret_after_lockout"])

    def test_legacy_and_link_and_admin_and_webhook_endpoints_are_limited(self):
        o = self.out
        self.assertEqual(o["legacy_done"], [None, None, "rate_limited", "rate_limited", "rate_limited"])
        self.assertEqual(o["link_codes"], [200, 200, 200, 429, 429])
        self.assertEqual(o["admin_codes"], [403, 403, 429, 429, 429])
        self.assertEqual(o["webhook_codes"], [403, 403, 429, 429])

    def test_batch1_behaviour_still_present(self):
        o = self.out
        self.assertTrue(o["strict"])
        self.assertEqual((o["bad_secret"], o["valid"], o["duplicate"]), ([403, "forbidden"], [200, "ok"], [200, "dup"]))
        self.assertFalse(o["secret_in_html"])
        self.assertTrue(all("tads" not in u for u in o["probe"]))
        self.assertNotIn(SECRET, self.r.stdout + self.r.stderr)

    def test_invalid_rate_limit_configuration_stops_the_process(self):
        r, _ = run("main_harness.py", {"RL_API_DONE_USER": "lots"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RL_API_DONE_USER", r.stderr)
        self.assertNotIn(SECRET, r.stderr)

    def test_production_still_requires_the_postback_secret(self):
        env = {k: v for k, v in BASE.items() if k != "MONETAG_POSTBACK_SECRET"}
        r = subprocess.run([sys.executable, "-I", str(ROOT / "tests" / "main_harness.py"), str(ROOT)], env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MONETAG_POSTBACK_SECRET is required", r.stderr)


class BotHandlerLimitTests(unittest.TestCase):
    def test_telegram_user_limits_on_the_real_handlers(self):
        r, o = run("handlers_harness.py")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(o["sessions_created"], 3)               # RL_BOT_START_USER=3/60: the 4th and 5th /start TOKEN never reach the DB
        self.assertEqual(o["limit_message"], 2)
        self.assertTrue(o["other_user_ok"])
        self.assertEqual(o["callback_slowdowns"], 2)             # RL_BOT_MSG_USER=4/60 over 6 taps
        self.assertEqual(o["text_replies"], 4)                   # silent after the limit
        self.assertEqual(o["sessions_during_outage"], 3)         # database down: still limited (strict local window), not unlimited
        self.assertTrue(o["degraded"])


if __name__ == "__main__":
    unittest.main()
