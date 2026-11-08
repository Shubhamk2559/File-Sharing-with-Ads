"""Max-earnings engine: bandit maths, config validation, optimizer tick (back-off / probation), report diagnostics."""
import random
import time
import unittest

from ads import bandit, engine, report, settings, store
from tests.fakemongo import FakeDB

NETS = ("monetag", "tads")


def model_of(spec):
    """spec: {net: (requests, verified, value_usd_per_imp)} -> model via the real builders."""
    ev = {n: bandit.Evidence(r, v) for n, (r, v, _) in spec.items()}
    vals = {n: {"value": x, "basis": "owner"} for n, (_, _, x) in spec.items()}
    return bandit.build_model(ev, vals, prior_strength=10)


class DecayTests(unittest.TestCase):
    def test_recent_days_weigh_more(self):
        rows = [{"day": "d0", "network": "monetag", "requests": 100, "verified": 50},
                {"day": "d3", "network": "monetag", "requests": 100, "verified": 50}]
        ev = bandit.decay_fold(rows, {"d0": 0, "d3": 3}, half_life_days=3, networks=NETS)
        self.assertAlmostEqual(ev["monetag"].requests, 150.0)           # 100 + 100 * 0.5
        self.assertAlmostEqual(ev["monetag"].verified, 75.0)
        self.assertEqual(ev["tads"].requests, 0)

    def test_unknown_day_or_network_ignored(self):
        ev = bandit.decay_fold([{"day": "old", "network": "monetag", "requests": 9}, {"day": "d0", "network": "zzz", "requests": 9}],
                               {"d0": 0}, 3, NETS)
        self.assertEqual(ev["monetag"].requests, 0)


class ValueTests(unittest.TestCase):
    def inp(self, **kw):
        base = {n: {"conf_revenue": 0.0, "conf_verified": 0, "owner_ecpm": None} for n in NETS}
        for n, d in kw.items():
            base[n].update(d)
        return base

    def test_nothing_known_is_relative_units_not_invented_money(self):
        v = bandit.estimate_values(self.inp(), 100)
        self.assertTrue(all(x["basis"] == "none" and x["value"] == 1.0 for x in v.values()))

    def test_confirmed_beats_owner_figure_and_is_shrunk(self):
        v = bandit.estimate_values(self.inp(monetag={"conf_revenue": 2.0, "conf_verified": 1000, "owner_ecpm": 1.0}), 100)
        self.assertEqual(v["monetag"]["basis"], "confirmed")
        self.assertAlmostEqual(v["monetag"]["value"], (2.0 + 100 * 0.001) / 1100)      # shrunk toward the owner's prior

    def test_too_few_confirmed_impressions_do_not_count(self):
        v = bandit.estimate_values(self.inp(monetag={"conf_revenue": 5.0, "conf_verified": 10}), 100)
        self.assertNotEqual(v["monetag"]["basis"], "confirmed")

    def test_unknown_borrows_average_never_higher(self):
        v = bandit.estimate_values(self.inp(monetag={"owner_ecpm": 2.0}), 100)
        self.assertEqual(v["tads"]["basis"], "assumed")
        self.assertAlmostEqual(v["tads"]["value"], 0.002)
        self.assertLessEqual(v["tads"]["value"], v["monetag"]["value"])

    def test_monetag_estimate_is_opt_in(self):
        i = self.inp(monetag={"est_revenue": 3.0, "est_impressions": 1000})
        self.assertEqual(bandit.estimate_values(i, 100, use_estimate=False)["monetag"]["basis"], "none")
        on = bandit.estimate_values(i, 100, use_estimate=True)["monetag"]
        self.assertEqual((on["basis"], round(on["value"], 6)), ("monetag_estimate", 0.003))


class PickTests(unittest.TestCase):
    PRIO = ["monetag", "tads"]

    def shares(self, model, elig=NETS, **kw):
        return bandit.expected_shares(list(elig), model, self.PRIO, kw.get("min_share", 0.03), kw.get("probation", set()), kw.get("ps", 0.1))

    def test_high_expected_value_network_gets_most_traffic(self):
        m = model_of({"monetag": (2000, 1200, 0.0010), "tads": (2000, 1400, 0.0040)})
        s = self.shares(m)
        self.assertGreater(s["tads"], 0.8)
        self.assertGreaterEqual(min(s.values()), 0.02)                   # nobody is starved completely

    def test_value_matters_not_just_fill(self):
        m = model_of({"monetag": (1000, 900, 0.0002), "tads": (1000, 500, 0.0020)})
        self.assertGreater(self.shares(m)["tads"], self.shares(m)["monetag"])

    def test_young_network_stays_uncertain_and_gets_explored(self):
        m = model_of({"monetag": (5000, 3000, 0.002), "tads": (3, 3, 0.002)})
        self.assertGreater(self.shares(m)["tads"], 0.05)

    def test_floor_applies_per_network(self):
        m = model_of({"monetag": (5000, 4000, 0.01), "tads": (5000, 100, 0.0001)})
        s = self.shares(m, min_share=0.1)
        self.assertGreater(s["tads"], 0.07)

    def test_probation_network_is_capped(self):
        m = model_of({"monetag": (1000, 500, 0.001), "tads": (1000, 500, 0.01)})
        free = self.shares(m)["tads"]
        held = self.shares(m, probation={"tads"}, ps=0.1)["tads"]
        self.assertGreater(free, 0.8)
        self.assertLess(held, 0.25)

    def test_fallback_chain_ordered_by_expected_value(self):
        m = model_of({"monetag": (1000, 500, 0.001), "tads": (1000, 500, 0.002)})
        order, _ = bandit.pick_order(list(NETS), m, self.PRIO, min_share=0, rng=random.Random(0))
        self.assertEqual(sorted(order), sorted(NETS))
        self.assertEqual(order[1:], sorted(order[1:], key=lambda n: -m[n]["ev_mean"]))

    def test_single_and_empty(self):
        self.assertEqual(bandit.pick_order(["tads"], {}, self.PRIO)[0], ["tads"])
        self.assertEqual(bandit.pick_order([], {}, self.PRIO)[0], [])

    def test_learns_online_and_beats_equal_split(self):
        """Closed-loop simulation: true conversion/value are hidden; the model only sees outcomes."""
        truth = {"monetag": (0.60, 0.0005), "tads": (0.50, 0.0040)}   # best EV: tads
        rng = random.Random(7)
        ev = {n: bandit.Evidence() for n in NETS}
        vals = {n: {"value": truth[n][1], "basis": "confirmed"} for n in NETS}
        picks = {n: 0 for n in NETS}
        earned = 0.0
        model = bandit.build_model(ev, vals, 10)
        for i in range(4000):
            if i % 50 == 0:
                model = bandit.build_model(ev, vals, 10)
            order, _ = bandit.pick_order(list(NETS), model, self.PRIO, 0.03, set(), 0.1, rng)
            n = order[0]
            picks[n] += 1
            ev[n].requests += 1
            if rng.random() < truth[n][0]:
                ev[n].verified += 1
                earned += truth[n][1]
        equal = sum(p * v for p, v in truth.values()) / 2 * 4000
        self.assertGreater(picks["tads"], 0.6 * 4000)
        self.assertGreater(earned, equal * 1.4)


class ValidationTests(unittest.TestCase):
    def v(self, patch):
        return settings.validate_patch(patch, settings.merged({}))

    def test_strategy(self):
        self.assertEqual(self.v({"strategy": "weighted"})[0]["strategy"], "weighted")
        self.assertTrue(self.v({"strategy": "yolo"})[1])

    def test_ecpm_prior(self):
        cfg, errs = self.v({"networks": {"monetag": {"ecpm_usd": 1.5}, "tads": {"ecpm_usd": ""}}})
        self.assertFalse(errs)
        self.assertEqual(cfg["networks"]["monetag"]["ecpm_usd"], 1.5)
        self.assertIsNone(cfg["networks"]["tads"]["ecpm_usd"])
        for bad in (-1, 5000, "abc", True):
            self.assertTrue(self.v({"networks": {"monetag": {"ecpm_usd": bad}}})[1], bad)

    def test_new_optimization_knobs(self):
        cfg, errs = self.v({"optimization": {"half_life_days": 5, "min_share": 0.05, "use_monetag_estimate": True}})
        self.assertFalse(errs)
        self.assertEqual(cfg["optimization"]["half_life_days"], 5.0)
        self.assertIs(cfg["optimization"]["use_monetag_estimate"], True)
        for bad in ({"min_share": 0.9}, {"half_life_days": 0}, {"use_monetag_estimate": "yes"}, {"probation_share": 2}):
            self.assertTrue(self.v({"optimization": bad})[1], bad)

    def test_defaults_are_max_earnings(self):
        self.assertEqual(settings.merged({})["strategy"], "thompson")


class EngineBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb = FakeDB()
        store.set_database(self.fdb)
        self._saved = (settings.TADS_WIDGET_ID, dict(settings.RUNTIME))
        settings.TADS_WIDGET_ID = "13070"
        settings.RUNTIME["strict"] = False        # development profile: these tests cover the non-production client-attested path
        await store.save_config({"mode": "auto", "networks": {"tads": {"enabled": True, "allow_client_reward": True},
                                                              "monetag": {"enabled": True}}})
        engine.invalidate()

    async def asyncTearDown(self):
        settings.TADS_WIDGET_ID, rt = self._saved
        settings.RUNTIME.clear(); settings.RUNTIME.update(rt)
        store.set_database(None); engine.invalidate()

    async def tick(self):
        self.fdb["ad_config"].docs = [d for d in self.fdb["ad_config"].docs if not str(d["_id"]).startswith("lease:")]
        return await engine.tick()

    async def seed(self, n, requests, verified, hard_fails=0):
        await store.bump(n, {"requests": requests, "verified": verified, "shown": verified, "hard_fails": hard_fails})


class TickTests(EngineBase):
    async def test_tick_stores_model_and_shares(self):
        await self.seed("monetag", 400, 100)
        await self.seed("tads", 400, 300)
        await store.set_confirmed_revenue(store.day_key(), "tads", 3.0)     # $3 over 300 verified = $10 eCPM
        await store.set_confirmed_revenue(store.day_key(), "monetag", 0.1)
        self.assertTrue(await self.tick())
        plan = await store.get_doc("plan")
        self.assertEqual(plan["model"]["tads"]["basis"], "confirmed")
        self.assertAlmostEqual(sum(plan["shares"].values()), 1.0, places=2)
        self.assertEqual(plan["leader"], "tads")
        self.assertGreater(plan["shares"]["tads"], 0.7)

    async def test_plan_request_follows_the_model(self):
        await self.seed("monetag", 400, 100)
        await self.seed("tads", 400, 300)
        await store.set_confirmed_revenue(store.day_key(), "tads", 3.0)
        await store.set_confirmed_revenue(store.day_key(), "monetag", 0.1)
        await self.tick()
        rng = random.Random(1)
        firsts = [(await engine.plan_request(rng)).order[0] for _ in range(300)]
        self.assertGreater(firsts.count("tads") / 300, 0.7)
        self.assertEqual(len((await engine.plan_request(rng)).order), 2)         # full fallback chain is offered

    async def test_bench_backoff_doubles_then_probation_then_forgiven(self):
        cfg = await engine.get_config(force=True)
        cd = cfg["optimization"]["cooldown_minutes"] * 60
        await self.seed("tads", 100, 0, hard_fails=100)
        await self.tick()
        p1 = await store.get_doc("plan")
        self.assertIn("tads", p1["benched"])
        self.assertAlmostEqual(p1["benched"]["tads"] - time.time(), cd, delta=5)
        self.assertEqual(p1["bench_level"]["tads"], 1)
        # cool-down ends while the bucket still shows failures -> benched again, twice as long
        await store.set_doc("plan", {"benched": {"tads": time.time() - 1}})
        await self.tick()
        p2 = await store.get_doc("plan")
        self.assertAlmostEqual(p2["benched"]["tads"] - time.time(), cd * 2, delta=5)
        self.assertEqual(p2["bench_level"]["tads"], 2)
        # healthy window after the bench -> expired bench becomes probation and strikes are forgiven
        self.fdb["ad_buckets"].docs.clear()
        await self.seed("tads", 60, 55)
        await store.set_doc("plan", {"benched": {"tads": time.time() - 1}})
        await self.tick()
        p3 = await store.get_doc("plan")
        self.assertNotIn("tads", p3["benched"])
        self.assertIn("tads", p3["probation"])
        self.assertEqual(p3["bench_level"]["tads"], 0)

    async def test_weighted_strategy_still_available(self):
        await engine.get_config(force=True)
        await store.save_config({"mode": "auto", "strategy": "weighted", "networks": {"tads": {"enabled": True, "allow_client_reward": True}}})
        engine.invalidate()
        await self.tick()
        plan = await engine.plan_request(random.Random(3))
        self.assertTrue(plan.order)
        self.assertNotIn("thompson", plan.reason)


class ReportTests(EngineBase):
    async def test_blockers_explain_why_only_monetag_gets_traffic(self):
        await store.save_config({"mode": "auto", "networks": {"tads": {"enabled": True, "allow_client_reward": False}}})
        engine.invalidate()
        cfg = await engine.get_config(force=True)
        b = engine.blockers(cfg)
        self.assertEqual(b["monetag"], [])
        self.assertEqual(b["tads"], ["client_attested_not_allowed"])
        rep = await report.build(7)
        self.assertTrue(any("Only 1 network" in h for h in rep["hints"]))
        await store.save_config({"mode": "auto", "networks": {"tads": {"enabled": False}}})
        engine.invalidate()
        rep = await report.build(7)
        self.assertIn("switched off", rep["networks"]["tads"]["blockers_text"])

    async def test_report_has_earnings_and_expected_per_network(self):
        await self.seed("monetag", 300, 100)
        await self.seed("tads", 300, 240)
        await store.set_confirmed_revenue(store.day_key(), "tads", 2.4)
        await store.set_confirmed_revenue(store.day_key(), "monetag", 0.2)
        await self.tick()
        rep = await report.build(7)
        e = rep["earnings"]
        self.assertTrue(e["comparable"])
        self.assertGreater(e["expected_per_1000_requests_usd"], e["equal_split_per_1000_usd"])
        self.assertGreater(e["uplift_pct"], 0)
        self.assertEqual(rep["networks"]["tads"]["expected"]["basis"], "confirmed")
        self.assertIn("Expected per 1000", report.summary_html(rep))

    async def test_owner_ecpm_is_labelled_as_owner_figure(self):
        await store.save_config({"mode": "auto", "networks": {"tads": {"enabled": True, "ecpm_usd": 3.0, "allow_client_reward": True}}})
        engine.invalidate()
        await self.tick()
        rep = await report.build(7)
        self.assertEqual(rep["networks"]["tads"]["expected"]["basis"], "owner")
        self.assertEqual(rep["networks"]["tads"]["expected"]["value_per_1000_usd"], 3.0)
        self.assertTrue(any("your own figure" in h for h in rep["hints"]))


if __name__ == "__main__":
    unittest.main()
