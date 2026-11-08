"""End-to-end Smart Ads flow against an in-memory Mongo fake: real service/store/verify/routes handlers."""
import ast
import asyncio
import hashlib
import hmac as _hmac
import json
import re
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from ads import engine, routes, service, settings, store
from ads.verify import Policy
from tests.fakemongo import FakeDB, FakeSessions

ROOT = Path(__file__).resolve().parent.parent
BOT_TOKEN = "123456:TEST-TOKEN"
UID = 777


def load_validate_init_data():
    """Pull the REAL validate_init_data out of main.py without importing the whole app (telegram/motor not needed)."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "validate_init_data")
    ns = {"parse_qsl": __import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl, "hmac": _hmac, "hashlib": hashlib,
          "time": time, "json": json}
    exec(compile(ast.Module([fn], []), "main.py", "exec"), ns)
    return ns["validate_init_data"]


validate_init_data = load_validate_init_data()


def make_init_data(user_id=UID, token=BOT_TOKEN, start_param="tok1", age=0, bot_token=BOT_TOKEN):
    f = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id}), "start_param": start_param}
    check = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    secret = _hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    f["hash"] = _hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(f)


class FakeBot:
    token, username = BOT_TOKEN, "testbot"
    async def send_chat_action(self, *a): pass
    async def delete_message(self, *a): pass


class FakeApp:
    bot = FakeBot(); bot_data = {"bot_id": 1}


class Base(unittest.IsolatedAsyncioTestCase):
    NETS = {"monetag": True, "tads": True}
    MIN_SECONDS = 0

    async def asyncSetUp(self):
        self.fdb = FakeDB()
        store.set_database(self.fdb)
        self.sess = FakeSessions(self.fdb)
        self._saved = (settings.TADS_WIDGET_ID, dict(settings.RUNTIME))
        settings.TADS_WIDGET_ID = "13070"
        settings.RUNTIME["monetag_postback"] = False
        settings.RUNTIME["strict"] = False        # DEVELOPMENT profile (client-attested rewards allowed); strict mode is covered in test_ad_security.py
        self.policy = Policy(monetag_postback=False, min_seconds=self.MIN_SECONDS, wait_seconds=20, strict=False)
        service.configure(service.Deps(sessions=self.sess, policy=self.policy))
        await store.save_config({"mode": "priority", "priority": ["monetag", "tads"], "max_attempts": 3,
                                 "networks": {n: {"enabled": e, "allow_client_reward": True} for n, e in self.NETS.items()}})
        engine.invalidate()
        self.sent = []
        self.app = FakeApp()

        async def auth(body):
            sid = body.get("sid")
            s = await self.sess.get_session(sid) if isinstance(sid, str) and sid else None
            if not s:
                return None, None, "Session expired. Please open the link again."
            info = validate_init_data(body.get("init_data", ""), BOT_TOKEN)
            if not info or info["user"].get("id") != s["user_id"]:
                return None, None, "Verification failed. Please open the link again."
            return s, self.app, None

        def identify(init_data):
            info = validate_init_data(init_data, BOT_TOKEN)
            return (self.app, info) if info else (None, None)

        async def send_file(bot, chat_id, doc, manager): self.sent.append(doc["token"]); return True

        self.ctx = routes.Ctx(auth=auth, identify=identify, send_file=send_file, db=self.sess, token_re=re.compile(r"^[A-Za-z0-9_-]{1,64}$"),
                              telegram_errors=(), forbidden=(), manager=None, ads_required=1)
        await self.sess.create_session("sid1", "tok1", UID, UID, 1, 1)
        self.init = make_init_data()

    async def asyncTearDown(self):
        settings.TADS_WIDGET_ID, rt = self._saved
        settings.RUNTIME.clear(); settings.RUNTIME.update(rt)
        store.set_database(None); engine.invalidate()

    def body(self, **extra): return dict({"sid": "sid1", "init_data": self.init}, **extra)
    async def start(self, **kw): return await routes.h_start(self.ctx, self.body(**kw), "1.1.1.1")


class StartTests(Base):
    async def test_valid_start_returns_success_and_network_config(self):
        r = await self.start()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["network"], r["sid"]), ("monetag", "sid1"))
        self.assertTrue(r["ad_id"].startswith("sid1."))
        self.assertNotIn("secret", json.dumps(r).lower())             # secrets never reach the browser

    async def test_direct_launch_without_sid_creates_session(self):
        r = await routes.h_start(self.ctx, {"init_data": self.init}, "2.2.2.2")
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(r["sid"], "sid1")

    async def test_missing_fields_give_understandable_errors(self):
        r = await routes.h_start(self.ctx, {}, "3.3.3.3")
        self.assertEqual((r["ok"], r["error"]), (False, "Please open this inside Telegram."))
        r = await routes.h_start(self.ctx, {"sid": "sid1"}, "3.3.3.4")            # sid but no initData
        self.assertFalse(r["ok"]); self.assertIn("Verification failed", r["error"])

    async def test_wrong_types_are_rejected_not_crashing(self):
        for bad in (123, ["sid1"], {"a": 1}, True):
            r = await routes.h_start(self.ctx, {"sid": bad, "init_data": 5}, "4.4.4.4")
            self.assertFalse(r["ok"], bad)

    async def test_invalid_and_foreign_telegram_auth_rejected(self):
        for init in ("", "hash=deadbeef", make_init_data(bot_token="999:OTHER"), make_init_data(age=7200), make_init_data(user_id=1)):
            r = await routes.h_start(self.ctx, {"sid": "sid1", "init_data": init}, "5.5.5.5")
            self.assertFalse(r["ok"], init[:20])
            self.assertEqual(r["code"], "session")

    async def test_valid_init_data_accepted(self):
        info = validate_init_data(make_init_data(), BOT_TOKEN)
        self.assertEqual(info["user"]["id"], UID)

    async def test_expired_or_unknown_session(self):
        r = await routes.h_start(self.ctx, {"sid": "nope", "init_data": self.init}, "6.6.6.6")
        self.assertEqual((r["ok"], r["code"]), (False, "session"))
        self.assertIn("Session expired", r["error"])                       # real expiry is still reported as such

    async def test_session_survives_repeat_and_reopen(self):
        a = await self.start()
        await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="skip"), "1.1.1.1")
        b = await self.start()                                             # reopen / retry with the same sid
        self.assertTrue(b["ok"], b); self.assertEqual(b["sid"], "sid1")

    async def test_concurrent_starts_only_one_ad_slot(self):
        rs = await asyncio.gather(*[routes.h_start(self.ctx, self.body(), f"9.9.9.{i}") for i in range(5)])
        self.assertEqual(sum(1 for r in rs if r["ok"]), 1, rs)
        self.assertTrue(all(r.get("code") == "busy" for r in rs if not r["ok"]))
        self.assertIsNotNone(await self.sess.get_session("sid1"))          # concurrency never invalidates the session

    async def test_no_provider_enabled(self):
        await store.save_config({"mode": "priority", "networks": {n: {"enabled": False} for n in settings.NETWORKS}}); engine.invalidate()
        r = await self.start()
        self.assertEqual((r["ok"], r["code"]), (False, "no_network"))


class FallbackTests(Base):
    async def fail(self, ad, reason="no_fill"):
        return await routes.h_report(self.ctx, self.body(ad_id=ad["ad_id"], event="fail", reason=reason), "1.1.1.1")

    async def test_monetag_unavailable_falls_back_to_tads(self):
        a = await self.start()
        r = await self.fail(a, "blocked")
        self.assertTrue(r["ok"] and r["next"]["ok"]); self.assertEqual(r["next"]["network"], "tads")
        self.assertEqual(r["next"]["unit_id"], "13070")

    async def test_both_unavailable_is_exhausted_not_infinite(self):
        a = await self.start()
        n = (await self.fail(a, "timeout"))["next"]
        r = await self.fail(n, "no_fill")
        self.assertTrue(r.get("exhausted")); self.assertNotIn("next", r)
        again = await self.start()                                         # user can retry afterwards
        self.assertTrue(again["ok"], again)

    async def test_no_second_ad_after_one_was_displayed(self):
        a = await self.start()
        await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="shown"), "1.1.1.1")
        r = await self.fail(a, "error")
        self.assertTrue(r.get("exhausted") and r.get("displayed")); self.assertNotIn("next", r)

    async def test_tads_not_offered_without_client_reward_permission(self):
        await store.save_config({"mode": "priority", "priority": ["monetag", "tads"],
                                 "networks": {"monetag": {"enabled": True}, "tads": {"enabled": True, "allow_client_reward": False}}}); engine.invalidate()
        a = await self.start()
        r = await self.fail(a, "no_fill")
        self.assertTrue(r.get("exhausted")); self.assertNotIn("next", r)


class RewardTests(Base):
    async def claim(self, ad, **kw): return await routes.h_done(self.ctx, self.body(ad_id=ad["ad_id"], completed=True, **kw), "1.1.1.1")

    async def shown_then_claim(self, ad):
        await routes.h_report(self.ctx, self.body(ad_id=ad["ad_id"], event="shown"), "1.1.1.1")
        return await self.claim(ad)

    async def test_verified_claim_unlocks_exactly_once(self):
        a = await self.start()
        r = await self.shown_then_claim(a)
        self.assertTrue(r["ok"] and r["delivered"], r)
        self.assertEqual(self.sent, ["tok1"])
        self.assertEqual(len(self.fdb["ad_rewards"].docs), 1)
        dup = await self.claim(a)                                          # duplicate callback / replay
        self.assertFalse(dup["ok"]); self.assertEqual(self.sent, ["tok1"])
        self.assertEqual(len(self.fdb["ad_rewards"].docs), 1)

    async def test_skipped_or_closed_ad_grants_nothing(self):
        a = await self.start()
        await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="shown"), "1.1.1.1")
        await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="skip"), "1.1.1.1")
        r = await self.claim(a)
        self.assertFalse(r["ok"]); self.assertEqual(self.sent, [])
        r = await routes.h_done(self.ctx, self.body(ad_id=a["ad_id"], completed=False), "1.1.1.1")
        self.assertFalse(r["ok"])

    async def test_client_cannot_claim_someone_elses_or_unknown_ad(self):
        r = await routes.h_done(self.ctx, self.body(ad_id="sid1.deadbeef", completed=True), "1.1.1.1")
        self.assertFalse(r["ok"]); self.assertEqual(self.sent, [])
        r = await routes.h_done(self.ctx, self.body(ad_id=["x"], completed=True), "1.1.1.1")
        self.assertFalse(r["ok"])

    async def test_completed_flag_must_be_literal_true(self):
        a = await self.start()
        for v in ("true", 1, None):
            r = await routes.h_done(self.ctx, self.body(ad_id=a["ad_id"], completed=v), "1.1.1.1")
            self.assertFalse(r["ok"])
            a = await self.start() if not (await store.get_attempt(a["ad_id"]) or {}).get("outcome") is None else a
        self.assertEqual(self.sent, [])

    async def test_tads_completion_only_counts_when_owner_allows_client_attestation(self):
        await self.sess.col.update_one({"sid": "sid1"}, {"$set": {"smart_active": None}})
        a = await self.start()
        n = (await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="fail", reason="no_fill"), "1.1.1.1"))["next"]
        r = await self.shown_then_claim(n)
        self.assertTrue(r["ok"] and r["delivered"], r)                      # allowed in this fixture (allow_client_reward=True)
        self.assertEqual(self.sent, ["tok1"])


class TadsDeniedTests(Base):
    async def test_tads_client_reward_flag_false_never_grants(self):
        await store.save_config({"mode": "priority", "priority": ["tads", "monetag"],
                                 "networks": {"tads": {"enabled": True, "allow_client_reward": False}, "monetag": {"enabled": False}}}); engine.invalidate()
        r = await self.start()
        self.assertEqual((r["ok"], r["code"]), (False, "no_network"))      # Tads can't be verified server-side => not served


class ExpiryCleanupTests(Base):
    async def test_stale_unfinished_attempts_are_cleaned_up(self):
        a = await self.start()
        await store._c("ad_requests").update_one({"_id": a["ad_id"]}, {"$set": {"t": time.time() - 3600}})
        self.assertEqual(await store.expire_stale(600), 1)
        self.assertEqual((await store.get_attempt(a["ad_id"]))["outcome"], "abandoned")
        await store.release_slot("sid1", a["ad_id"])


class ValidationPayloadTests(unittest.TestCase):
    ERR = [{"type": "missing", "loc": ("query", "request"), "msg": "Field required", "input": "SECRET-INITDATA"},
           {"type": "int_parsing", "loc": ("body", "n"), "msg": "Input should be a valid integer", "input": "hash=abc"}]

    def test_payload_lists_missing_and_invalid_fields_without_inputs(self):
        p = routes.validation_payload(self.ERR)
        self.assertFalse(p["ok"]); self.assertEqual(p["code"], "validation_error")
        self.assertIn("missing: query.request", p["error"]); self.assertIn("invalid: body.n", p["error"])
        self.assertNotIn("SECRET", json.dumps(p)); self.assertNotIn("hash=abc", json.dumps(p))

    def test_empty(self):
        self.assertIn("invalid request", routes.validation_payload([])["error"])


class FakeReq:
    def __init__(self, raw, ctype="application/json"):
        self._raw = raw; self.headers = {"content-type": ctype} if ctype else {}
        self.url = type("U", (), {"path": "/api/smart/start"})()
    async def body(self): return self._raw


class BodyParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_json(self):
        self.assertEqual(await routes.read_json_body(FakeReq(b'{"sid":"a"}')), ({"sid": "a"}, None))
        self.assertEqual((await routes.read_json_body(FakeReq(b'{"a":1}', "application/json; charset=utf-8")))[1], None)

    async def test_invalid_json(self):
        b, bad = await routes.read_json_body(FakeReq(b"{oops")); self.assertEqual(bad["code"], "invalid_json")

    async def test_form_data_mismatch(self):
        b, bad = await routes.read_json_body(FakeReq(b"sid=1&init_data=x", "application/x-www-form-urlencoded"))
        self.assertEqual(bad["code"], "bad_content_type"); self.assertIn("application/json", bad["error"])

    async def test_non_object(self):
        b, bad = await routes.read_json_body(FakeReq(b"[1,2]")); self.assertEqual(bad["code"], "invalid_json")

    async def test_empty_body_defers_to_field_checks(self):
        self.assertEqual(await routes.read_json_body(FakeReq(b"", None)), ({}, None))


if __name__ == "__main__":
    unittest.main()
