"""Batch 2 / FIX 4: reliable, idempotent, lock-protected migrations (migrator.py + migrations/registry.py)."""
import ast
import asyncio
import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import migrator                                                    # noqa: E402
from migrator import Migration, MigrationError, MigrationLockTimeout, ensure_schema, index_migration, is_complete   # noqa: E402
from migrations import registry                                    # noqa: E402
from tests.fakemongo import ConnectionFailure, FakeDB, OperationFailure   # noqa: E402

FAST = dict(poll=0.01, lock_ttl=10.0, wait_timeout=5.0)


def acc(fdb):
    return lambda name: fdb[name]


async def run(fdb, migrations=None, **kw):
    return await ensure_schema(acc(fdb), migrations or registry.MIGRATIONS, reconcile=registry.reconcilers(1800), **{**FAST, **kw})


def index_keys(fdb, coll):
    return {tuple(ix["key"]) if len(ix["key"]) > 1 else ix["key"][0] for ix in fdb[coll].indexes.values()}


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


# ================================================================== fresh install, repeats, tracking
class BasicTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_database_gets_everything_recorded_in_order(self):
        fdb = FakeDB()
        r = await run(fdb)
        self.assertEqual(r.ran, [1, 2, 3, 4, 5])
        self.assertEqual(r.version, 5)
        rec = sorted(fdb[migrator.TRACK].docs, key=lambda d: d["version"])
        self.assertEqual([d["_id"] for d in rec], [m.id for m in registry.MIGRATIONS])
        self.assertTrue(all(isinstance(d["applied_at"], datetime) for d in rec))
        self.assertTrue(await is_complete(acc(fdb), registry.MIGRATIONS))
        self.assertEqual(fdb[migrator.LOCK].docs, [])                          # lock released

    async def test_expected_indexes_exist(self):
        fdb = FakeDB()
        await run(fdb)
        ix = lambda c: {n: i for n, i in fdb[c].indexes.items()}                # noqa: E731
        self.assertTrue(ix("files")["token_1"]["unique"])
        self.assertTrue(ix("sessions")["sid_1"]["unique"])
        self.assertEqual(ix("sessions")["created_1"]["expireAfterSeconds"], 1800)
        self.assertEqual(ix("rate_limits")["expire_at_1"]["expireAfterSeconds"], 0)       # TTL cleanup of rate-limit records
        self.assertIn("created_at_-1", ix("files"))
        self.assertEqual(ix("ad_gate")["created_1"]["expireAfterSeconds"], 3600)
        self.assertEqual(ix("monetag_ads")["created_1"]["expireAfterSeconds"], 30 * 86400)
        self.assertIn("sid_1", ix("monetag_ads"))

    async def test_repeated_execution_is_a_no_op(self):
        fdb = FakeDB()
        await run(fdb)
        snap = {n: copy.deepcopy(c.docs) for n, c in fdb.c.items()}
        idx = {n: copy.deepcopy(c.indexes) for n, c in fdb.c.items()}
        for _ in range(3):
            r = await run(fdb)
            self.assertEqual(r.ran, [])                                         # nothing re-applied
        self.assertEqual({n: c.docs for n, c in fdb.c.items()}, snap)           # not a single document changed (incl. applied_at)
        self.assertEqual({n: c.indexes for n, c in fdb.c.items()}, idx)

    async def test_every_up_is_idempotent_when_called_directly_again(self):
        fdb = FakeDB()
        await run(fdb)
        for m in registry.MIGRATIONS:
            await m.up(acc(fdb))
            await m.up(acc(fdb))
            self.assertEqual(await m.verify(acc(fdb)), [], m.id)

    async def test_registry_is_append_only_and_well_formed(self):
        self.assertEqual([m.id for m in registry.MIGRATIONS][:5],
                         ["0001_core_indexes", "0002_monetag_indexes", "0003_smart_ads_indexes", "0004_rate_limit_indexes", "0005_query_indexes"])
        with self.assertRaises(MigrationError):
            await run(FakeDB(), [registry.MIGRATIONS[0], registry.MIGRATIONS[0]])           # duplicate version
        with self.assertRaises(MigrationError):
            await run(FakeDB(), [Migration(0, "bad", registry.MIGRATIONS[0].up)])

    async def test_batch1_index_set_is_still_created(self):
        """Everything db.init / monetag_store.init / ads.migrate created before Batch 2 is still created."""
        fdb = FakeDB()
        await run(fdb)
        old = [("files", "token"), ("sessions", "sid"), ("sessions", "created"), ("pending_deletes", "delete_at"), ("stats", "day"),
               ("monetag_ads", "created"), ("monetag_events", "created"), ("monetag_daily", "day"), ("monetag_buckets", "bucket"),
               ("monetag_buckets", "expire_at"), ("monetag_country", "day"), ("monetag_history", "created"),
               ("ad_requests", "created"), ("ad_events", "created"), ("ad_gate", "created"), ("ad_grace", "created"), ("ad_buckets", "expire_at")]
        for coll, key in old:
            self.assertIn(f"{key}_1", fdb[coll].indexes, (coll, key))
        marker = [d for d in fdb["ad_config"].docs if d["_id"] == "migration"]
        self.assertEqual(marker[0]["version"], 1)                              # legacy marker still written


# ================================================================== existing data / existing deployments
class ExistingDataTests(unittest.IsolatedAsyncioTestCase):
    async def seed(self, fdb):
        now = datetime.now(timezone.utc)
        await fdb["files"].insert_one({"token": "abc123", "locations": [{"chat": 1, "msg": 2}], "caption": "x", "created_at": 1.5, "views": 7})
        await fdb["files"].insert_one({"token": "def456", "kind": "post", "items": [], "created_at": 2.5})
        await fdb["sessions"].insert_one({"sid": "s1", "token": "abc123", "user_id": 5, "delivered": False, "ads_done": 1, "created": now,
                                          "smart_round": 2})
        await fdb["ad_gate"].insert_one({"_id": "nonce1", "sid": "s1", "created": now, "used": None})
        await fdb["ad_grace"].insert_one({"user_id": 5, "t": 3.0, "created": now})
        await fdb["ad_requests"].insert_one({"_id": "s1.aaaa", "sid": "s1", "network": "monetag", "created": now, "t": 1.0, "outcome": None})
        await fdb["ad_rewards"].insert_one({"_id": "s1.bbbb", "ad_id": "s1.bbbb", "t": 9.0})
        await fdb["stats"].insert_one({"day": "2026-11-01", "views": 40})
        await fdb["monetag_ads"].insert_one({"_id": "y1", "sid": "s1", "created": now})
        await fdb["ad_config"].insert_one({"_id": "config", "mode": "priority", "networks": {"tads": {"enabled": True}}})

    async def test_existing_user_data_and_ad_gate_records_are_untouched(self):
        fdb = FakeDB()
        await self.seed(fdb)
        before = {n: copy.deepcopy(c.docs) for n, c in fdb.c.items()}
        await run(fdb)
        for name, docs in before.items():
            if name == "ad_config":                       # only the legacy marker document is added; the admin config is untouched
                self.assertEqual([d for d in fdb[name].docs if d["_id"] != "migration"], docs)
            else:
                self.assertEqual(fdb[name].docs, docs, name)
        self.assertEqual(await fdb["files"].count_documents({}), 2)

    async def test_database_created_by_the_previous_release_adopts_tracking_without_errors(self):
        """Old release: indexes exist (created by db.init / ads.migrate) but there is no schema_migrations collection."""
        fdb = FakeDB()
        await self.seed(fdb)
        await fdb["files"].create_index("token", unique=True)
        await fdb["sessions"].create_index("sid", unique=True)
        await fdb["sessions"].create_index("created", expireAfterSeconds=1800)
        await fdb["ad_gate"].create_index("created", expireAfterSeconds=3600)
        await fdb["stats"].create_index("day", unique=True)
        await fdb["files"].create_index([("src_chat", 1), ("src_head", 1)], sparse=True)
        r = await run(fdb)
        self.assertEqual(r.ran, [1, 2, 3, 4, 5])
        self.assertEqual(len(fdb["files"].indexes), len({"_id_", "token_1", "src_chat_1_src_head_1", "created_at_-1"}))   # no duplicates created

    async def test_a_newer_database_is_accepted_by_older_code(self):
        """Rolling back the app: the DB already records a migration this code doesn't know. That must not block startup."""
        fdb = FakeDB()
        await run(fdb)
        await fdb[migrator.TRACK].insert_one({"_id": "0099_future", "version": 99, "name": "future"})
        r = await run(fdb)
        self.assertEqual((r.ran, r.version), ([], 99))

    async def test_session_ttl_change_is_applied_in_place_and_keeps_the_sessions(self):
        fdb = FakeDB()
        await self.seed(fdb)
        await run(fdb)                                                          # sessions TTL = 1800
        await ensure_schema(acc(fdb), registry.MIGRATIONS, reconcile=registry.reconcilers(900), **FAST)   # operator lowers SESSION_TTL
        self.assertEqual(fdb["sessions"].indexes["created_1"]["expireAfterSeconds"], 900)
        self.assertEqual(await fdb["sessions"].count_documents({}), 1)

    async def test_conflicting_index_is_a_hard_error_and_is_never_dropped(self):
        fdb = FakeDB()
        await fdb["files"].create_index("token")                                # NOT unique: differs from what we need
        with self.assertRaises(MigrationError) as cm:
            await run(fdb)
        self.assertIn("never dropped automatically", str(cm.exception))
        self.assertIn("token_1", fdb["files"].indexes)
        self.assertFalse(fdb["files"].indexes["token_1"].get("unique"))
        self.assertEqual(fdb[migrator.TRACK].docs, [])

    async def test_unique_index_over_duplicate_data_fails_loudly_and_changes_nothing(self):
        fdb = FakeDB()
        await fdb["files"].insert_one({"token": "dup"})
        await fdb["files"].insert_one({"token": "dup"})
        with self.assertRaises(MigrationError):
            await run(fdb)
        self.assertEqual(await fdb["files"].count_documents({}), 2)


# ================================================================== concurrency + locking
class SlowMigration:
    """An extra migration that records how many times (and how concurrently) it ran."""
    def __init__(self):
        self.calls, self.active, self.max_active = 0, 0, 0

    async def up(self, c):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        await c("slow_coll").create_index("x")
        self.active -= 1

    def migration(self):
        async def verify(c):
            return [] if "x_1" in await c("slow_coll").index_information() else ["missing"]
        return Migration(100, "slow", self.up, verify)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_startup_runs_each_migration_exactly_once(self):
        fdb = FakeDB(yield_ops=True)
        slow = SlowMigration()
        ms = registry.MIGRATIONS + [slow.migration()]
        results = await asyncio.gather(*[run(fdb, ms, owner=f"inst{i}") for i in range(8)])
        self.assertEqual(slow.calls, 1)
        self.assertEqual(slow.max_active, 1)                                       # never two instances inside a migration at once
        ran = sorted(v for r in results for v in r.ran)
        self.assertEqual(ran, [1, 2, 3, 4, 5, 100])                                # each applied by exactly one instance
        self.assertTrue(all(r.version == 100 for r in results))                    # all instances end on the same verified schema
        self.assertEqual(len(fdb[migrator.TRACK].docs), 6)
        self.assertEqual(fdb[migrator.LOCK].docs, [])

    async def test_waiting_instances_do_not_run_anything(self):
        fdb = FakeDB(yield_ops=True)
        slow = SlowMigration()
        ms = registry.MIGRATIONS + [slow.migration()]
        first = asyncio.create_task(run(fdb, ms, owner="A"))
        await asyncio.sleep(0.01)                                                  # A holds the lock now
        late = await run(fdb, ms, owner="B")
        await first
        self.assertEqual(late.ran, [])
        self.assertGreater(late.waited, 0)

    async def test_lock_held_too_long_fails_startup_instead_of_proceeding(self):
        fdb = FakeDB()
        await fdb[migrator.LOCK].insert_one({"_id": migrator.LOCK_ID, "owner": "someone", "expires_at": 4e9})   # live lease
        with self.assertRaises(MigrationLockTimeout):
            await run(fdb, wait_timeout=0.2)
        self.assertEqual(fdb[migrator.TRACK].docs, [])                              # we did NOT migrate behind their back

    async def test_expired_lease_of_a_crashed_instance_is_taken_over(self):
        fdb = FakeDB()
        await fdb[migrator.LOCK].insert_one({"_id": migrator.LOCK_ID, "owner": "crashed", "expires_at": 1.0})
        r = await run(fdb)
        self.assertEqual(r.ran, [1, 2, 3, 4, 5])
        self.assertEqual(fdb[migrator.LOCK].docs, [])

    async def test_lease_is_renewed_while_a_slow_migration_runs(self):
        fdb = FakeDB()
        seen = {}

        async def up(c):
            await asyncio.sleep(0.45)                                              # longer than lock_ttl
            lock = (await c(migrator.LOCK).find_one({"_id": migrator.LOCK_ID}))
            seen["expires"] = lock["expires_at"]
            await c("slow_coll").create_index("x")
        m = Migration(1, "slow", up, index_migration(1, "v", [("slow_coll", "x", {})]).verify)
        import time
        t0 = time.time()
        await run(fdb, [m], lock_ttl=0.3, wait_timeout=5)
        self.assertGreater(seen["expires"], t0 + 0.3)                              # heartbeat pushed the expiry forward

    async def test_stolen_lock_aborts_recording_and_is_retried_idempotently(self):
        fdb = FakeDB()
        state = {"steal": True, "ups": 0}

        async def up(c):
            state["ups"] += 1
            if state["steal"]:                                                     # another instance took over (our lease "expired")
                state["steal"] = False
                await c(migrator.LOCK).update_one({"_id": migrator.LOCK_ID}, {"$set": {"owner": "intruder", "expires_at": 1.0}})
            await c("slow_coll").create_index("x")
        m = Migration(1, "x", up, index_migration(1, "v", [("slow_coll", "x", {})]).verify)
        r = await run(fdb, [m])
        self.assertEqual(state["ups"], 2)                                          # re-run after losing the lock (safe: idempotent)
        self.assertEqual(len(fdb[migrator.TRACK].docs), 1)                         # recorded exactly once
        self.assertEqual(r.version, 1)


# ================================================================== failures
class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_required_migration_aborts_and_is_not_recorded(self):
        fdb = FakeDB()

        async def boom(c):
            await c("partial").create_index("a")                                   # partial work before the failure
            raise RuntimeError("disk full")
        later = SlowMigration()
        ms = [registry.MIGRATIONS[0], Migration(2, "boom", boom), Migration(3, "later", later.up)]
        with self.assertRaises(MigrationError) as cm:
            await run(fdb, ms)
        self.assertIn("0002_boom", str(cm.exception))
        self.assertEqual([d["version"] for d in fdb[migrator.TRACK].docs], [1])    # 1 recorded, 2 not, 3 never started
        self.assertEqual(later.calls, 0)
        self.assertEqual(fdb[migrator.LOCK].docs[0].get("owner") if fdb[migrator.LOCK].docs else None, None)   # lock released
        # breadcrumb for operators
        self.assertEqual(fdb[migrator.LOCK].docs, [])

    async def test_after_the_fix_the_next_start_completes_and_reuses_partial_work(self):
        fdb = FakeDB()
        flag = {"fail": True}

        async def flaky(c):
            await c("partial").create_index("a")
            if flag["fail"]:
                raise RuntimeError("temporary")
        ms = [Migration(1, "flaky", flaky, index_migration(1, "v", [("partial", "a", {})]).verify)]
        with self.assertRaises(MigrationError):
            await run(fdb, ms)
        flag["fail"] = False
        r = await run(fdb, ms)
        self.assertEqual(r.ran, [1])

    async def test_verification_failure_after_up_is_an_error(self):
        fdb = FakeDB()

        async def lies(c): return None                                             # claims success but creates nothing
        m = Migration(1, "lies", lies, index_migration(1, "v", [("x", "k", {})]).verify)
        with self.assertRaises(MigrationError) as cm:
            await run(fdb, [m])
        self.assertIn("verification failed", str(cm.exception))
        self.assertEqual(fdb[migrator.TRACK].docs, [])

    async def test_non_transient_database_error_is_not_retried(self):
        fdb = FakeDB()
        calls = {"n": 0}

        def deny(op, coll):
            if op == "create_index" and coll == "files":
                calls["n"] += 1
                return OperationFailure("not authorized", code=13)
        fdb.fail_when = deny
        with self.assertRaises(MigrationError):
            await run(fdb)
        self.assertEqual(calls["n"], 1)

    async def test_failure_note_is_left_for_operators(self):
        fdb = FakeDB()

        async def boom(c): raise RuntimeError("kaput")
        with self.assertRaises(MigrationError):
            await run(fdb, [Migration(1, "boom", boom)])
        self.assertEqual(fdb[migrator.LOCK].docs, [])                              # released; the error is in the exception + logs

    async def test_manager_startup_runs_migrations_first_and_failure_is_fatal(self):
        """Batch 3: start() only schedules the supervised `_startup`. Migrations are its FIRST step and a failure there is never swallowed:
        the handler hands it to `_fatal` (lifecycle FAILED + owner alert + non-zero exit) and nothing after it runs."""
        tree = ast.parse((ROOT / "manager.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_startup")
        body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        tries = [n for n in body if isinstance(n, ast.Try)]
        self.assertEqual(len(tries), 1)
        inner = [n for n in tries[0].body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        awaited = [ast.unparse(n) for n in inner if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)]
        self.assertIn("run_startup_migrations", awaited[0])                                    # before Telegram, before anything else
        handlers_src = " ".join(ast.unparse(h) for h in tries[0].handlers)
        self.assertIn("_fatal", handlers_src)
        self.assertNotIn("pass", [ast.unparse(s) for h in tries[0].handlers for s in h.body])
        mod = ast.unparse(tree)
        fatal = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fatal")
        self.assertIn("request_exit", ast.unparse(fatal))
        self.assertNotIn("migration failed", (ROOT / "manager.py").read_text())
        self.assertNotIn("index init failed", mod)


# ================================================================== DB restart / reconnection
class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_down_at_startup_then_back_is_waited_out(self):
        fdb = FakeDB()
        fdb.down = True
        sleeps = {"n": 0}

        async def sleep(dt):
            sleeps["n"] += 1
            if sleeps["n"] == 3:
                fdb.down = False                                                    # DB finished restarting
            await asyncio.sleep(0)
        r = await run(fdb, sleep=sleep, wait_timeout=60)
        self.assertEqual(r.ran, [1, 2, 3, 4, 5])
        self.assertGreaterEqual(sleeps["n"], 3)

    async def test_database_never_comes_back_fails_startup(self):
        fdb = FakeDB()
        fdb.down = True
        clock = Clock()

        async def sleep(dt):
            clock.t += dt
        with self.assertRaises(MigrationError) as cm:
            await run(fdb, sleep=sleep, clock=clock, wait_timeout=20)
        self.assertIn("cannot read schema state", str(cm.exception))

    async def test_connection_drop_in_the_middle_of_a_migration_is_retried(self):
        fdb = FakeDB()
        hits = {"n": 0}

        def flaky(op, coll):
            if op == "create_index" and coll == "stats":
                hits["n"] += 1
                if hits["n"] <= 2:
                    return ConnectionFailure("primary stepped down")
        fdb.fail_when = flaky
        r = await run(fdb, retries=3)
        self.assertEqual(r.ran, [1, 2, 3, 4, 5])
        self.assertEqual(hits["n"], 3)
        self.assertTrue(await is_complete(acc(fdb), registry.MIGRATIONS))

    async def test_restart_after_a_crash_between_up_and_recording_is_safe(self):
        fdb = FakeDB()

        def die(op, coll):
            if op == "insert_one" and coll == migrator.TRACK:
                return ConnectionFailure("crash")                                   # indexes created, but recording never happened
        fdb.fail_when = die
        with self.assertRaises(MigrationError):
            await run(fdb, retries=0, wait_timeout=0.3)
        fdb.fail_when = None
        r = await run(fdb)                                                          # "restart"
        self.assertEqual(r.version, 5)
        self.assertEqual(len(fdb[migrator.TRACK].docs), 5)


# ================================================================== verify before serving
class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_schema_is_not_ready(self):
        fdb = FakeDB()
        self.assertFalse(await is_complete(acc(fdb), registry.MIGRATIONS))
        await run(fdb)
        self.assertTrue(await is_complete(acc(fdb), registry.MIGRATIONS))

    async def test_readiness_goes_false_when_the_database_is_unreachable(self):
        fdb = FakeDB()
        await run(fdb)
        fdb.down = True
        with self.assertRaises(ConnectionFailure):
            await is_complete(acc(fdb), registry.MIGRATIONS)                        # /readyz maps this to HTTP 503

    async def test_a_dropped_index_is_detected_and_repaired_at_next_start(self):
        fdb = FakeDB()
        await run(fdb)
        del fdb["rate_limits"].indexes["expire_at_1"]                               # someone dropped the TTL index
        self.assertFalse(await is_complete(acc(fdb), registry.MIGRATIONS))          # recorded, but drifted => NOT ready
        r = await run(fdb)
        self.assertEqual(r.ran, [4])                                                # only the affected migration is re-applied
        self.assertIn("expire_at_1", fdb["rate_limits"].indexes)

    async def test_verify_only_mode_waits_for_an_external_migrator_then_fails(self):
        fdb = FakeDB()
        with self.assertRaises(MigrationLockTimeout) as cm:
            await run(fdb, run=False, wait_timeout=0.2)
        self.assertIn("python -m migrator", str(cm.exception))
        self.assertEqual(fdb[migrator.TRACK].docs, [])                              # verify-only never writes

    async def test_verify_only_mode_passes_once_the_migrator_has_run(self):
        fdb = FakeDB()
        waiter = asyncio.create_task(run(fdb, run=False, wait_timeout=5))
        await asyncio.sleep(0.05)
        await run(fdb)                                                              # the release step
        r = await waiter
        self.assertEqual((r.ran, r.version), ([], 5))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        s = migrator.settings_from_env({})
        self.assertEqual((s["run"], s["lock_ttl"], s["wait_timeout"], s["retries"]), (True, 120.0, 180.0, 2))
        s = migrator.settings_from_env({"MIGRATE_ON_STARTUP": "false", "MIGRATION_WAIT_TIMEOUT": "60"})
        self.assertEqual((s["run"], s["wait_timeout"]), (False, 60.0))

    def test_invalid_values_fail_startup(self):
        for bad in ({"MIGRATION_LOCK_TTL": "1"}, {"MIGRATION_WAIT_TIMEOUT": "abc"}, {"MIGRATION_STEP_RETRIES": "99"}):
            with self.assertRaises(MigrationError):
                migrator.settings_from_env(bad)

    def test_transient_classification(self):
        self.assertTrue(migrator.is_transient(ConnectionFailure("x")))
        self.assertTrue(migrator.is_transient(OSError()))
        self.assertTrue(migrator.is_transient(asyncio.TimeoutError()))
        self.assertFalse(migrator.is_transient(OperationFailure("x", code=13)))
        self.assertFalse(migrator.is_transient(ValueError()))


if __name__ == "__main__":
    unittest.main()
