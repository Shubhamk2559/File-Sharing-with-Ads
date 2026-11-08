"""Reliable MongoDB schema migrations: versioned, idempotent, multi-instance safe, verified before the app serves traffic.

Collections (both tiny, created on demand, never touched by the application otherwise)
  schema_migrations   one document per APPLIED migration, `_id` = "0003_smart_ads_indexes" (unique => atomic tracking)
  schema_lock         single document `_id: "migrations"` = lease {owner, expires_at}; the migration lock

Startup protocol (`ensure_schema`)
  1. Read what is recorded and VERIFY it (every expected index really exists with the expected options). Nothing pending => no lock,
     no writes: N instances restarting at once is a read-only no-op.
  2. Something pending => try to take the lock. Exactly one instance wins (conditional update / insert on a fixed `_id`; the loser gets
     DuplicateKeyError). The winner re-checks under the lock, runs each pending migration in version order, verifies it, records it, and
     releases the lock. A heartbeat keeps the lease alive during slow index builds; an instance that dies simply lets the lease expire and
     the next instance takes over (migrations are idempotent, so re-running half-finished work is safe).
  3. Losers wait (bounded by MIGRATION_WAIT_TIMEOUT) until everything is recorded and verified. They never run migrations concurrently.
  4. Any failure => MigrationError => startup aborts (the process exits non-zero, the platform restarts it). Nothing "continues anyway".
  5. `reconcile` steps (idempotent, env-dependent things such as the session TTL index) run every start after the versioned migrations.

Existing data is never modified: migrations only create/verify indexes and write their own bookkeeping documents. Index creation with the same
key+options is a no-op in MongoDB, so databases created by earlier releases (which created the same indexes without tracking) simply get
their migrations recorded on first start. Index NAMES are never customised, so they match the ones already in production.

Pure module (no motor/config import) so it is unit-testable; `python -m migrator` runs the same code from a shell (release step).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Mapping

from ads.store import DuplicateKeyError          # pymongo's class in production, a stand-in where pymongo isn't installed

log = logging.getLogger("migrator")

TRACK, LOCK, LOCK_ID = "schema_migrations", "schema_lock", "migrations"
INDEX_OPTIONS_CONFLICT, INDEX_KEY_SPECS_CONFLICT = 85, 86          # MongoDB error codes
_TRANSIENT_NAMES = {"AutoReconnect", "ConnectionFailure", "ServerSelectionTimeoutError", "NetworkTimeout", "NotPrimaryError",
                    "NotMasterError", "ExecutionTimeout", "WaitQueueTimeoutError"}


class MigrationError(RuntimeError):
    """A required migration failed / could not be verified. The application must not start."""


class MigrationLockTimeout(MigrationError):
    """Another instance held the migration lock (or the schema stayed incomplete) for longer than MIGRATION_WAIT_TIMEOUT."""


class _LockLost(Exception):
    pass


def is_transient(e: BaseException) -> bool:
    """Connectivity-type errors that are worth retrying (DB restart, failover, network blip)."""
    if isinstance(e, (OSError, asyncio.TimeoutError)):
        return True
    return any(c.__name__ in _TRANSIENT_NAMES for c in type(e).__mro__)


# ------------------------------------------------------------------ definitions
Accessor = Callable[[str], object]                       # c(name) -> collection


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[Accessor], Awaitable[None]]            # MUST be idempotent
    verify: Callable[[Accessor], Awaitable[list]] | None = None   # -> list of problems ([] = fine); also used to detect drift

    @property
    def id(self) -> str:
        return f"{self.version:04d}_{self.name}"


def _validate(migrations: Iterable[Migration]) -> list[Migration]:
    ms = sorted(migrations, key=lambda m: m.version)
    seen = set()
    for m in ms:
        if m.version < 1 or m.version in seen:
            raise MigrationError(f"invalid or duplicate migration version {m.version}")
        seen.add(m.version)
    return ms


# ------------------------------------------------------------------ indexes
def norm_keys(keys) -> list[tuple]:
    return [(keys, 1)] if isinstance(keys, str) else [(k, int(d)) for k, d in keys]


_OPT_KEYS = ("unique", "sparse", "partialFilterExpression")


def _opt_view(d: Mapping) -> dict:
    return {k: d[k] for k in _OPT_KEYS if d.get(k)}


async def ensure_index(coll, keys, **opts) -> None:
    """create_index that is idempotent AND copes with option drift. A TTL value that merely differs (e.g. SESSION_TTL was changed) is
    updated in place with collMod; any other conflict is a hard error (never auto-dropped: that could destroy a unique constraint)."""
    try:
        await coll.create_index(keys, **opts)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) not in (INDEX_OPTIONS_CONFLICT, INDEX_KEY_SPECS_CONFLICT):
            raise
        nk = norm_keys(keys)
        info = await coll.index_information()
        existing = next((i for i in info.values() if [(k, int(d)) for k, d in i["key"]] == nk), None)
        if (existing and "expireAfterSeconds" in opts and existing.get("expireAfterSeconds") is not None
                and _opt_view(existing) == _opt_view(opts)):
            await coll.database.command("collMod", coll.name, index={"keyPattern": dict(nk), "expireAfterSeconds": int(opts["expireAfterSeconds"])})
            log.warning("TTL of %s%s changed %s -> %s", coll.name, nk, existing.get("expireAfterSeconds"), opts["expireAfterSeconds"])
            return
        raise MigrationError(f"index conflict on {coll.name} {nk}: an index with the same key but different options already exists "
                             f"(existing={_opt_view(existing or {})}, wanted={_opt_view(opts)}). Resolve it manually; it is never dropped automatically.") from e


async def index_problems(coll, keys, opts: Mapping) -> list[str]:
    nk = norm_keys(keys)
    info = await coll.index_information()
    existing = next((i for i in info.values() if [(k, int(d)) for k, d in i["key"]] == nk), None)
    if existing is None:
        return [f"{coll.name}: missing index {nk}"]
    out = []
    if _opt_view(existing) != _opt_view(opts):
        out.append(f"{coll.name} {nk}: options differ (existing={_opt_view(existing)}, wanted={_opt_view(opts)})")
    if "expireAfterSeconds" in opts and existing.get("expireAfterSeconds") != int(opts["expireAfterSeconds"]):
        out.append(f"{coll.name} {nk}: expireAfterSeconds is {existing.get('expireAfterSeconds')}, wanted {opts['expireAfterSeconds']}")
    return out


def index_migration(version: int, name: str, specs: list, extra_up: Callable | None = None) -> Migration:
    """Migration made of (collection, keys, options) index specs (+ optional extra idempotent step)."""
    async def up(c):
        for coll, keys, opts in specs:
            await ensure_index(c(coll), keys, **opts)
        if extra_up:
            await extra_up(c)

    async def verify(c):
        probs: list = []
        for coll, keys, opts in specs:
            probs += await index_problems(c(coll), keys, opts)
        return probs

    return Migration(version, name, up, verify)


# ------------------------------------------------------------------ lock (lease)
async def _acquire(c, owner: str, ttl: float, now: float) -> bool:
    col = c(LOCK)
    doc = {"owner": owner, "expires_at": now + ttl, "acquired_at": now}
    if await col.find_one_and_update({"_id": LOCK_ID, "expires_at": {"$lt": now}}, {"$set": doc}):    # expired lease: atomic takeover
        return True
    try:
        await col.insert_one(dict(doc, _id=LOCK_ID))                                                   # no lock yet: unique _id decides
        return True
    except DuplicateKeyError:
        return False


async def _renew(c, owner: str, ttl: float, now: float) -> bool:
    return await c(LOCK).find_one_and_update({"_id": LOCK_ID, "owner": owner}, {"$set": {"expires_at": now + ttl}}) is not None


async def _release(c, owner: str) -> None:
    try:
        await c(LOCK).delete_one({"_id": LOCK_ID, "owner": owner})       # only our own lease; best effort (an expired lease is taken over anyway)
    except Exception:  # noqa: BLE001
        log.warning("could not release the migration lock (it expires on its own)")


class _Lease:
    def __init__(self, c, owner, ttl, clock):
        self.c, self.owner, self.ttl, self.clock = c, owner, ttl, clock
        self.lost, self._task = False, None

    async def _beat(self):
        while True:
            await asyncio.sleep(max(0.05, self.ttl / 3))          # real sleep on purpose: a stubbed sleep must never turn this into a busy loop
            try:
                if not await _renew(self.c, self.owner, self.ttl, self.clock()):
                    self.lost = True
                    return
            except Exception:  # noqa: BLE001  - transient: the next beat retries; the lease still has 2/3 of its TTL left
                log.warning("migration lock renewal failed (will retry)")

    def start(self):
        self._task = asyncio.ensure_future(self._beat())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001
                pass

    async def check(self):
        """Raise if we no longer own the lock (checked right before anything is recorded)."""
        if self.lost or not await _renew(self.c, self.owner, self.ttl, self.clock()):
            self.lost = True
            raise _LockLost()


# ------------------------------------------------------------------ runner
@dataclass
class Result:
    version: int                                        # highest recorded version
    ran: list = field(default_factory=list)             # migrations THIS process applied
    waited: float = 0.0                                 # seconds spent waiting for another instance
    reconciled: int = 0


async def _applied(c) -> dict:
    docs = await c(TRACK).find({}).to_list(length=10_000)
    return {int(d["version"]): d for d in docs if "version" in d}


async def _pending(c, migrations: list) -> list:
    """[(migration, [reasons])] for migrations that are not recorded OR whose verification fails (drift, e.g. a dropped index)."""
    applied = await _applied(c)
    out = []
    for m in migrations:
        if m.version not in applied:
            out.append((m, ["not recorded"]))
        elif m.verify:
            probs = await m.verify(c)
            if probs:
                out.append((m, probs))
    return out


async def is_complete(c, migrations: Iterable[Migration]) -> bool:
    """Read-only readiness check: everything recorded and verified. Raises on DB errors (caller decides)."""
    return not await _pending(c, _validate(migrations))


async def _retry(fn: Callable[[], Awaitable], *, deadline: float, clock, sleep, what: str):
    """Retry connectivity errors with backoff until the deadline (DB restarting while the app boots); everything else propagates."""
    delay = 0.25
    while True:
        try:
            return await fn()
        except _LockLost:
            raise
        except Exception as e:  # noqa: BLE001
            if not is_transient(e) or clock() >= deadline:
                raise
            log.warning("%s: database not reachable (%s); retrying in %.1fs", what, type(e).__name__, delay)
            await sleep(delay)
            delay = min(delay * 2, 5.0)


async def _run_one(c, m: Migration, lease: _Lease, retries: int, clock, sleep) -> None:
    for attempt in range(retries + 1):
        try:
            await lease.check()
            t0 = clock()
            await m.up(c)
            probs = await m.verify(c) if m.verify else []
            if probs:
                raise MigrationError(f"migration {m.id} ran but verification failed: " + "; ".join(probs[:5]))
            await lease.check()                          # still ours? only then record it
            try:
                await c(TRACK).insert_one({"_id": m.id, "version": m.version, "name": m.name, "applied_at": datetime.now(timezone.utc),
                                           "duration_ms": int((clock() - t0) * 1000), "owner": lease.owner})
            except DuplicateKeyError:
                pass                                     # recorded by someone else meanwhile: same idempotent result
            log.info("migration %s applied", m.id)
            return
        except (MigrationError, _LockLost):
            raise
        except Exception as e:  # noqa: BLE001
            if is_transient(e) and attempt < retries:
                log.warning("migration %s hit %s (attempt %d/%d); retrying", m.id, type(e).__name__, attempt + 1, retries + 1)
                await sleep(min(0.5 * 2 ** attempt, 5.0))
                continue
            raise MigrationError(f"migration {m.id} failed: {type(e).__name__}: {e}") from e


async def ensure_schema(c: Accessor, migrations: Iterable[Migration], *, reconcile: Iterable[Callable] = (), run: bool = True,
                        owner: str | None = None, lock_ttl: float = 120.0, wait_timeout: float = 180.0, poll: float = 1.0,
                        retries: int = 2, clock: Callable[[], float] = time.time, sleep=asyncio.sleep) -> Result:
    """Bring the database to the expected schema and prove it. Raises MigrationError / MigrationLockTimeout; never returns on failure.

    run=False: verify only (an external release step runs `python -m migrator`); waits up to wait_timeout for it, then fails."""
    ms = _validate(migrations)
    owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    t0 = clock()
    deadline = t0 + wait_timeout
    res = Result(version=0)
    waited = 0.0

    while True:
        try:
            pending = await _retry(lambda: _pending(c, ms), deadline=deadline, clock=clock, sleep=sleep, what="schema check")
        except Exception as e:  # noqa: BLE001
            raise MigrationError(f"cannot read schema state: {type(e).__name__}: {e}") from e
        if not pending:
            break
        if not run:
            if clock() >= deadline:
                raise MigrationLockTimeout("schema is not up to date and MIGRATE_ON_STARTUP=false: run `python -m migrator` first; pending: "
                                           + ", ".join(m.id for m, _ in pending))
            await sleep(poll)
            continue

        try:
            got = await _retry(lambda: _acquire(c, owner, lock_ttl, clock()), deadline=deadline, clock=clock, sleep=sleep, what="migration lock")
        except Exception as e:  # noqa: BLE001
            raise MigrationError(f"cannot take the migration lock: {type(e).__name__}: {e}") from e
        if not got:
            if clock() >= deadline:
                raise MigrationLockTimeout(f"another instance held the migration lock for more than {wait_timeout:.0f}s")
            ws = clock()
            await sleep(poll)
            waited += clock() - ws
            continue

        lease = _Lease(c, owner, lock_ttl, clock)
        lease.start()
        try:
            try:
                todo = await _retry(lambda: _pending(c, ms), deadline=deadline, clock=clock, sleep=sleep, what="schema re-check")
                for m, why in todo:                          # re-check UNDER the lock: someone may have finished while we waited
                    log.info("applying migration %s (%s)", m.id, "; ".join(why)[:200])
                    await _run_one(c, m, lease, retries, clock, sleep)
                    res.ran.append(m.version)
            except _LockLost:
                log.warning("migration lock lost (another instance took over); re-checking")
                continue
            except MigrationError as e:
                await _note_failure(c, owner, str(e))
                raise
            except Exception as e:  # noqa: BLE001
                await _note_failure(c, owner, f"{type(e).__name__}: {e}")
                raise MigrationError(f"migration run failed: {type(e).__name__}: {e}") from e
        finally:
            await lease.stop()
            await _release(c, owner)
        # loop: re-verify from scratch. If a migration we just ran is STILL pending, verification inside _run_one already raised.

    for r in reconcile:
        try:
            await _retry(lambda r=r: r(c), deadline=deadline, clock=clock, sleep=sleep, what="reconcile")
        except MigrationError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MigrationError(f"startup reconcile step failed: {type(e).__name__}: {e}") from e
        res.reconciled += 1

    applied = await _retry(lambda: _applied(c), deadline=deadline, clock=clock, sleep=sleep, what="schema state")
    res.version = max(applied) if applied else 0
    res.waited = waited
    log.info("schema ready: version=%s ran=%s waited=%.1fs", res.version, res.ran, waited)
    return res


async def _note_failure(c, owner: str, msg: str) -> None:
    """Leave a breadcrumb for operators on the lock document (best effort)."""
    try:
        await c(LOCK).update_one({"_id": LOCK_ID, "owner": owner}, {"$set": {"last_error": msg[:500], "last_error_at": time.time()}})
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ environment
def settings_from_env(env: Mapping | None = None) -> dict:
    env = os.environ if env is None else env

    def num(name, default, lo, hi, cast=float):
        raw = str(env.get(name, "")).strip()
        if not raw:
            return default
        try:
            v = cast(raw)
        except ValueError:
            raise MigrationError(f"{name} must be a number")
        if not lo <= v <= hi:
            raise MigrationError(f"{name} must be between {lo} and {hi}")
        return v

    return {
        "run": str(env.get("MIGRATE_ON_STARTUP", "true")).strip().lower() in ("1", "true", "yes", "on"),
        "lock_ttl": num("MIGRATION_LOCK_TTL", 120.0, 10.0, 3600.0),
        "wait_timeout": num("MIGRATION_WAIT_TIMEOUT", 180.0, 5.0, 3600.0),
        "poll": num("MIGRATION_POLL_INTERVAL", 1.0, 0.05, 30.0),
        "retries": int(num("MIGRATION_STEP_RETRIES", 2, 0, 10, int)),
    }


def main() -> None:  # pragma: no cover - needs a real MongoDB
    """python -m migrator   (idempotent; safe to run while the app is running or from a release step)"""
    import sys
    from motor.motor_asyncio import AsyncIOMotorClient
    from migrations import registry
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db = AsyncIOMotorClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=15000)[os.getenv("DB_NAME", "filebot")]
    s = settings_from_env()
    s["run"] = True
    try:
        r = asyncio.run(ensure_schema(lambda n: db[n], registry.MIGRATIONS, reconcile=registry.reconcilers(int(os.getenv("SESSION_TTL", "1800"))), **s))
    except MigrationError as e:
        print("MIGRATION FAILED:", e, file=sys.stderr)
        sys.exit(1)
    print(f"schema ready: version={r.version} applied_now={r.ran}")


if __name__ == "__main__":
    main()
