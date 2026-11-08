"""Executes the real main.py (stubbed Telegram/FastAPI/Mongo, see main_harness.py) under both profiles."""
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
        "TADS_WIDGET_ID": "13070"}


def run_main(extra: dict):
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tests" / "main_harness.py"), str(ROOT)],
                       env={**BASE, **extra}, capture_output=True, text=True, timeout=120)
    return r


class MainWiringTests(unittest.TestCase):
    def test_production_profile(self):
        r = run_main({"MONETAG_POSTBACK_SECRET": SECRET})
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(out["strict"])                                         # policy handed to the ad engine is strict
        self.assertTrue(out["postback_mounted"])
        self.assertEqual(out["bad_secret"], [403, "forbidden"])
        self.assertEqual(out["wrong_user"], [403, "rejected"])
        self.assertEqual(out["valid"], [200, "ok"])
        self.assertEqual(out["duplicate"], [200, "dup"])
        self.assertEqual(out["verified_flags"], [True, True])
        self.assertIn("Verification failed", out["auth_fresh"])                # got past the expiry gate, failed on (empty) initData
        self.assertIn("Session expired", out["auth_expired"])                  # explicit expiry, not only Mongo's TTL reaper
        self.assertTrue(all("tads" not in u for u in out["probe"]), out["probe"])   # Tads isn't served, so its host must not count as a blocker
        self.assertNotIn(SECRET, r.stdout + r.stderr)                          # nothing prints the secret
        self.assertFalse(out["secret_in_html"])                                # ... and it never reaches the served Mini App page

    def test_production_without_secret_does_not_start(self):
        r = run_main({})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MONETAG_POSTBACK_SECRET is required", r.stderr)

    def test_production_refuses_legacy_verification(self):
        r = run_main({"MONETAG_POSTBACK_SECRET": SECRET, "AD_VERIFY_MODE": "legacy"})
        self.assertNotEqual(r.returncode, 0)

    def test_development_profile_is_separate(self):
        r = run_main({"APP_ENV": "development"})
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertFalse(out["strict"])
        self.assertFalse(out["postback_mounted"])                              # no secret => endpoint isn't mounted at all
        self.assertTrue(any("tads" in u for u in out["probe"]), out["probe"])  # Tads still served in development
        self.assertIn("development profile", r.stdout + r.stderr)                         # loudly labelled


if __name__ == "__main__":
    unittest.main()
