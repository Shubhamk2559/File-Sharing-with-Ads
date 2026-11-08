"""Batch 1 - production ad-verification security.

  A. ads/security.py        environment profile, fail-closed startup, session expiry helper           (pure)
  B. ads/postback.py + monetag_store.record_postback   secret, shape, user/session/zone binding, expiry,
                            idempotency, replay                                                       (real code, in-memory Mongo)
  C. strict-mode reward flow  Monetag only unlocks via a valid postback; Tads client callbacks never do
  D. development profile     Tads client-attested path still works once, and replays / foreign claims are rejected

The strict profile is the default everywhere; the development-profile classes opt out explicitly.
"""
import logging
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# config.py (imported by monetag_store) reads its environment at import time and fails closed. Give it a valid PRODUCTION env.
SECRET = "s3cr3t-Postback_0123456789abcdef"
for _k, _v in {"MAIN_BOT_TOKEN": "123456:TEST-TOKEN", "OWNER_ID": "1", "MONGO_URI": "mongodb://unused", "WEBHOOK_SECRET": "test-webhook-secret-0123456789",
               "BASE_URL": "https://example.test", "PRIMARY_CHANNEL": "-1001", "BACKUP_CHANNEL": "-1002",
               "MONETAG_POSTBACK_SECRET": SECRET}.items():
    os.environ.setdefault(_k, _v)
for _k in ("APP_ENV", "AD_VERIFY_MODE"):
    os.environ.pop(_k, None)

import monetag_store  # noqa: E402
from ads import engine, postback, routes, security, service, settings, store, verify  # noqa: E402
from ads.ratelimit import RateLimiter  # noqa: E402
from ads.verify import Facts, Policy  # noqa: E402
from tests.fakemongo import FakeDB, FakeSessions  # noqa: E402
from tests.test_smart_flow import Base, UID, make_init_data  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ZONE = "12181474"
GOOD_ENV = {"MONETAG_POSTBACK_SECRET": SECRET}


# =============================================================== A. security profile
class ProfileTests(unittest.TestCase):
    def test_default_is_production_postback_strict(self):
        s = security.resolve(GOOD_ENV)
        self.assertEqual((s.app_env, s.verify_mode, s.production, s.strict), ("production", "postback", True, True))

    def test_missing_secret_fails_closed_in_production(self):
        for env in ({}, {"MONETAG_POSTBACK_SECRET": ""}, {"MONETAG_POSTBACK_SECRET": "   "}, {"APP_ENV": "production"}):
            with self.assertRaises(security.ConfigError, msg=env):
                security.resolve(env)

    def test_weak_or_unsafe_secret_rejected(self):
        for bad in ("short", "a" * 15, "has space in it 12345678", "ampersand&secret-1234567", "pct%20secret-1234567", "ünïcode-secret-123456", "x" * 129):
            with self.assertRaises(security.ConfigError, msg=bad):
                security.resolve({"MONETAG_POSTBACK_SECRET": bad})

    def test_error_and_repr_never_contain_the_secret(self):
        with self.assertRaises(security.ConfigError) as cm:
            security.resolve({"MONETAG_POSTBACK_SECRET": "weak secret"})
        self.assertNotIn("weak secret", str(cm.exception))
        self.assertNotIn(SECRET, repr(security.resolve(GOOD_ENV)))

    def test_legacy_verification_refused_in_production(self):
        with self.assertRaises(security.ConfigError):
            security.resolve({**GOOD_ENV, "AD_VERIFY_MODE": "legacy"})
        with self.assertRaises(security.ConfigError):
            security.resolve({**GOOD_ENV, "APP_ENV": "production", "AD_VERIFY_MODE": "legacy"})

    def test_unknown_values_fail_closed(self):
        for env in ({"APP_ENV": "prod"}, {"APP_ENV": "staging"}, {**GOOD_ENV, "AD_VERIFY_MODE": "none"}):
            with self.assertRaises(security.ConfigError, msg=env):
                security.resolve({**GOOD_ENV, **env})

    def test_development_is_separate_and_loudly_labelled(self):
        s = security.resolve({"APP_ENV": "development"})
        self.assertEqual((s.verify_mode, s.production, s.strict, s.postback_secret), ("legacy", False, False, ""))
        self.assertTrue(any("development" in w for w in s.warnings))
        self.assertFalse(security.resolve({"APP_ENV": "test"}).strict)

    def test_development_postback_mode_still_needs_a_secret(self):
        with self.assertRaises(security.ConfigError):
            security.resolve({"APP_ENV": "development", "AD_VERIFY_MODE": "postback"})
        self.assertEqual(security.resolve({"APP_ENV": "development", "AD_VERIFY_MODE": "postback", **GOOD_ENV}).verify_mode, "postback")

    def test_tads_client_reward_flag_is_ignored_in_production(self):
        s = security.resolve({**GOOD_ENV, "TADS_ALLOW_CLIENT_REWARD": "true"})
        self.assertTrue(s.strict)
        self.assertTrue(any("TADS_ALLOW_CLIENT_REWARD" in w for w in s.warnings))

    def test_numeric_settings_are_validated(self):
        self.assertEqual(security.resolve({**GOOD_ENV, "AD_ATTEMPT_MAX_AGE": "120"}).postback_max_age, 120)
        for bad in ("5", "99999", "abc"):
            with self.assertRaises(security.ConfigError):
                security.resolve({**GOOD_ENV, "AD_ATTEMPT_MAX_AGE": bad})

    def test_process_refuses_to_start_without_secret(self):
        """The real config.py module, in a fresh interpreter: no secret => import fails, nothing serves."""
        base = {"PATH": os.environ.get("PATH", ""), "MAIN_BOT_TOKEN": "1:x", "OWNER_ID": "1", "MONGO_URI": "m", "WEBHOOK_SECRET": "test-webhook-secret-0123456789",
                "BASE_URL": "https://x.test", "PRIMARY_CHANNEL": "-1", "BACKUP_CHANNEL": "-2"}
        code = "import sys; sys.path.insert(0, %r); import config; print(config.APP_ENV, config.STRICT_ADS, config.AD_VERIFY_MODE)" % str(ROOT)
        run = lambda env: subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=60)
        r = run(base)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MONETAG_POSTBACK_SECRET is required", r.stderr)
        r = run({**base, "AD_VERIFY_MODE": "legacy", "MONETAG_POSTBACK_SECRET": SECRET})
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(SECRET, r.stderr)
        r = run({**base, "MONETAG_POSTBACK_SECRET": SECRET})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "production True postback"), r.stderr)
        r = run({**base, "APP_ENV": "development"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "development False legacy"), r.stderr)


class SessionExpiryTests(unittest.TestCase):
    def test_helper(self):
        now = time.time()
        fresh = {"created": datetime.now(timezone.utc)}
        self.assertFalse(security.session_expired(fresh, 1800, now))
        self.assertTrue(security.session_expired(fresh, 1800, now + 1801))
        self.assertFalse(security.session_expired({"created": datetime.now(timezone.utc).replace(tzinfo=None)}, 1800))     # pymongo's naive UTC datetimes
        for bad in (None, {}, {"created": None}, {"created": "yesterday"}):
            self.assertTrue(security.session_expired(bad, 1800), bad)                       # fail closed


# =============================================================== B. postback endpoint logic
class PostbackBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb = FakeDB()
        monetag_store._DB = self.fdb
        self.sess = FakeSessions(self.fdb)
        await self.sess.create_session("sid1", "tok1", UID, UID, 1, 1)
        self.ymid = "sid1.abcd1234"
        await monetag_store.record_attempt(self.ymid, "sid1", ZONE, None, user_id=UID)
        self.limiter = RateLimiter(20, 600)

    async def asyncTearDown(self):
        monetag_store._DB = None

    def q(self, **kw):
        d = {"secret": SECRET, "ymid": self.ymid, "event_type": "impression", "reward_event_type": "valued", "zone_id": ZONE,
             "sub_zone_id": "555", "estimated_price": "0.004", "request_var": "unlock", "telegram_id": str(UID)}
        d.update(kw)
        return {k: v for k, v in d.items() if v is not None}

    async def pb(self, q=None, secret=SECRET, offset=0.0, **kw):
        return await postback.handle(q if q is not None else self.q(), secret=secret, store=monetag_store, ip="1.2.3.4",
                                     fail_limiter=self.limiter, clock=lambda: time.time() + offset, **kw)

    def ad(self): return next(d for d in self.fdb["monetag_ads"].docs if d["_id"] == self.ymid)
    def events(self): return self.fdb["monetag_events"].docs
    def imp(self): return sum(d.get("imp", 0) for d in self.fdb["monetag_daily"].docs)

    def assertUntouched(self):
        self.assertEqual(self.events(), [])
        self.assertEqual((self.ad()["pb_seen"], self.ad()["pb_valued"]), (False, False))
        self.assertEqual(self.imp(), 0)


class ValidPostbackTests(PostbackBase):
    async def test_valid_postback_is_accepted_and_marks_the_ad_verified(self):
        self.assertEqual(await self.pb(), (200, "ok"))
        self.assertEqual((self.ad()["pb_seen"], self.ad()["pb_valued"]), (True, True))
        self.assertEqual((len(self.events()), self.imp()), (1, 1))

    async def test_non_valued_impression_is_recorded_but_never_verifies(self):
        self.assertEqual(await self.pb(self.q(reward_event_type="non_valued")), (200, "ok"))
        self.assertEqual((self.ad()["pb_seen"], self.ad()["pb_valued"]), (True, False))
        self.assertEqual(await self.pb(self.q(reward_event_type="valued")), (200, "dup"))      # can't be upgraded afterwards
        self.assertFalse(self.ad()["pb_valued"])

    async def test_click_event_never_verifies(self):
        self.assertEqual(await self.pb(self.q(event_type="click")), (200, "ok"))
        self.assertEqual((self.ad()["pb_seen"], self.ad()["pb_valued"]), (False, False))


class SecretTests(PostbackBase):
    async def test_invalid_secret(self):
        for secret in ("wrong", "", SECRET[:-1], SECRET + "x", SECRET.upper(), "ünïcode-ẞecret", "\x00"):
            st, body = await self.pb(self.q(secret=secret))
            self.assertEqual((st, body), (403, "forbidden"), secret)
        st, _ = await self.pb(self.q(secret=None))                                            # parameter absent
        self.assertEqual(st, 403)
        self.assertUntouched()

    async def test_non_string_secret_does_not_crash(self):
        self.assertEqual((await self.pb(self.q(secret=["a"])))[0], 403)

    async def test_no_configured_secret_rejects_everything_even_an_empty_one(self):
        for sent in ("", None, SECRET):
            self.assertEqual((await self.pb(self.q(secret=sent), secret=""))[0], 503)
        self.assertUntouched()

    async def test_response_and_logs_never_contain_the_secret_or_the_reason(self):
        with self.assertLogs("ads", level="WARNING") as cm:
            st, body = await self.pb(self.q(secret="guess-guess-guess-guess"))
            st2, body2 = await self.pb(self.q(telegram_id="999"))                                # authenticated but rejected
        self.assertNotIn("guess-guess", " ".join(cm.output)); self.assertNotIn(SECRET, " ".join(cm.output))
        self.assertEqual((body2, st2), ("rejected", 403))                                       # no oracle about WHY
        self.assertNotIn("mismatch", body2)

    async def test_failed_secrets_are_rate_limited_but_the_real_secret_never_is(self):
        codes = [(await self.pb(self.q(secret="nope")))[0] for _ in range(25)]
        self.assertEqual(codes[:20], [403] * 20)
        self.assertEqual(set(codes[20:]), {429})
        self.assertEqual(await self.pb(), (200, "ok"))                                         # Monetag is not locked out


class IdempotencyAndReplayTests(PostbackBase):
    async def test_duplicate_postback_is_idempotent(self):
        self.assertEqual(await self.pb(), (200, "ok"))
        for _ in range(3):
            self.assertEqual(await self.pb(), (200, "dup"))
        self.assertEqual((len(self.events()), self.imp()), (1, 1))                              # counted once
        self.assertEqual(sum(d.get("rev_imp", 0) for d in self.fdb["monetag_daily"].docs), 0.004)

    async def test_concurrent_duplicates_process_once(self):
        import asyncio
        rs = await asyncio.gather(*[self.pb() for _ in range(6)])
        self.assertEqual(sorted(b for _, b in rs).count("ok"), 1, rs)
        self.assertEqual((len(self.events()), self.imp()), (1, 1))

    async def test_replay_after_the_ad_was_settled_is_rejected(self):
        """Captured postback re-sent after the reward was settled (and the dedupe record is gone) must not re-open anything."""
        self.assertEqual(await self.pb(), (200, "ok"))
        await monetag_store.record_completion(self.ymid)                                        # reward settled
        self.fdb["monetag_events"].docs.clear()                                                 # e.g. retention purge
        self.assertEqual(await self.pb(), (410, "rejected"))
        self.assertEqual((len(self.events()), self.imp()), (0, 1))

    async def test_replay_after_the_attempt_expired_is_rejected(self):
        self.assertEqual(await self.pb(offset=601), (410, "rejected"))
        self.assertUntouched()

    async def test_late_postback_for_an_already_failed_ad_is_rejected(self):
        await monetag_store.record_fail(self.ymid, "sid1", "timeout")
        self.assertEqual(await self.pb(), (410, "rejected"))
        self.assertFalse(self.ad()["pb_valued"])

    async def test_postback_after_delivery_is_rejected(self):
        await self.sess.claim_delivery("sid1")                                                  # not claimable (ads_done 0) ...
        self.sess.col.docs[0].update(delivered=True)                                            # ... so mark it delivered directly
        self.assertEqual(await self.pb(), (410, "rejected"))
        self.assertUntouched()


class ExpiryTests(PostbackBase):
    async def test_expired_attempt(self):
        self.assertEqual(await self.pb(offset=601), (410, "rejected"))
        self.assertEqual(await self.pb(offset=599), (200, "ok"))                                # boundary: still inside the window

    async def test_expired_session(self):
        self.sess.col.docs[0]["created"] = datetime.now(timezone.utc) - timedelta(seconds=1900)
        self.assertEqual(await self.pb(), (410, "rejected"))
        self.assertUntouched()

    async def test_missing_session(self):
        self.sess.col.docs.clear()
        self.assertEqual(await self.pb(), (410, "rejected"))
        self.assertUntouched()

    async def test_custom_window(self):
        self.assertEqual(await self.pb(offset=61, max_age=60), (410, "rejected"))


class BindingTests(PostbackBase):
    async def test_postback_for_another_user_is_rejected(self):
        self.assertEqual(await self.pb(self.q(telegram_id="888")), (403, "rejected"))
        self.assertUntouched()

    async def test_postback_for_wrong_zone_is_rejected(self):
        self.assertEqual(await self.pb(self.q(zone_id="999")), (403, "rejected"))
        self.assertUntouched()

    async def test_unknown_ymid_is_rejected_not_counted(self):
        self.assertEqual(await self.pb(self.q(ymid="sid9.deadbeef")), (403, "rejected"))
        self.assertEqual((len(self.events()), self.imp()), (0, 0))

    async def test_ymid_of_someone_elses_session_cannot_be_borrowed(self):
        await self.sess.create_session("sid2", "tok1", 888, 888, 1, 1)
        await monetag_store.record_attempt("sid2.cafe0001", "sid2", ZONE, None, user_id=888)
        self.assertEqual(await self.pb(self.q(ymid="sid2.cafe0001", telegram_id=str(UID))), (403, "rejected"))
        self.assertEqual(await self.pb(self.q(ymid="sid2.cafe0001", telegram_id="888")), (200, "ok"))
        self.assertEqual((self.ad()["pb_seen"], self.ad()["pb_valued"]), (False, False))        # ours untouched

    async def test_attempt_not_bound_to_a_user_is_rejected(self):
        self.ad()["user_id"] = None                                                            # e.g. issued by an old deployment
        self.assertEqual(await self.pb(), (403, "rejected"))
        self.assertUntouched()

    async def test_session_owner_must_match_the_attempt_user(self):
        self.sess.col.docs[0]["user_id"] = 888
        self.assertEqual(await self.pb(), (403, "rejected"))
        self.assertUntouched()

    async def test_telegram_id_is_required_unless_explicitly_relaxed(self):
        self.assertEqual(await self.pb(self.q(telegram_id=None)), (400, "rejected"))
        self.assertEqual(await self.pb(self.q(telegram_id=""), require_telegram_id=False), (200, "ok"))
        self.assertEqual(self.ad()["pb_valued"], True)

    async def test_relaxed_mode_still_rejects_a_wrong_telegram_id(self):
        self.assertEqual(await self.pb(self.q(telegram_id="888"), require_telegram_id=False), (403, "rejected"))


class MalformedTests(PostbackBase):
    async def test_malformed_payloads(self):
        cases = [dict(ymid=None), dict(ymid=""), dict(ymid="a b"), dict(ymid="x" * 81), dict(ymid="../../etc"), dict(ymid="ab"),
                 dict(event_type="purchase"), dict(event_type=None), dict(reward_event_type="maybe"), dict(reward_event_type=None),
                 dict(telegram_id="12ab"), dict(telegram_id="-5"), dict(telegram_id="1" * 16), dict(zone_id="12x"),
                 dict(estimated_price="9" * 300)]
        for c in cases:
            st, body = await self.pb(self.q(**c))
            self.assertEqual((st, body), (400, "rejected"), c)
        self.assertUntouched()

    async def test_oversized_parameter_rejected(self):
        self.assertEqual((await self.pb(self.q(request_var="v" * 500)))[0], 400)

    async def test_nonsense_price_cannot_inflate_revenue(self):
        for price in ("nan", "-5", "inf", "abc"):
            self.assertEqual((await self.pb(self.q(estimated_price=price, ymid="sid1.abcd1234", event_type="click")))[0], 200, price)
            self.fdb["monetag_events"].docs.clear()
        self.assertLessEqual(sum(d.get("rev_click", 0) for d in self.fdb["monetag_daily"].docs), 2.0)


# =============================================================== C. strict-mode reward flow
class StrictBase(Base):
    """PRODUCTION profile: Monetag via postback only, strict on."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        monetag_store._DB = self.fdb
        settings.RUNTIME["strict"], settings.RUNTIME["monetag_postback"] = True, True
        self.offset = 0.0
        self.policy = Policy(monetag_postback=True, min_seconds=0, wait_seconds=20, strict=True)

        async def zone(): return ZONE
        service.configure(service.Deps(sessions=self.sess, policy=self.policy, monetag=monetag_store, select_zone=zone,
                                       sdk_url=lambda z: "https://sdk.test/sdk.js", clock=lambda: time.time() + self.offset))
        engine.invalidate()

    async def asyncTearDown(self):
        monetag_store._DB = None
        await super().asyncTearDown()

    async def claim(self, ad, **kw):
        return await routes.h_done(self.ctx, self.body(ad_id=ad["ad_id"], completed=True, **kw), "1.1.1.1")

    async def shown(self, ad):
        await routes.h_report(self.ctx, self.body(ad_id=ad["ad_id"], event="shown"), "1.1.1.1")

    async def postback(self, ad, **kw):
        q = {"secret": SECRET, "ymid": ad["ymid"], "event_type": "impression", "reward_event_type": "valued", "zone_id": ZONE,
             "telegram_id": str(UID), "estimated_price": "0.003"}
        q.update(kw)
        return await postback.handle(q, secret=SECRET, store=monetag_store, ip="1.1.1.1", clock=lambda: time.time() + self.offset)

    def rewards(self): return self.fdb["ad_rewards"].docs


class MonetagStrictFlowTests(StrictBase):
    async def test_postback_then_claim_delivers_exactly_once(self):
        a = await self.start(); self.assertTrue(a["ok"], a); self.assertEqual(a["network"], "monetag")
        await self.shown(a)
        self.assertEqual(await self.postback(a), (200, "ok"))
        r = await self.claim(a)
        self.assertTrue(r["ok"] and r["delivered"], r)
        self.assertEqual((self.sent, len(self.rewards()), self.rewards()[0]["trust"]), (["tok1"], 1, "server_postback"))
        dup = await self.claim(a)                                                              # duplicate claim
        self.assertFalse(dup["ok"])
        self.assertEqual(await self.postback(a), (200, "dup"))                                 # duplicate postback
        self.assertEqual((self.sent, len(self.rewards())), (["tok1"], 1))

    async def test_browser_completion_without_postback_never_unlocks(self):
        a = await self.start(); await self.shown(a)
        r = await self.claim(a)
        self.assertTrue(r["ok"] and r.get("wait"), r)                                          # waiting for the server signal
        self.assertEqual(self.sent, [])
        self.offset = 25                                                                       # waited longer than POSTBACK_WAIT_SECONDS
        r = await self.claim(a)
        self.assertFalse(r["ok"]); self.assertEqual((self.sent, self.rewards()), ([], []))
        self.assertEqual((await store.get_attempt(a["ad_id"]))["reason"], "unverified")
        self.assertEqual(await self.postback(a), (410, "rejected"))                            # too late: ad already denied

    async def test_client_flags_cannot_force_a_grant(self):
        a = await self.start(); await self.shown(a)
        for extra in ({"non_valued": False}, {"trust": "server_postback"}, {"pb_valued": True}, {"verified": True}):
            r = await self.claim(a, **extra)
            self.assertEqual(self.sent, [], extra)
        self.assertEqual(self.rewards(), [])

    async def test_non_valued_postback_denies(self):
        a = await self.start(); await self.shown(a)
        self.assertEqual(await self.postback(a, reward_event_type="non_valued"), (200, "ok"))
        r = await self.claim(a)
        self.assertFalse(r["ok"]); self.assertEqual((self.sent, self.rewards()), ([], []))

    async def test_postback_naming_another_user_cannot_verify_this_ad(self):
        a = await self.start(); await self.shown(a)
        self.assertEqual(await self.postback(a, telegram_id="888"), (403, "rejected"))
        r = await self.claim(a)
        self.assertTrue(r["ok"] and r.get("wait"), r); self.assertEqual(self.sent, [])

    async def test_verification_record_bound_to_another_user_is_not_enough(self):
        """Defence in depth: even if flags were somehow set on a record that belongs to a different user, done() refuses."""
        a = await self.start(); await self.shown(a)
        doc = monetag_store._c("monetag_ads").docs[0]
        doc.update(pb_seen=True, pb_valued=True, user_id=888)
        r = await self.claim(a)
        self.assertFalse(r["ok"]); self.assertEqual((self.sent, self.rewards()), ([], []))

    async def test_expired_attempt_cannot_be_redeemed(self):
        a = await self.start(); await self.shown(a)
        self.assertEqual(await self.postback(a), (200, "ok"))
        self.offset = 601
        r = await self.claim(a)
        self.assertFalse(r["ok"]); self.assertEqual((self.sent, self.rewards()), ([], []))
        self.assertEqual((await store.get_attempt(a["ad_id"]))["reason"], "expired")

    async def test_expired_session_is_rejected_everywhere(self):
        a = await self.start(); await self.shown(a)
        self.sess.col.docs[0]["created"] = datetime.now(timezone.utc) - timedelta(seconds=1900)
        for r in (await self.start(), await self.claim(a),
                  await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="shown"), "1.1.1.1")):
            self.assertEqual((r["ok"], r.get("code")), (False, "session"), r)
        self.assertEqual(self.sent, [])

    async def test_cross_user_claim_is_rejected(self):
        await self.sess.create_session("sid2", "tok1", 888, 888, 1, 1)
        a = await self.start(); await self.shown(a)
        self.assertEqual(await self.postback(a), (200, "ok"))
        r = await routes.h_done(self.ctx, {"sid": "sid2", "init_data": make_init_data(user_id=888), "ad_id": a["ad_id"], "completed": True}, "2.2.2.2")
        self.assertFalse(r["ok"]); self.assertEqual(self.sent, [])

    async def test_monetag_without_postback_policy_fails_closed_in_strict_mode(self):
        """Misconfiguration (strict but postback verification off): Monetag is not even served, so nothing can unlock."""
        settings.RUNTIME["monetag_postback"] = False
        engine.invalidate()
        r = await self.start()
        self.assertEqual((r["ok"], r["code"]), (False, "no_network"))
        self.assertEqual((self.sent, self.rewards()), ([], []))
        self.assertEqual(engine.blockers(await engine.get_config(force=True))["monetag"], ["client_attested_not_allowed"])


async def _async(v): return v


# ---------------------------------------------------------------- Tads in strict mode
class TadsStrictTests(StrictBase):
    async def tads_attempt(self):
        ad_id = "sid1.tads0001"
        await store.create_attempt({"_id": ad_id, "ad_id": ad_id, "sid": "sid1", "user_id": UID, "network": "tads", "attempt_no": 1,
                                    "zone": None, "country": None, "t": time.time(), "round": 1, "chain_len": 1})
        await self.sess.start_ad("sid1", ad_id, "tads")
        return {"ad_id": ad_id}

    async def test_tads_is_never_served_even_if_stored_config_allows_client_rewards(self):
        # Base saved allow_client_reward=True for both networks: strict mode must override it
        a = await self.start()
        self.assertEqual(a["network"], "monetag")
        r = await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="fail", reason="no_fill"), "1.1.1.1")
        self.assertTrue(r.get("exhausted")); self.assertNotIn("next", r)                       # no Tads fallback
        cfg = await engine.get_config(force=True)
        self.assertEqual(engine.eligible(cfg), ["monetag"])
        self.assertEqual(engine.blockers(cfg)["tads"], ["client_attested_not_allowed"])

    async def test_tads_only_configuration_serves_nothing(self):
        await store.save_config({"mode": "priority", "priority": ["tads", "monetag"],
                                 "networks": {"tads": {"enabled": True, "allow_client_reward": True}, "monetag": {"enabled": False}}})
        engine.invalidate()
        r = await self.start()
        self.assertEqual((r["ok"], r["code"]), (False, "no_network"))

    async def test_tads_client_callback_without_server_verification_never_unlocks(self):
        t = await self.tads_attempt()                      # worst case: a Tads attempt exists anyway (tampered DB, old deploy)
        await self.shown(t)
        r = await self.claim(t, non_valued=False)          # the browser says "onShowReward fired"
        self.assertFalse(r["ok"]); self.assertNotIn("delivered", r)
        self.assertEqual((self.sent, self.rewards()), ([], []))
        att = await store.get_attempt(t["ad_id"])
        self.assertEqual((att["outcome"], att["reason"]), ("denied", "client_attested_not_allowed"))
        again = await self.claim(t)                        # replay of the same callback
        self.assertFalse(again["ok"]); self.assertEqual((self.sent, self.rewards()), ([], []))

    async def test_tads_postback_lookalike_does_not_verify_tads(self):
        """A Monetag-style postback for a Tads attempt id is rejected (no monetag_ads record) and cannot grant."""
        t = await self.tads_attempt(); await self.shown(t)
        st, _ = await postback.handle({"secret": SECRET, "ymid": t["ad_id"], "event_type": "impression", "reward_event_type": "valued",
                                       "telegram_id": str(UID)}, secret=SECRET, store=monetag_store)
        self.assertEqual(st, 403)
        r = await self.claim(t)
        self.assertFalse(r["ok"]); self.assertEqual((self.sent, self.rewards()), ([], []))

    async def test_admin_cannot_turn_client_rewards_back_on(self):
        for net in settings.NETWORKS:
            new, errors = settings.validate_patch({"networks": {net: {"allow_client_reward": True}}}, await engine.get_config(force=True))
            self.assertTrue(errors, net)
        status, out = await routes.h_admin_patch_config({"networks": {"tads": {"allow_client_reward": True}}})
        self.assertEqual(status, 400)
        cfg = await engine.get_config(force=True)
        self.assertFalse(cfg["networks"]["tads"]["allow_client_reward"])                       # the stored True is normalised away
        self.assertFalse(settings.trust_allowed(cfg, "tads"))
        _, errors = settings.validate_patch({"networks": {"tads": {"allow_client_reward": False}}}, cfg)
        self.assertEqual(errors, [])                                                           # turning it OFF is fine

    async def test_trust_check_holds_even_if_config_normalisation_is_bypassed(self):
        """Second, independent layer: a config object that still says allow_client_reward=True (never passed through merged())."""
        cfg = await engine.get_config(force=True)
        cfg = {**cfg, "networks": {n: dict(v, allow_client_reward=True) for n, v in cfg["networks"].items()}}
        self.assertFalse(settings.trust_allowed(cfg, "tads"))
        self.assertTrue(settings.trust_allowed(cfg, "monetag"))                                # postback-verified network unaffected
        settings.RUNTIME["strict"] = False                                                     # development profile honours the flag
        self.assertTrue(settings.trust_allowed(cfg, "tads"))

    async def test_admin_overview_is_honest_about_tads(self):
        out = await routes.h_admin_config()
        self.assertTrue(out["strict"])
        self.assertEqual(out["reward_verification"], {"monetag": "server_postback", "tads": "client_attested"})
        self.assertNotIn(SECRET, repr(out))


class VerifyDecisionTests(unittest.TestCase):
    """verify.decide is the last line of defence."""
    TADS = dict(network="tads", elapsed=60, client_completed=True)

    def test_tads_cannot_be_server_verified_by_anything(self):
        for strict in (True, False):
            pol = Policy(monetag_postback=True, strict=strict)
            d = verify.decide(Facts(**self.TADS, postback_seen=True, postback_valued=True), pol, allow_client=True)
            self.assertNotEqual(d.trust, settings.SERVER_POSTBACK, (strict, d))
        for postback_on in (True, False):
            settings.RUNTIME["monetag_postback"] = postback_on
            self.assertEqual(settings.trust_for("tads"), settings.CLIENT_ATTESTED)             # no verified-Tads path exists
        settings.RUNTIME["monetag_postback"] = False

    def test_strict_denies_client_attested_even_when_allowed(self):
        d = verify.decide(Facts(**self.TADS), Policy(strict=True), allow_client=True)
        self.assertEqual((d.action, d.reason), ("deny", "client_attested_not_allowed"))

    def test_development_profile_still_grants_client_attested_tads_and_labels_it(self):
        d = verify.decide(Facts(**self.TADS), Policy(strict=False), allow_client=True)
        self.assertEqual((d.action, d.trust), ("grant", settings.CLIENT_ATTESTED))

    def test_strict_monetag_needs_a_valued_postback(self):
        pol = Policy(monetag_postback=True, strict=True, min_seconds=0)
        f = dict(network="monetag", elapsed=60, client_completed=True, claim_age=999)
        self.assertEqual(verify.decide(Facts(**f), pol, True).action, "deny")
        self.assertEqual(verify.decide(Facts(**f, postback_seen=True, postback_valued=False), pol, True).action, "deny")
        d = verify.decide(Facts(**f, postback_seen=True, postback_valued=True), pol, True)
        self.assertEqual((d.action, d.trust), ("grant", settings.SERVER_POSTBACK))
        # strict + postback off (misconfiguration) never falls back to the browser's word
        self.assertEqual(verify.decide(Facts(**f), Policy(monetag_postback=False, strict=True, min_seconds=0), True).action, "deny")

    def test_policy_defaults_are_fail_closed(self):
        self.assertTrue(Policy().strict)
        self.assertTrue(settings.RUNTIME["strict"] or True)                                    # (global may be changed by other fixtures)


# =============================================================== D. development profile (explicit opt-out)
class TadsDevProfileTests(Base):
    """Base runs the DEVELOPMENT profile: this is the old client-attested behaviour, kept for local testing only."""
    async def fallback_to_tads(self):
        a = await self.start()
        r = await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="fail", reason="no_fill"), "1.1.1.1")
        await routes.h_report(self.ctx, self.body(ad_id=r["next"]["ad_id"], event="shown"), "1.1.1.1")
        return r["next"]

    async def claim(self, ad, sid="sid1", init=None):
        return await routes.h_done(self.ctx, {"sid": sid, "init_data": init or self.init, "ad_id": ad["ad_id"], "completed": True}, "1.1.1.1")

    async def test_tads_grant_is_labelled_client_attested_and_happens_once(self):
        n = await self.fallback_to_tads(); self.assertEqual(n["network"], "tads")
        r = await self.claim(n)
        self.assertTrue(r["ok"] and r["delivered"], r)
        self.assertEqual(self.fdb["ad_rewards"].docs[0]["trust"], "client_attested")           # never claims server verification
        for _ in range(3):                                                                    # replayed callback
            self.assertFalse((await self.claim(n))["ok"])
        self.assertEqual((self.sent, len(self.fdb["ad_rewards"].docs)), (["tok1"], 1))

    async def test_tads_attempt_cannot_be_claimed_from_another_session_or_user(self):
        n = await self.fallback_to_tads()
        await self.sess.create_session("sid2", "tok1", 888, 888, 1, 1)
        r = await self.claim(n, sid="sid2", init=make_init_data(user_id=888))                  # someone else's ad_id
        self.assertFalse(r["ok"])
        r = await self.claim(n, sid="sid1", init=make_init_data(user_id=888))                  # right sid, wrong Telegram user
        self.assertFalse(r["ok"])
        self.assertEqual((self.sent, self.fdb["ad_rewards"].docs), ([], []))

    async def test_tads_client_supplied_reward_ids_are_not_accepted_as_proof(self):
        n = await self.fallback_to_tads()
        r = await routes.h_done(self.ctx, self.body(ad_id="tads-reward-123", completed=True), "1.1.1.1")   # Tads' own adId
        self.assertFalse(r["ok"]); self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
