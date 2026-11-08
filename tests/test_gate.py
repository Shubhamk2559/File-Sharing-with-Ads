"""Ad gate: Monetag -> Tads rounds, server-verified ad-blocker challenge, no-ad grace. Real service/store/gate/routes handlers
against the in-memory Mongo fake."""
import re
import time
import unittest

from ads import engine, gate, routes, settings
from tests.test_smart_flow import Base

IP = "9.9.9.9"


class GateBase(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._g = (settings.GATE_ROUNDS, settings.GATE_GRACE, settings.GATE_MIN_TOTAL_SECONDS,
                   settings.GATE_GRACE_MAX_PER_HOUR, settings.GATE_CHALLENGE_TTL, settings.GATE_GRACE_REASONS)
        settings.GATE_ROUNDS, settings.GATE_GRACE, settings.GATE_MIN_TOTAL_SECONDS = 3, True, 0
        settings.GATE_GRACE_MAX_PER_HOUR, settings.GATE_CHALLENGE_TTL = 20, 90
        settings.GATE_GRACE_REASONS = frozenset({"no_fill", "timeout"})
        gate.configure("unit-test-secret")

    async def asyncTearDown(self):
        (settings.GATE_ROUNDS, settings.GATE_GRACE, settings.GATE_MIN_TOTAL_SECONDS, settings.GATE_GRACE_MAX_PER_HOUR,
         settings.GATE_CHALLENGE_TTL, settings.GATE_GRACE_REASONS) = self._g
        gate.configure("")
        await super().asyncTearDown()

    async def fail_round(self, reasons=("no_fill", "no_fill"), shown_first=False):
        """One full round: Monetag fails, Tads is handed over, Tads fails. Returns the final report."""
        ad = await self.start()
        assert ad["ok"], ad
        i = 0
        while True:
            if shown_first and i == 0:
                await routes.h_report(self.ctx, self.body(ad_id=ad["ad_id"], event="shown"), IP)
            rep = await routes.h_report(self.ctx, self.body(ad_id=ad["ad_id"], event="fail", reason=reasons[min(i, len(reasons) - 1)]), IP)
            i += 1
            if rep.get("next"):
                ad = rep["next"]
                continue
            return rep

    async def rounds(self, n=3, **kw):
        for _ in range(n):
            await self.fail_round(**kw)

    async def solve(self, skip=(), corrupt=()):
        ch = await routes.h_challenge(self.ctx, self.body(), IP)
        self.assertTrue(ch["ok"], ch)
        proofs = {}
        for b in ch["baits"]:
            if b["k"] in skip:
                continue
            path, q = b["u"].split("?")
            p = dict(x.split("=") for x in q.split("&"))
            kind, out = await gate.bait_response(path, p["c"], p["k"])
            if kind == "script":
                proofs[b["k"]] = re.search(r'"%s"\]="([0-9a-f]+)"' % b["k"], out).group(1)
            else:
                proofs[b["k"]] = True
            if b["k"] in corrupt:
                proofs[b["k"]] = "0" * 24
        return ch["cid"], proofs

    async def grace(self, cid, proofs, **extra):
        return await routes.h_grace(self.ctx, self.body(cid=cid, proofs=proofs, **extra), IP)


class FlowOrderTests(GateBase):
    async def test_defaults_are_production_gate(self):
        self.assertEqual(settings.DEFAULT_CONFIG["mode"], "priority")            # Monetag first, Tads fallback
        self.assertTrue(settings.SMART_ADS_ENABLED)
        self.assertEqual(settings.GATE_ROUNDS, 3)

    async def test_round_is_monetag_then_tads_and_counts_server_side(self):
        a = await self.start()
        self.assertEqual(a["network"], "monetag")
        rep = await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="fail", reason="no_fill"), IP)
        self.assertEqual(rep["next"]["network"], "tads")
        rep = await routes.h_report(self.ctx, self.body(ad_id=rep["next"]["ad_id"], event="fail", reason="no_fill"), IP)
        self.assertTrue(rep["exhausted"])
        atts = (await routes.store.attempts_for_sid("sid1"))
        self.assertEqual(sorted((x["round"], x["network"]) for x in atts), [(1, "monetag"), (1, "tads")])
        self.assertTrue(any(x.get("round_exhausted") for x in atts))
        await self.fail_round()
        self.assertEqual(gate.clean_rounds(await routes.store.attempts_for_sid("sid1"))[0], 2)

    async def test_watched_ad_still_delivers_without_any_grace(self):
        a = await self.start()
        await routes.h_report(self.ctx, self.body(ad_id=a["ad_id"], event="shown"), IP)
        r = await routes.h_done(self.ctx, self.body(ad_id=a["ad_id"], completed=True), IP)
        self.assertTrue(r["ok"] and r["delivered"], r)
        self.assertEqual(self.sent, ["tok1"])


class GraceTests(GateBase):
    async def test_three_no_ad_rounds_and_clean_device_deliver_the_file(self):
        await self.rounds(3)
        cid, proofs = await self.solve()
        r = await self.grace(cid, proofs, cosmetic=True, tamper=False)
        self.assertTrue(r["ok"] and r["delivered"], r)
        self.assertEqual(self.sent, ["tok1"])
        s = await self.sess.get_session("sid1")
        self.assertTrue(s["delivered"] and s["grace"])
        self.assertEqual(len(self.fdb["ad_grace"].docs), 1)

    async def test_fewer_than_three_rounds_no_file(self):
        await self.rounds(2)
        cid, proofs = await self.solve()
        r = await self.grace(cid, proofs)
        self.assertEqual((r["ok"], r["code"]), (False, "rounds"))
        self.assertEqual(self.sent, [])

    async def test_no_attempts_at_all_no_file(self):
        cid, proofs = await self.solve()
        r = await self.grace(cid, proofs)
        self.assertEqual((r["ok"], r["code"]), (False, "rounds"))

    async def test_blocked_sdk_round_never_counts_as_no_ad(self):
        await self.fail_round(); await self.fail_round()
        await self.fail_round(reasons=("no_fill", "blocked"))                    # Tads script blocked in round 3
        cid, proofs = await self.solve()
        r = await self.grace(cid, proofs)
        self.assertEqual((r["ok"], r["code"]), (False, "rounds"))
        self.assertEqual(self.sent, [])

    async def test_error_reason_does_not_count(self):
        await self.rounds(3, reasons=("error", "error"))
        cid, proofs = await self.solve()
        self.assertEqual((await self.grace(cid, proofs))["code"], "rounds")

    async def test_round_where_an_ad_was_displayed_does_not_count(self):
        await self.rounds(2)
        await self.fail_round(shown_first=True)
        cid, proofs = await self.solve()
        self.assertEqual((await self.grace(cid, proofs))["code"], "rounds")

    async def test_attempt_still_in_flight_blocks_grace(self):
        await self.rounds(3)
        await self.start()                                                       # a 4th attempt is live
        cid, proofs = await self.solve()
        self.assertEqual((await self.grace(cid, proofs))["code"], "busy")

    async def test_grace_disabled_by_config(self):
        settings.GATE_GRACE = False
        await self.rounds(3)
        cid, proofs = await self.solve()
        self.assertEqual((await self.grace(cid, proofs))["code"], "disabled")

    async def test_too_soon_returns_wait_then_succeeds(self):
        settings.GATE_MIN_TOTAL_SECONDS = 600
        await self.rounds(3)
        cid, proofs = await self.solve()
        r = await self.grace(cid, proofs)
        self.assertEqual((r["ok"], r["code"]), (False, "too_soon")); self.assertGreater(r["wait"], 0)
        self.assertEqual(self.sent, [])
        for a in self.fdb["ad_requests"].docs:                                   # time passes
            a["t"] -= 700
        cid, proofs = await self.solve()
        self.assertTrue((await self.grace(cid, proofs))["delivered"])

    async def test_hourly_cap_per_user(self):
        settings.GATE_GRACE_MAX_PER_HOUR = 1
        await self.fdb["ad_grace"].insert_one({"_id": "x", "user_id": 777, "t": time.time() - 60})
        await self.rounds(3)
        cid, proofs = await self.solve()
        self.assertEqual((await self.grace(cid, proofs))["code"], "limited")
        self.assertEqual(self.sent, [])


class BlockerTests(GateBase):
    """Every way an ad-blocker (or a lazy client) can fail the server-verified challenge must end with NO file."""

    async def _expect_blocked(self, **solve_kw):
        await self.rounds(3)
        cid, proofs = await self.solve(**solve_kw.get("solve", {}))
        r = await self.grace(cid, proofs, **solve_kw.get("extra", {}))
        self.assertEqual((r["ok"], r["code"]), (False, "blocker"), r)
        self.assertNotIn("why", r)                                               # reason codes stay server-side
        self.assertEqual(self.sent, [])
        self.assertFalse((await self.sess.get_session("sid1"))["delivered"])

    async def test_each_bait_blocked_alone_is_enough(self):
        for k, _kind, _p in gate.BAITS:
            with self.subTest(bait=k):
                await self.asyncTearDown()                                       # fresh DB per bait
                await self.asyncSetUp()
                await self._expect_blocked(solve={"skip": (k,)})

    async def test_stubbed_or_wrong_proof_is_rejected(self):
        await self._expect_blocked(solve={"corrupt": ("s1",)})

    async def test_client_lying_that_the_image_bait_loaded_is_caught_by_the_server_hit_log(self):
        await self.rounds(3)
        cid, proofs = await self.solve(skip=("i1",))                             # blocker stopped the image: server never saw it
        proofs["i1"] = True                                                      # ...but a patched client claims it loaded
        r = await self.grace(cid, proofs)
        self.assertEqual((r["code"], self.sent), ("blocker", []))

    async def test_missing_proofs_entirely(self):
        await self.rounds(3)
        await self.solve()
        cid = (await routes.h_challenge(self.ctx, self.body(), IP))["cid"]
        r = await self.grace(cid, {})
        self.assertEqual(r["code"], "blocker")

    async def test_client_flags_cosmetic_and_tamper(self):
        await self._expect_blocked(extra={"cosmetic": False})

    async def test_tamper_flag(self):
        await self._expect_blocked(extra={"tamper": True})

    async def test_no_challenge_at_all(self):
        await self.rounds(3)
        r = await self.grace(None, {})
        self.assertEqual(r["code"], "blocker")
        r = await self.grace("f" * 24, {"s1": "x"})
        self.assertEqual(r["code"], "blocker")

    async def test_challenge_cannot_be_reused(self):
        await self.rounds(3)
        s = await self.sess.get_session("sid1")
        cid, proofs = await self.solve()
        body = {"cid": cid, "proofs": proofs}
        self.assertTrue((await gate.grant(s, body))["ok"])
        r = await gate.grant(s, body)
        self.assertEqual((r["ok"], r["code"], r["why"]), (False, "blocker", "replayed"))

    async def test_challenge_solved_before_the_rounds_is_stale(self):
        cid, proofs = await self.solve()                                         # pre-solved while the blocker was off
        await self.rounds(3)
        ch = self.fdb["ad_gate"].docs[0]
        ch["iat"] -= 30                                                          # issued well before the last failed attempt
        r = await self.grace(cid, proofs)
        self.assertEqual((r["code"]), "blocker")
        self.assertEqual(self.sent, [])

    async def test_expired_challenge(self):
        await self.rounds(3)
        cid, proofs = await self.solve()
        self.fdb["ad_gate"].docs[-1]["iat"] -= settings.GATE_CHALLENGE_TTL + 5
        self.assertEqual((await self.grace(cid, proofs))["code"], "blocker")

    async def test_challenge_is_bound_to_session_and_user(self):
        await self.rounds(3)
        cid, proofs = await self.solve()
        other = {"sid": "other", "user_id": 999}
        ok, why = await gate.verify_challenge(other, cid, proofs, 0)
        self.assertEqual((ok, why), (False, "unknown_challenge"))
        ok, why = await gate.verify_challenge({"sid": "sid1", "user_id": 999}, cid, proofs, 0)
        self.assertFalse(ok)

    async def test_bait_with_bad_nonce_gives_no_proof_and_no_record(self):
        kind, out = await gate.bait_response("/js/adframe.js", "deadbeef", "s1")
        self.assertEqual(kind, "script"); self.assertNotIn("__gv", out)
        self.assertEqual(len(self.fdb["ad_gate"].docs), 0)
        ch = await routes.h_challenge(self.ctx, self.body(), IP)
        kind, out = await gate.bait_response("/js/adframe.js", ch["cid"], "s2")   # right nonce, wrong key for that path
        self.assertNotIn("__gv", out)
        self.assertNotIn("hit_s1", self.fdb["ad_gate"].docs[0]); self.assertNotIn("hit_s2", self.fdb["ad_gate"].docs[0])

    async def test_bait_urls_look_like_ads_to_filter_lists_but_the_app_page_does_not(self):
        paths = [p for _, _, p in gate.BAITS]
        self.assertTrue(all(re.search(r"ad|banner", p) for p in paths), paths)
        for p in ("/assets/sa.js", "/assets/sg.js", "/app", "/api/smart/start", "/api/gate/challenge"):
            self.assertIsNone(re.search(r"(?<![a-z])ads?(?![a-z])|banner|sponsor", p), p)     # our own app must never be filtered

    async def test_challenge_requires_valid_telegram_session(self):
        r = await routes.h_challenge(self.ctx, {"sid": "sid1", "init_data": "garbage"}, IP)
        self.assertFalse(r["ok"])
        r = await routes.h_challenge(self.ctx, {}, IP)
        self.assertFalse(r["ok"])

    async def test_grace_requires_valid_telegram_session(self):
        await self.rounds(3)
        cid, proofs = await self.solve()
        r = await routes.h_grace(self.ctx, {"sid": "sid1", "init_data": "garbage", "cid": cid, "proofs": proofs}, IP)
        self.assertFalse(r["ok"]); self.assertEqual(self.sent, [])


class RouteWiringTests(unittest.TestCase):
    def test_gate_routes_registered(self):
        from tests.test_routes_wiring import FakeApp
        from ads.verify import Policy
        app = FakeApp()
        routes.register(app, auth=None, identify=None, send_file=None, db=object(), token_re=None, telegram_errors=(),
                        forbidden=(), policy=Policy(), manager=None, ads_required=1, secret="s")
        for m, p in (("POST", "/api/gate/challenge"), ("POST", "/api/smart/grace"), ("GET", "/assets/sa.js"), ("GET", "/assets/sg.js")):
            self.assertIn((m, p), app.routes)
        for _k, _kind, path in gate.BAITS:
            self.assertIn(("GET", path), app.routes)
        names = [fn.__name__ for fn in app.routes.values()]
        self.assertEqual(len(names), len(set(names)), "route handler names must be unique")


if __name__ == "__main__":
    unittest.main()
