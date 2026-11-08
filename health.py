"""Process lifecycle, readiness and supervised background tasks (Batch 3). Pure asyncio: no telegram / motor / FastAPI import.

  GET /       LIVENESS.  Process up and event loop answers. Never touches MongoDB or Telegram.
  GET /readyz READINESS. Startup finished (migrations verified, Telegram initialised) AND MongoDB answers AND every migration verified.
While the lifecycle is not READY (starting / failed / stopping) HealthGate answers every other route with 503 + Retry-After.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import signal
import time
from typing import Awaitable, Callable

import observability as obs
from ads import slog

log = logging.getLogger("health")

# matched by NAME so this module needs neither telegram nor pymongo
TRANSIENT_NAMES = frozenset({
    "NetworkError", "TimedOut", "RetryAfter", "ConnectTimeout", "ReadTimeout", "ConnectError",
    "AutoReconnect", "ConnectionFailure", "ServerSelectionTimeoutError", "NetworkTimeout", "NotPrimaryError", "NotMasterError",
    "ExecutionTimeout", "WaitQueueTimeoutError",
    "TimeoutError", "ConnectionError", "ConnectionResetError", "BrokenPipeError",
})
PERMANENT_TELEGRAM_NAMES = frozenset({"InvalidToken", "Unauthorized"})


def is_transient(e: BaseException) -> bool:
    if isinstance(e, (OSError, asyncio.TimeoutError)):
        return True
    return any(c.__name__ in TRANSIENT_NAMES for c in type(e).__mro__)


def is_permanent_telegram(e: BaseException) -> bool:
    return any(c.__name__ in PERMANENT_TELEGRAM_NAMES for c in type(e).__mro__)


def _retry_after(e: BaseException) -> float | None:
    ra = getattr(e, "retry_after", None)
    if ra is None:
        return None
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def retry_until(fn: Callable[[], Awaitable], *, timeout: float, what: str, base: float = 1.0, cap: float = 30.0,
                      sleep=asyncio.sleep, clock: Callable[[], float] = time.monotonic, classify=is_transient):
    """Retry transient errors with exponential backoff until `timeout`; anything else (and the last error after the deadline) is raised."""
    deadline = clock() + timeout
    delay, attempt = base, 0
    while True:
        attempt += 1
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if is_permanent_telegram(e) or not classify(e) or clock() >= deadline:
                raise
            ra = _retry_after(e)
            wait = min(max(ra, 0.0), 60.0) if ra else delay
            slog.event("retrying", level="warning", what=what, attempt=attempt, error=type(e).__name__, wait=round(wait, 1))
            await sleep(wait)
            delay = min(delay * 2, cap)


# ====================================================================== lifecycle
class State(str, enum.Enum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class Lifecycle:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.state = State.NEW
        self.phase = "new"
        self.created_at = clock()
        self.ready_at: float | None = None
        self.failure: dict | None = None
        self.exit_code: int = 0
        self._exit_cb: Callable[[int], None] | None = None

    @property
    def accepting(self) -> bool:
        return self.state is State.READY

    def begin_start(self) -> bool:
        """False if a startup was already begun: the caller must NOT start anything again."""
        if self.state is not State.NEW:
            return False
        self.state, self.phase = State.STARTING, "starting"
        return True

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def mark_ready(self) -> None:
        if self.state is State.STARTING:
            self.state, self.phase, self.ready_at = State.READY, "ready", self.clock()

    def fail(self, phase: str, error: str) -> None:
        if self.state in (State.STOPPING, State.STOPPED):
            return
        self.state, self.phase = State.FAILED, phase
        self.failure = {"phase": phase, "error": error, "at": obs._iso(self.clock())}

    def begin_stopping(self) -> bool:
        if self.state in (State.STOPPING, State.STOPPED):
            return False
        self.state, self.phase = State.STOPPING, "stopping"
        return True

    def mark_stopped(self) -> None:
        self.state, self.phase = State.STOPPED, "stopped"

    def set_exit_callback(self, cb: Callable[[int], None] | None) -> None:
        self._exit_cb = cb

    def request_exit(self, code: int = 1) -> None:
        """A permanent startup failure must end the process NON-ZERO so the platform restarts it / flags the deployment."""
        self.exit_code = code
        if self._exit_cb:
            self._exit_cb(code)
        else:                                   # plain `uvicorn main:app`: SIGTERM -> graceful shutdown
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass

    def snapshot(self) -> dict:
        now = self.clock()
        return {"state": self.state.value, "phase": self.phase, "uptime_s": round(now - self.created_at, 1),
                "ready_since": obs._iso(self.ready_at) if self.ready_at else None, "failure": self.failure}


LIFECYCLE = Lifecycle()


# ====================================================================== readiness
class Readiness:
    """/readyz logic. The public endpoint does real work at most once per `cache_seconds` (single flight)."""

    def __init__(self, lifecycle: Lifecycle, ping: Callable[[], Awaitable], migrations_complete: Callable[[], Awaitable[bool]], *,
                 cache_seconds: float = 3.0, timeout: float = 3.0, clock: Callable[[], float] = time.monotonic):
        self.lc, self.ping, self.migrations_complete = lifecycle, ping, migrations_complete
        self.cache_seconds, self.timeout, self.clock = cache_seconds, timeout, clock
        self._lock = asyncio.Lock()
        self._cached: tuple | None = None          # (expires, ok, body)

    async def check(self) -> tuple[bool, dict]:
        if not self.lc.accepting:                  # starting / failed / stopping: never touch the database
            return False, {"status": "not_ready",
                           "checks": {"startup": self.lc.state.value, "database": "unknown", "migrations": "unknown"}}
        now = self.clock()
        if self._cached and now < self._cached[0]:
            return self._cached[1], self._cached[2]
        async with self._lock:
            now = self.clock()
            if self._cached and now < self._cached[0]:
                return self._cached[1], self._cached[2]
            checks = {"startup": "ready", "database": "ok", "migrations": "ok"}
            try:
                await asyncio.wait_for(self.ping(), self.timeout)
            except Exception:  # noqa: BLE001
                checks["database"], checks["migrations"] = "unavailable", "unknown"
            else:
                try:
                    if not await asyncio.wait_for(self.migrations_complete(), self.timeout):
                        checks["migrations"] = "pending"
                except Exception:  # noqa: BLE001
                    checks["database"], checks["migrations"] = "unavailable", "unknown"
            ok = checks["database"] == "ok" and checks["migrations"] == "ok"
            body = {"status": "ready" if ok else "not_ready", "checks": checks}
            self._cached = (self.clock() + self.cache_seconds, ok, body)
            if not ok:
                obs.METRICS.inc("readyz.not_ready")
            return ok, body


# ====================================================================== traffic gate (ASGI)
class HealthGate:
    """Pure ASGI middleware: while not READY every HTTP route except the health probes answers 503 + Retry-After."""

    OPEN = ("/", "/readyz")

    def __init__(self, app, lifecycle: Lifecycle = LIFECYCLE, open_paths=OPEN):
        self.app, self.lc, self.open = app, lifecycle, tuple(open_paths)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and not self.lc.accepting and scope.get("path") not in self.open:
            obs.METRICS.inc("http.rejected_not_ready")
            body = b'{"ok":false,"code":"unavailable","error":"Service is starting or restarting. Please retry shortly."}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5"), (b"cache-control", b"no-store"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


# ====================================================================== background tasks
class _Task:
    __slots__ = ("name", "task", "restart", "status", "crashes", "restarts", "last_error", "last_crash_at")

    def __init__(self, name, restart):
        self.name, self.restart, self.task = name, restart, None
        self.status, self.crashes, self.restarts, self.last_error, self.last_crash_at = "new", 0, 0, None, None


class TaskSupervisor:
    """Runs named background coroutines exactly once each.

    * start(name) is idempotent: a second start of a task still alive is IGNORED (and logged) => no duplicate optimiser / cleanup loops.
    * A crash is logged, counted and the task restarts with exponential backoff (reset after `stable_after` seconds of stable running).
      on_crash(name, consecutive, exc) is awaited at the 3rd and then every 10th consecutive crash.
    * A task that RETURNS normally is finished (feature switched off); it is not restarted.
    * stop() cancels everything and waits (bounded). After stop(), start() refuses.
    """

    def __init__(self, *, sleep=asyncio.sleep, clock: Callable[[], float] = time.monotonic, on_crash=None, base_backoff: float = 1.0,
                 max_backoff: float = 60.0, stable_after: float = 300.0):
        self._t: dict = {}
        self._sleep, self._clock, self._on_crash = sleep, clock, on_crash
        self.base, self.cap, self.stable_after = base_backoff, max_backoff, stable_after
        self._closed = False

    def start(self, name: str, factory: Callable[[], Awaitable], *, restart: bool = True) -> bool:
        if self._closed:
            slog.event("background_task_refused", level="warning", task=name, reason="shutting_down")
            return False
        cur = self._t.get(name)
        if cur and cur.task is not None and not cur.task.done():
            slog.event("background_task_duplicate_ignored", level="warning", task=name)
            obs.METRICS.inc("tasks.duplicate_ignored")
            return False
        st = _Task(name, restart)
        st.task = asyncio.get_running_loop().create_task(self._run(st, factory), name=f"bg:{name}")
        self._t[name] = st
        slog.event("background_task_started", task=name)
        return True

    async def _run(self, st: _Task, factory) -> None:
        backoff, consecutive = self.base, 0
        while True:
            t0 = self._clock()
            st.status = "running"
            try:
                await factory()
                st.status = "finished"
                return
            except asyncio.CancelledError:
                st.status = "cancelled"
                raise
            except Exception as e:  # noqa: BLE001
                st.crashes += 1
                st.last_error, st.last_crash_at = type(e).__name__, time.time()
                if self._clock() - t0 >= self.stable_after:
                    backoff, consecutive = self.base, 0
                consecutive += 1
                obs.METRICS.inc("tasks.crashes")
                log.error("background task %s crashed (%s)", st.name, type(e).__name__, exc_info=True)
                if self._on_crash and (consecutive == 3 or (consecutive > 3 and consecutive % 10 == 0)):
                    try:
                        await self._on_crash(st.name, consecutive, e)
                    except Exception:  # noqa: BLE001
                        pass
                if not st.restart or self._closed:
                    st.status = "crashed"
                    return
                st.status = "restarting"
                st.restarts += 1
                await self._sleep(backoff)
                backoff = min(backoff * 2, self.cap)

    def running(self, name: str) -> bool:
        t = self._t.get(name)
        return bool(t and t.task is not None and not t.task.done())

    async def stop(self, timeout: float = 5.0) -> list:
        """Cancel all tasks, wait up to `timeout`; returns names that did not finish in time."""
        self._closed = True
        tasks = [t.task for t in self._t.values() if t.task is not None and not t.task.done()]
        for t in tasks:
            t.cancel()
        stuck: list = []
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            stuck = [t.get_name().removeprefix("bg:") for t in pending]
        return stuck

    def snapshot(self) -> dict:
        return {n: {"status": ("running" if self.running(n) else t.status), "crashes": t.crashes, "restarts": t.restarts,
                    "last_error": t.last_error} for n, t in self._t.items()}


# ====================================================================== MongoDB monitor
async def db_monitor_loop(ping: Callable[[], Awaitable], dep: obs.Dependency, alerts: obs.AlertManager, *, interval: float,
                          timeout: float = 5.0, sleep=asyncio.sleep) -> None:
    """Pings MongoDB in the background. Motor reconnects by itself; this loop makes the outage VISIBLE (one log line + one owner alert at
    the transition) and notices the recovery. Polls faster while failing."""
    while True:
        try:
            await asyncio.wait_for(ping(), timeout)
            ev = dep.success()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            ev = dep.failure(e)
            slog.event("mongodb_ping_failed", level="warning", error=type(e).__name__, consecutive=dep.consecutive_failures)
            obs.METRICS.inc("db.ping_failed")
        if ev == "down":
            slog.event("mongodb_unavailable", level="error", error=dep.last_error, failures=dep.consecutive_failures)
            await alerts.alert("mongodb_down", f"MongoDB is unreachable ({dep.last_error}, {dep.consecutive_failures} failed checks). "
                                               "Rate limits run in degraded (local) mode and most requests answer 503 until it is back. "
                                               "Check Atlas status / network allow-list.")
        elif ev == "recovered":
            slog.event("mongodb_recovered", downtime_s=round(dep.last_downtime or 0, 1))
            await alerts.recover("mongodb_down", f"MongoDB connection restored after ~{int(dep.last_downtime or 0)}s.")
        await sleep(interval if dep.consecutive_failures == 0 else max(2.0, interval / 3))
