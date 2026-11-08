"""Batch 2 / FIX 3: shared persistent rate limiting (ads/ratelimit.py).

Two application "instances" are two SharedLimiter objects with separate process state over ONE FakeDB (= one MongoDB).
FakeDB(yield_ops=True) makes every operation yield to the event loop first, so concurrent tasks really interleave.
"""
import asyncio
import ast
import inspect
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ads import postback, ratelimit, routes, settings              # noqa: E402
from ads.ratelimit import (Decision, RateLimitConfigError, SharedLimiter, client_ip, load_settings)   # noqa: E402
from ads.store import DuplicateKeyError                              # noqa: E402
from tests.fakemongo import ConnectionFailure, FakeDB, FakeSessions  # noqa: E402


class Clock:
    def __init__(self, t=6000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


def make(fdb, clock, env=None, **kw):
    """One application instance."""
    s = load_settings(env or {}, salt="unit-test-salt")
    if kw:
        s = ratelimit.Settings(**{**s.__dict__, **kw})
    return SharedLimiter(lambda: fdb["rate_limits"], s, clock)


def env(**kv):
    return {"RL_" + k.upper(): v for k, v in kv.items()}


async def count_allowed(limiters, category, key, n):
    res = await asyncio.gather(*[limiters[i % len(limiters)].hit(category, key) for i in range(n)])
    return sum(1 for r in res if r.allowed)


# =============================================================================== sharing + atomicity
class SharedAcrossInstancesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(yield_ops=True), Clock()
        self.e = env(api_done_user="10/60")

    async def test_two_instances_share_one_budget(self):
        a, b = make(self.fdb, self.clock, self.e), make(self.fdb, self.clock, self.e)
        results = []
        for i in range(30):                                            # alternate instances: 30 requests, limit 10
            results.append((await (a if i % 2 else b).hit("api_done_user", 777)).allowed)
        self.assertEqual(sum(results), 10)                              # NOT 20 (that is what two process-local limiters would allow)
        self.assertEqual(results[:10], [True] * 10)
        self.assertFalse(any(results[10:]))

    async def test_limit_survives_an_instance_restart(self):
        a = make(self.fdb, self.clock, self.e)
        for _ in range(10):
            self.assertTrue((await a.hit("api_done_user", 5)).allowed)
        a2 = make(self.fdb, self.clock, self.e)                         # "restart": brand-new process state
        self.assertFalse((await a2.hit("api_done_user", 5)).allowed)

    async def test_concurrent_requests_never_exceed_the_limit(self):
        ls = [make(self.fdb, self.clock, self.e) for _ in range(4)]
        self.assertEqual(await count_allowed(ls, "api_done_user", 1, 300), 10)
        # a second burst in the same window gets nothing
        self.assertEqual(await count_allowed(ls, "api_done_user", 1, 50), 0)

    async def test_concurrent_burst_on_limit_of_one(self):
        ls = [make(self.fdb, self.clock, env(api_done_user="1/60")) for _ in range(5)]
        self.assertEqual(await count_allowed(ls, "api_done_user", "x", 40), 1)

    async def test_many_keys_concurrently_each_get_exactly_their_own_budget(self):
        ls = [make(self.fdb, self.clock, self.e) for _ in range(3)]
        res = await asyncio.gather(*[ls[i % 3].hit("api_done_user", i % 5) for i in range(150)])      # 5 users x 30 requests
        per_user = {u: 0 for u in range(5)}
        for i, r in enumerate(res):
            per_user[i % 5] += r.allowed
        self.assertEqual(per_user, {u: 10 for u in range(5)})

    async def test_first_insert_race_is_retried_not_lost(self):
        """Two upserts racing on a brand new window document: MongoDB gives the loser DuplicateKeyError. It must be retried (counts)."""
        calls = {"n": 0}

        def fail_once(op, coll):
            if op == "find_one_and_update" and coll == "rate_limits":
                calls["n"] += 1
                if calls["n"] == 1:
                    return DuplicateKeyError("simulated lost upsert race")

        self.fdb.fail_when = fail_once
        a = make(self.fdb, self.clock, self.e)
        self.assertTrue((await a.hit("api_done_user", 9)).allowed)
        self.assertEqual(calls["n"], 2)

    async def test_persistent_duplicate_key_means_denied(self):
        a = make(self.fdb, self.clock, self.e)
        for _ in range(10):
            await a.hit("api_done_user", 9)
        d = await a.hit("api_done_user", 9)
        self.assertEqual((d.allowed, d.reason), (False, "limit"))
        self.assertGreaterEqual(d.retry_after, 1)

    async def test_denied_attempts_do_not_consume_budget_and_cost_no_db_work(self):
        a = make(self.fdb, self.clock, env(api_done_user="3/60"))
        for _ in range(3):
            await a.hit("api_done_user", 1)
        self.assertFalse((await a.hit("api_done_user", 1)).allowed)      # first denial is learned from the store
        ops = {"n": 0}
        self.fdb.fail_when = lambda op, coll: ops.__setitem__("n", ops["n"] + 1)
        for _ in range(500):                                            # flood while denied
            self.assertFalse((await a.hit("api_done_user", 1)).allowed)
        self.assertEqual(ops["n"], 0)                                    # in-process deny shortcut: no database round trip at all
        self.assertEqual(self.fdb["rate_limits"].docs[0]["n"], 3)        # the counter never went above the limit


# =============================================================================== expiry / windows
class WindowAndExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(), Clock(6000.0)                  # 6000 is a window boundary for 60 s windows
        self.lim = make(self.fdb, self.clock, env(api_done_user="10/60"))

    async def burst(self, n=15):
        return sum([(await self.lim.hit("api_done_user", 1)).allowed for _ in range(n)])

    async def test_budget_returns_after_the_window(self):
        self.assertEqual(await self.burst(), 10)
        self.clock.advance(61)                                           # new window; previous one is mostly outside the sliding window
        self.assertGreaterEqual(await self.burst(), 1)
        self.clock.advance(180)                                          # idle for 3 windows: everything is free again
        self.assertEqual(await self.burst(), 10)

    async def test_no_double_burst_at_the_window_boundary(self):
        self.clock.t = 6059.0
        self.assertEqual(await self.burst(), 10)                         # full budget at the very end of a window...
        self.clock.t = 6061.0                                            # ...two seconds later (new window)
        self.assertLessEqual(await self.burst(), 1)                      # a plain fixed window would allow 10 more here

    async def test_retry_after_is_reported_and_roughly_right(self):
        self.clock.t = 6010.0
        await self.burst()
        d = await self.lim.hit("api_done_user", 1)
        self.assertFalse(d.allowed)
        self.assertTrue(1 <= d.retry_after <= 60)

    async def test_denied_user_recovers_by_themselves(self):
        await self.burst()
        self.assertFalse((await self.lim.hit("api_done_user", 1)).allowed)
        self.clock.advance(125)
        self.assertTrue((await self.lim.hit("api_done_user", 1)).allowed)

    async def test_ttl_cleanup_removes_expired_records(self):
        await self.burst()
        docs = self.fdb["rate_limits"].docs
        self.assertEqual(len(docs), 1)
        exp = docs[0]["expire_at"]
        self.assertEqual(exp.timestamp(), 6000 + 2 * 60)                 # kept for the current + the next window (sliding carry), then expires
        await self.fdb["rate_limits"].create_index("expire_at", expireAfterSeconds=0)   # what migration 0004 does
        self.assertEqual(self.fdb.reap(6000 + 100), 0)                   # still needed for the sliding window
        self.assertEqual(self.fdb.reap(6000 + 121), 1)                   # TTL monitor removes it afterwards
        self.assertEqual(self.fdb["rate_limits"].docs, [])

    async def test_documents_never_contain_the_raw_key(self):
        await self.lim.hit("api_done_user", 987654321)
        await self.lim.hit("api_start_ip", "203.0.113.77")
        blob = repr(self.fdb["rate_limits"].docs)
        self.assertNotIn("987654321", blob)
        self.assertNotIn("203.0.113.77", blob)


# =============================================================================== IP / user / category isolation
class KeyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(), Clock()
        self.lim = make(self.fdb, self.clock, env(api_done_user="3/60", api_done_ip="5/60", api_start_user="3/60"))

    async def hits(self, cat, key, n):
        return [(await self.lim.hit(cat, key)).allowed for _ in range(n)]

    async def test_users_are_independent(self):
        self.assertEqual(await self.hits("api_done_user", 1, 5), [True] * 3 + [False] * 2)
        self.assertEqual(await self.hits("api_done_user", 2, 3), [True] * 3)           # user 2 is not affected by user 1

    async def test_ips_are_independent(self):
        self.assertEqual(await self.hits("api_done_ip", "1.1.1.1", 7), [True] * 5 + [False] * 2)
        self.assertEqual(await self.hits("api_done_ip", "2.2.2.2", 5), [True] * 5)

    async def test_ip_and_user_limits_are_separate_budgets(self):
        await self.hits("api_done_ip", "1.1.1.1", 5)
        self.assertTrue((await self.lim.hit("api_done_user", 1)).allowed)             # an exhausted IP bucket doesn't burn the user's

    async def test_categories_are_independent_for_the_same_user(self):
        await self.hits("api_done_user", 1, 3)
        self.assertEqual(await self.hits("api_start_user", 1, 3), [True] * 3)

    async def test_a_shared_ip_cannot_starve_other_ips_and_users_of_that_ip_keep_their_own_user_budget(self):
        await self.hits("api_done_ip", "9.9.9.9", 5)
        self.assertFalse((await self.lim.hit("api_done_ip", "9.9.9.9")).allowed)
        self.assertTrue((await self.lim.hit("api_done_ip", "8.8.8.8")).allowed)

    async def test_unknown_category_is_a_programming_error_not_a_silent_allow(self):
        with self.assertRaises(KeyError):
            await self.lim.hit("no_such_category", 1)


# =============================================================================== database unavailable
class StoreFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(), Clock()
        self.e = env(api_done_user="5/60", admin_fail_ip="3/600")
        self.lim = make(self.fdb, self.clock, self.e, cooldown=5.0)

    async def test_failed_credential_counters_fail_closed(self):
        self.fdb.down = True
        for _ in range(20):
            d = await self.lim.hit("admin_fail_ip", "1.2.3.4")
            self.assertFalse(d.allowed)
            self.assertEqual((d.degraded, d.reason), (True, "store_error_deny"))

    async def test_normal_categories_stay_limited_locally_never_unlimited(self):
        self.fdb.down = True
        res = [(await self.lim.hit("api_done_user", 42)) for _ in range(12)]
        self.assertEqual(sum(r.allowed for r in res), 5)                               # same limit, enforced in-process
        self.assertTrue(all(r.degraded for r in res))
        self.assertEqual([r.allowed for r in res[:5]], [True] * 5)

    async def test_strict_mode_denies_everything_while_the_store_is_down(self):
        strict = make(self.fdb, self.clock, dict(self.e, RL_ON_STORE_ERROR="deny"))
        self.fdb.down = True
        self.assertFalse((await strict.hit("api_done_user", 1)).allowed)
        self.assertFalse((await strict.hit("link_ip", "1.1.1.1")).allowed)

    async def test_any_exception_type_degrades_safely(self):
        for exc in (ValueError("boom"), RuntimeError("x"), ConnectionFailure("net"), OSError("sock")):
            fdb = FakeDB()
            fdb.fail_when = lambda op, coll, e=exc: e
            lim = make(fdb, Clock(), self.e)
            res = [(await lim.hit("api_done_user", 1)).allowed for _ in range(8)]
            self.assertEqual(sum(res), 5, repr(exc))

    async def test_hanging_store_is_cut_off_by_the_timeout(self):
        class Hang:
            async def find_one(self, *a, **k): await asyncio.sleep(30)
            async def find_one_and_update(self, *a, **k): await asyncio.sleep(30)
        s = load_settings(self.e, "salt")
        lim = SharedLimiter(lambda: Hang(), ratelimit.Settings(**{**s.__dict__, "timeout": 0.05}), self.clock)
        d = await asyncio.wait_for(lim.hit("api_done_user", 1), 2)
        self.assertTrue(d.degraded)
        self.assertEqual(lim.stats["store_errors"], 1)

    async def test_circuit_breaker_stops_hammering_a_dead_store_then_recovers(self):
        ops = {"n": 0}

        def count(op, coll):
            ops["n"] += 1
        self.fdb.fail_when = count
        self.fdb.down = True
        await self.lim.hit("api_done_user", 1)
        first = ops["n"]
        for i in range(50):
            await self.lim.hit("api_done_user", 100 + i)
        self.assertEqual(self.lim.stats["store_errors"], 1)                            # one failure, then the breaker is open
        self.assertEqual(ops["n"], first)                                              # no further DB calls during the cool-down

        self.fdb.down = False                                                          # database is back...
        self.clock.advance(6)                                                          # ...and the cool-down is over
        self.assertTrue((await self.lim.hit("api_done_user", 1)).allowed)
        self.assertFalse((await self.lim.hit("api_done_user", 1)).degraded)            # shared limits are active again
        self.assertGreater(len(self.fdb["rate_limits"].docs), 0)

    async def test_database_restart_keeps_the_shared_counters(self):
        for _ in range(4):
            await self.lim.hit("api_done_user", 7)
        self.fdb.down = True                                                           # restart: unreachable for a while
        await self.lim.hit("api_done_user", 7)
        self.fdb.down = False                                                          # same data after the restart
        self.clock.advance(6)
        d = await self.lim.hit("api_done_user", 7)
        self.assertFalse(d.degraded)
        self.assertEqual(self.fdb["rate_limits"].docs[0]["n"], 5 if d.allowed else 4)  # counter continues from persisted state, not from 0
        self.assertFalse((await self.lim.hit("api_done_user", 7)).allowed)

    async def test_local_fallback_memory_is_bounded_and_fails_closed_when_full(self):
        lim = make(self.fdb, self.clock, self.e, max_local_keys=1000)
        self.fdb.down = True
        res = [(await lim.hit("api_done_user", k)).allowed for k in range(1200)]
        self.assertEqual(sum(res), 1000)                                               # a key flood can't grow memory without bound
        self.assertEqual(len(lim._local._hits), 1000)
        self.assertTrue((await lim.hit("api_done_user", 5)).allowed)                   # a key already tracked keeps working within its own limit

    async def test_degraded_alert_is_logged_once_not_per_request(self):
        self.fdb.down = True
        with self.assertLogs("ads", level="ERROR") as cm:
            for i in range(40):
                await self.lim.hit("api_done_user", i)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("ratelimit_store_unavailable", cm.output[0])


# =============================================================================== configuration
class ConfigTests(unittest.TestCase):
    def test_defaults_keep_the_batch1_numbers(self):
        s = load_settings({})
        self.assertEqual((s.limits["api_done_user"].limit, s.limits["api_done_user"].window), (20, 60))
        self.assertEqual((s.limits["api_done_ip"].limit, s.limits["gate_challenge_ip"].limit, s.limits["bait_ip"].limit), (90, 600, 3000))
        self.assertEqual((s.limits["admin_fail_ip"].limit, s.limits["admin_fail_ip"].window), (10, 600))
        self.assertEqual((s.limits["postback_fail_ip"].limit, s.limits["postback_fail_ip"].window), (20, 600))
        self.assertEqual(s.trusted_proxies, 1)

    def test_env_overrides(self):
        s = load_settings({"RL_API_DONE_USER": "7/30", "RL_LINK_IP": "500", "TRUSTED_PROXY_COUNT": "2", "RL_STORE_TIMEOUT_MS": "250"})
        self.assertEqual((s.limits["api_done_user"].limit, s.limits["api_done_user"].window), (7, 30))
        self.assertEqual((s.limits["link_ip"].limit, s.limits["link_ip"].window), (500, 60))     # only the max: default window kept
        self.assertEqual((s.trusted_proxies, s.timeout), (2, 0.25))

    def test_invalid_values_stop_the_process_instead_of_being_ignored(self):
        for bad in ({"RL_API_DONE_USER": "abc"}, {"RL_API_DONE_USER": "0/60"}, {"RL_API_DONE_USER": "5/0"}, {"RL_API_DONE_USER": "5/999999"},
                    {"RL_ON_STORE_ERROR": "ignore"}, {"TRUSTED_PROXY_COUNT": "-1"}, {"TRUSTED_PROXY_COUNT": "x"}, {"RL_STORE_TIMEOUT_MS": "1"}):
            with self.assertRaises(RateLimitConfigError, msg=str(bad)):
                load_settings(bad)

    def test_fail_modes(self):
        s = load_settings({})
        for name in ("admin_fail_ip", "monetag_admin_fail_ip", "postback_fail_ip", "webhook_fail_ip"):
            self.assertEqual(s.limits[name].fail_mode, "deny", name)
        for name in ("api_done_user", "link_ip", "bot_start_user"):
            self.assertEqual(s.limits[name].fail_mode, "local", name)
        self.assertTrue(all(v.fail_mode == "deny" for v in load_settings({"RL_ON_STORE_ERROR": "deny"}).limits.values()))

    def test_every_category_used_by_the_code_exists(self):
        used = set()
        for f in ("main.py", "handlers.py", "ads/routes.py"):
            used |= set(re.findall(r'''(?:_limited|_bot_limited|hit|allow|bind)\(\s*(?:\w+,\s*)?"([a-z_]+)"''', (ROOT / f).read_text(encoding="utf-8")))
        used |= set(re.findall(r'"((?:api|gate|legacy|link|file_delivery|admin|monetag_admin|postback|webhook|bait)_[a-z_]+)"', (ROOT / "main.py").read_text()))
        missing = {u for u in used if u not in ratelimit.DEFAULTS and not u.endswith("_id")}
        self.assertEqual(missing, set())


# =============================================================================== client IP
class ClientIpTests(unittest.TestCase):
    def test_spoofed_x_forwarded_for_cannot_choose_its_own_bucket(self):
        # attacker sends a fresh fake leftmost address on every request; our proxy appends the real one
        ips = {client_ip(f"10.0.0.{i}, 203.0.113.5", "172.16.0.1", 1) for i in range(50)}
        self.assertEqual(ips, {"203.0.113.5"})

    def test_two_trusted_proxies(self):
        self.assertEqual(client_ip("6.6.6.6, 203.0.113.5, 198.51.100.2", "172.16.0.1", 2), "203.0.113.5")

    def test_untrusted_or_missing_header_uses_the_socket_peer(self):
        self.assertEqual(client_ip("203.0.113.5", "172.16.0.1", 0), "172.16.0.1")           # no proxy in front: header is attacker-controlled
        self.assertEqual(client_ip(None, "172.16.0.1", 1), "172.16.0.1")
        self.assertEqual(client_ip("203.0.113.5", "172.16.0.1", 2), "172.16.0.1")           # fewer entries than proxies: not set by all of them
        self.assertEqual(client_ip("not-an-ip", "172.16.0.1", 1), "172.16.0.1")
        self.assertEqual(client_ip("", None, 1), "unknown")

    def test_ipv6_is_grouped_by_64(self):
        a = client_ip("2001:db8:1:2:aaaa::1", "10.0.0.1", 1)
        b = client_ip("2001:db8:1:2:bbbb::9", "10.0.0.1", 1)
        c = client_ip("2001:db8:1:3::1", "10.0.0.1", 1)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(client_ip("::ffff:203.0.113.5", "10.0.0.1", 1), "203.0.113.5")

    def test_code_no_longer_trusts_the_leftmost_entry(self):
        for f in ("main.py", "ads/routes.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertNotIn('split(",")[0]', src, f)
            self.assertIn("client_ip", src, f)


# =============================================================================== postback + admin integration
SECRET = "unit-test-secret-0123456789"


class FakeMonetag:
    """Minimal store: records how many postbacks reached it."""
    def __init__(self): self.calls = 0
    async def record_postback(self, q, **kw):
        self.calls += 1
        return "ok"


class PostbackSharedLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(yield_ops=True), Clock()
        self.mon = FakeMonetag()

    def inst(self):
        l = make(self.fdb, self.clock, env(postback_fail_ip="20/600"))
        return l, l.bind("postback_fail_ip")

    async def pb(self, bound, secret, ip="6.6.6.6"):
        return (await postback.handle({"secret": secret, "ymid": "a.b"}, secret=SECRET, store=self.mon, ip=ip, fail_limiter=bound, clock=self.clock))[0]

    async def test_bad_secret_budget_is_shared_between_instances(self):
        (_, a), (_, b) = self.inst(), self.inst()
        codes = [await self.pb(a if i % 2 else b, "wrong") for i in range(30)]
        self.assertEqual(codes[:20], [403] * 20)                      # 20 failures in TOTAL, not 20 per instance
        self.assertEqual(set(codes[20:]), {429})

    async def test_concurrent_guessing_never_gets_more_than_the_budget(self):
        binds = [self.inst()[1] for _ in range(4)]
        codes = await asyncio.gather(*[self.pb(binds[i % 4], "wrong") for i in range(200)])
        self.assertEqual(codes.count(403), 20)
        self.assertEqual(codes.count(429), 180)

    async def test_correct_secret_is_never_limited_even_after_the_ip_is_locked_out(self):
        _, a = self.inst()
        for _ in range(30):
            await self.pb(a, "wrong")
        self.assertEqual(await self.pb(a, SECRET), 200)
        self.assertEqual(self.mon.calls, 1)

    async def test_correct_secret_still_works_and_bad_ones_stay_blocked_while_the_limiter_store_is_down(self):
        limiter_db = FakeDB()
        lim = make(limiter_db, self.clock, env(postback_fail_ip="20/600"))
        bound = lim.bind("postback_fail_ip")
        limiter_db.down = True
        self.assertEqual(await self.pb(bound, "wrong"), 429)          # fail closed: guessing is not free during an outage
        self.assertEqual(await self.pb(bound, "wrong2", ip="7.7.7.7"), 429)
        self.assertEqual(await self.pb(bound, SECRET), 200)           # Monetag's real postbacks are unaffected
        self.assertEqual(self.mon.calls, 1)


def _ctx(limiter, token="adm-token"):
    async def auth(body): return None, None, "x"
    return routes.Ctx(auth=auth, identify=lambda i: (None, None), send_file=None, db=None, token_re=re.compile(".*"), limiter=limiter, admin_token=token)


class AdminLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(), Clock()

    async def test_admin_brute_force_is_shared_and_locks_out(self):
        a = _ctx(make(self.fdb, self.clock, env(admin_fail_ip="3/600")))
        b = _ctx(make(self.fdb, self.clock, env(admin_fail_ip="3/600")))
        res = [await routes.admin_check(c, "Bearer nope", "5.5.5.5") for c in (a, b, a, b, a, b)]
        self.assertEqual([r[0] for r in res], [403, 403, 403, 429, 429, 429])
        self.assertIsNone(await routes.admin_check(a, "Bearer adm-token", "9.9.9.9"))     # another IP with the right token is fine

    async def test_admin_during_store_outage_right_token_works_wrong_token_is_refused(self):
        ctx = _ctx(make(self.fdb, self.clock, env(admin_fail_ip="3/600")))
        self.fdb.down = True
        self.assertIsNone(await routes.admin_check(ctx, "Bearer adm-token", "5.5.5.5"))
        for _ in range(5):
            self.assertEqual((await routes.admin_check(ctx, "Bearer nope", "5.5.5.5"))[0], 429)

    async def test_no_admin_token_means_404_before_any_counting(self):
        ctx = _ctx(make(self.fdb, self.clock), token="")
        self.assertEqual((await routes.admin_check(ctx, None, "1.1.1.1"))[0], 404)
        self.assertEqual(self.fdb["rate_limits"].docs, [])


# =============================================================================== file delivery + route helpers
class DeliveryLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fdb, self.clock = FakeDB(), Clock()
        self.sess = FakeSessions(self.fdb)
        self.sent = []

        async def send_file(bot, chat_id, doc, manager):
            self.sent.append(doc["token"])
            return True
        lim = make(self.fdb, self.clock, env(file_delivery_user="2/3600"))
        self.ctx = routes.Ctx(auth=None, identify=None, send_file=send_file, db=self.sess, token_re=re.compile(".*"), limiter=lim)
        self.app = type("A", (), {"bot": object()})()

    async def deliver(self, sid, uid=7):
        await self.sess.create_session(sid, "tok", uid, uid, 1, 1)
        await self.sess.col.update_one({"sid": sid}, {"$set": {"ads_done": 1}})
        s = await self.sess.get_session(sid)
        return await routes._deliver(self.ctx, s, s, self.app, {"ok": True})

    async def test_hourly_cap_per_user_is_enforced_before_the_claim(self):
        self.assertTrue((await self.deliver("s1"))["delivered"])
        self.assertTrue((await self.deliver("s2"))["delivered"])
        r = await self.deliver("s3")
        self.assertEqual((r["ok"], r["code"]), (False, "rate_limited"))
        self.assertEqual(len(self.sent), 2)
        self.assertFalse((await self.sess.get_session("s3"))["delivered"])             # not consumed: deliverable later
        self.assertTrue((await self.deliver("other", uid=8))["delivered"])             # other users unaffected
        self.clock.advance(3 * 3600)
        self.assertTrue((await self.ctx.limiter.hit("file_delivery_user", 7)).allowed)  # and the cap resets on its own


class RoutesWiringTests(unittest.TestCase):
    def test_no_process_local_limiter_left_in_production_code(self):
        for f in ("main.py", "ads/routes.py", "handlers.py", "ads/postback.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertNotIn("RateLimiter(", src, f)
            self.assertNotIn("rl_user", src, f)
        self.assertNotIn("ctx.rl_", (ROOT / "ads/routes.py").read_text())

    def test_every_public_endpoint_is_rate_limited_before_doing_work(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        for name in ("webhook", "permanent_link", "ad_start", "ad_fail", "ad_done", "ad_grace", "monetag_postback", "_admin_guard"):
            src = ast.unparse(fns[name])
            self.assertTrue("limiter" in src or "_limited" in src or "_postback_fail_limiter" in src or "_client_ip" in src, name)
        # legacy Mini App endpoints: the IP check comes before the body is even parsed
        for name in ("ad_start", "ad_fail", "ad_done", "ad_grace"):
            src = ast.unparse(fns[name])
            self.assertLess(src.index("_limited"), src.index("request.json()"), name)

    def test_route_signatures_unchanged_for_fastapi(self):
        # permanent_link now takes `request`; main.py must still not postpone annotations (FastAPI would treat it as a query param)
        self.assertNotIn("from __future__ import annotations", (ROOT / "main.py").read_text())
        self.assertNotIn("from __future__ import annotations", (ROOT / "ads/routes.py").read_text().split('"""')[2])


if __name__ == "__main__":
    unittest.main()
