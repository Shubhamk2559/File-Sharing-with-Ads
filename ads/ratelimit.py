"""Shared, persistent rate limiting (MongoDB) for every instance of the app.

Why: the previous limiter lived in process memory, so N instances allowed N x the limit and a restart reset every counter.

How it works
------------
* One tiny document per (category, key, window) in the `rate_limits` collection, created and incremented with a SINGLE
  atomic `find_one_and_update(..., upsert=True)`:

      filter: {_id: "<category>|<keyhash>|<window index>", n: {$lt: <allowed in this window>}}
      update: {$inc: {n: 1}, $setOnInsert: {expire_at: ...}}

  If the counter is already at the limit the filter does not match, the upsert tries to insert the same `_id` and MongoDB
  answers DuplicateKeyError == "denied". No read-modify-write, so concurrent requests on any number of instances can never
  exceed the limit. A DuplicateKeyError can also come from two first-inserts racing, so it is retried once.
* Sliding-window-counter: the previous window's count is weighted by how much of it still overlaps "now", which removes the
  classic fixed-window 2x burst at the window boundary. (Roughly: allowed = limit - prev_count * (1 - elapsed_fraction).)
* Denied attempts do NOT consume budget, so a legitimate client that retries politely recovers as soon as the window moves.
* Keys are HMAC-hashed (no raw IPs / user ids at rest). Records expire through a TTL index on `expire_at`.

Failure handling (never silently unlimited)
-------------------------------------------
Every call is bounded by a timeout. On ANY store error / timeout the limiter does NOT allow everything:
  * `fail_mode="deny"`  categories (brute-force counters: failed admin tokens, failed postback secrets, failed webhook secrets)
    are denied. They only run AFTER a credential already failed, so a legitimate caller never sees this.
  * `fail_mode="local"` categories fall back to a strict per-process sliding window with the SAME limit (bounded memory,
    refuses new keys when full). Worst case during an outage is limit x instances, never "no limit".
A circuit breaker skips the store for `cooldown` seconds after a failure (no stacked timeouts) and probes again afterwards, so the
shared limits resume by themselves when the database is back. Degradation is logged (rate-limited) at ERROR level.

Pure module: no import of config.py or motor, so it is unit-testable.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from . import slog
from .store import DuplicateKeyError          # pymongo's class in production, a stand-in where pymongo isn't installed

try:
    from pymongo import ReturnDocument
    _AFTER = ReturnDocument.AFTER
except ImportError:  # tests run without pymongo
    _AFTER = True

COLLECTION = "rate_limits"
DENY, LOCAL = "deny", "local"


class RateLimitConfigError(RuntimeError):
    """Invalid RL_* / TRUSTED_PROXY_COUNT environment. Raised at startup (fail closed), message never contains secrets."""


# ------------------------------------------------------------------ categories
@dataclass(frozen=True)
class Limit:
    limit: int
    window: int            # seconds
    fail_mode: str = LOCAL


def _l(limit, window, fail_mode=LOCAL):
    return Limit(limit, window, fail_mode)


# name -> default. Every one can be overridden with RL_<NAME>="<max>/<seconds>"  e.g. RL_API_DONE_USER=30/60
# Existing Batch-1 numbers are kept; new categories are listed under their purpose.
DEFAULTS: dict[str, Limit] = {
    # --- Mini App API (smart flow), per client IP: coarse flood shield BEFORE any DB work (IPs can be shared: carrier NAT)
    "api_start_ip": _l(90, 60), "api_report_ip": _l(90, 60), "api_done_ip": _l(90, 60),
    # --- Mini App API, per authenticated Telegram user (identity proven by initData HMAC): the tight limit
    "api_start_user": _l(20, 60), "api_report_user": _l(20, 60), "api_done_user": _l(20, 60),
    # --- ad-gate (blocker challenge / grace): generous per IP (many users behind one carrier IP), tight per user
    "gate_challenge_ip": _l(600, 60), "gate_grace_ip": _l(600, 60),
    "gate_challenge_user": _l(20, 60), "gate_grace_user": _l(20, 60),
    # --- bait fetches: a refusal looks like an ad blocker to the page, so keep this very high
    "bait_ip": _l(3000, 60),
    # --- legacy (non-smart) Mini App flow
    "legacy_start_ip": _l(90, 60), "legacy_fail_ip": _l(90, 60), "legacy_done_ip": _l(90, 60), "legacy_grace_ip": _l(90, 60),
    "legacy_start_user": _l(20, 60), "legacy_fail_user": _l(20, 60), "legacy_done_user": _l(20, 60), "legacy_grace_user": _l(20, 60),
    # --- file delivery: public permanent link redirect (per IP) and files actually sent per user (per hour)
    "link_ip": _l(120, 60),
    "file_delivery_user": _l(30, 3600),
    # --- Telegram bot (per user): link opens (/start TOKEN) create sessions + messages; menu/callback/text spam
    "bot_start_user": _l(10, 60), "bot_msg_user": _l(30, 60),
    # --- admin surfaces. *_fail counters run only AFTER a wrong token => fail closed
    "admin_ip": _l(120, 60), "admin_fail_ip": _l(10, 600, DENY),
    "monetag_admin_ip": _l(120, 60), "monetag_admin_fail_ip": _l(10, 600, DENY),
    # --- credentials guessing on machine endpoints (fail closed). A CORRECT postback secret is never limited (Batch 1 rule).
    "postback_fail_ip": _l(20, 600, DENY),
    "webhook_fail_ip": _l(20, 600, DENY),
}

MAX_LIMIT, MAX_WINDOW = 1_000_000, 86_400


def parse_limit(name: str, raw: str, default: Limit) -> Limit:
    """'<max>/<seconds>' (or just '<max>' to keep the default window)."""
    raw = (raw or "").strip()
    if not raw:
        return default
    lim, _, win = raw.partition("/")
    try:
        n = int(lim.strip())
        w = int(win.strip()) if win.strip() else default.window
    except ValueError:
        raise RateLimitConfigError(f"RL_{name.upper()} must look like '<max>/<seconds>' (e.g. 20/60)")
    if not (1 <= n <= MAX_LIMIT) or not (1 <= w <= MAX_WINDOW):
        raise RateLimitConfigError(f"RL_{name.upper()}: max must be 1..{MAX_LIMIT} and seconds 1..{MAX_WINDOW}")
    return Limit(n, w, default.fail_mode)


@dataclass(frozen=True)
class Settings:
    limits: Mapping[str, Limit]
    on_error: str = LOCAL                 # fallback for non-critical categories when the store fails: local | deny
    timeout: float = 0.8                  # seconds per store call
    cooldown: float = 5.0                 # seconds the store is skipped after a failure (circuit breaker)
    trusted_proxies: int = 1              # number of reverse proxies that append to X-Forwarded-For
    max_local_keys: int = 100_000
    salt: str = ""


def load_settings(env: Mapping, salt: str = "") -> Settings:
    limits = {name: parse_limit(name, str(env.get("RL_" + name.upper(), "")), d) for name, d in DEFAULTS.items()}

    def num(name, default, lo, hi, cast=float):
        raw = str(env.get(name, "")).strip()
        if not raw:
            return default
        try:
            v = cast(raw)
        except ValueError:
            raise RateLimitConfigError(f"{name} must be a number")
        if not lo <= v <= hi:
            raise RateLimitConfigError(f"{name} must be between {lo} and {hi}")
        return v

    on_error = str(env.get("RL_ON_STORE_ERROR", LOCAL)).strip().lower() or LOCAL
    if on_error not in (LOCAL, DENY):
        raise RateLimitConfigError("RL_ON_STORE_ERROR must be 'local' or 'deny'")
    if on_error == DENY:                  # operator wants strict: every category fails closed
        limits = {k: Limit(v.limit, v.window, DENY) for k, v in limits.items()}
    return Settings(
        limits=limits, on_error=on_error,
        timeout=num("RL_STORE_TIMEOUT_MS", 800, 50, 10_000) / 1000.0,
        cooldown=num("RL_STORE_COOLDOWN", 5.0, 0.0, 300.0),
        trusted_proxies=int(num("TRUSTED_PROXY_COUNT", 1, 0, 10, int)),
        max_local_keys=int(num("RL_LOCAL_MAX_KEYS", 100_000, 1_000, 5_000_000, int)),
        salt=str(env.get("RATE_LIMIT_SALT") or salt or ""),
    )


# ------------------------------------------------------------------ client IP
def client_ip(xff: str | None, peer: str | None, trusted_proxies: int = 1) -> str:
    """Client address for rate limiting.

    X-Forwarded-For is client-controlled except for the entries our own proxies appended. With `trusted_proxies` = N reverse
    proxies in front of the app, the N-th entry FROM THE RIGHT is the address the outermost trusted proxy saw. Taking the leftmost
    entry (the old behaviour) lets anyone dodge every IP limit by sending a fresh fake `X-Forwarded-For` on each request.
    If the header has fewer entries than expected, or the value is not an IP, it did not come through our proxies => use the socket peer.
    IPv6 is reduced to its /64 (one subscriber owns a whole /64, so per-address limits would be trivial to evade).
    """
    peer = (peer or "").strip() or "unknown"
    cand = peer
    if trusted_proxies > 0 and xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= trusted_proxies:
            cand = parts[-trusted_proxies]
    try:
        ip = ipaddress.ip_address(cand)
    except ValueError:
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


# ------------------------------------------------------------------ local (fallback) window
class _LocalWindow:
    """Strict per-process sliding window used ONLY while the shared store is unavailable. Bounded memory: when full of live keys it
    refuses NEW keys (fail closed) instead of evicting live ones (which would reset their limits)."""

    def __init__(self, max_keys: int, clock):
        self.max_keys, self.clock = max_keys, clock
        self._hits: dict = {}

    def hit(self, key, limit: int, window: float) -> tuple[bool, float]:
        now = self.clock()
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self.max_keys:
                self._prune(now)
                if len(self._hits) >= self.max_keys:
                    return False, window
            q = self._hits[key] = deque()
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return False, max(1.0, window - (now - q[0]))
        q.append(now)
        return True, 0.0

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= 3600]:
            self._hits.pop(k, None)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0           # seconds, only when denied
    degraded: bool = False         # decided without the shared store
    reason: str = "ok"             # ok | limit | cache | store_error_deny | store_error_local

    def __bool__(self) -> bool:
        return self.allowed


# ------------------------------------------------------------------ the limiter
class SharedLimiter:
    def __init__(self, collection: Callable, settings: Settings, clock: Callable[[], float] = time.time):
        """collection: zero-arg callable returning the Motor/PyMongo collection for `rate_limits` (resolved per call, so a
        reconnecting client or a swapped test database is always picked up)."""
        self._coll, self.s, self.clock = collection, settings, clock
        self._local = _LocalWindow(settings.max_local_keys, clock)
        self._denied: dict = {}              # (category, keyhash) -> deny-until (in-process shortcut for floods, never allows anything)
        self._open_until = 0.0               # circuit breaker
        self._last_alert = 0.0
        self.stats = {"store_errors": 0, "degraded_decisions": 0, "denied": 0, "allowed": 0, "last_error": None}
        # Batch 3: `on_state(state, info)` is a SYNC callback ("degraded" | "recovered"), called once per transition.
        self.on_state: Callable | None = None
        self._degraded_since: float | None = None

    # -- public API ----------------------------------------------------
    @property
    def limits(self):
        return self.s.limits

    async def allow(self, category: str, key) -> bool:
        return (await self.hit(category, key)).allowed

    def bind(self, category: str) -> "Bound":
        return Bound(self, category)

    async def hit(self, category: str, key) -> Decision:
        """Count one attempt. Never raises for store problems (programming errors such as an unknown category still do)."""
        lim = self.s.limits[category]
        now = self.clock()
        h = self._hash(category, key)

        until = self._denied.get((category, h), 0.0)
        if until > now:                                    # already denied for the rest of this window: no DB work
            return self._count(Decision(False, max(1, math.ceil(until - now)), False, "cache"))
        if until:
            self._denied.pop((category, h), None)

        if now < self._open_until:                         # store known to be down: don't pile up timeouts
            return self._count(self._degraded(category, key, lim, now))
        try:
            d = await asyncio.wait_for(self._shared(category, h, lim, now), self.s.timeout)
        except Exception as e:  # noqa: BLE001  - ANY failure must degrade safely, never allow-all
            self._store_failed(e, now)
            return self._count(self._degraded(category, key, lim, now))
        if self._degraded_since is not None:               # the shared store answered again
            self._recovered(now)
        if not d.allowed:
            if len(self._denied) > 50_000:
                self._denied = {k: v for k, v in self._denied.items() if v > now}
            if len(self._denied) <= 50_000:
                self._denied[(category, h)] = now + d.retry_after
        return self._count(d)

    # -- internals -----------------------------------------------------
    def _hash(self, category: str, key) -> str:
        return hmac.new(self.s.salt.encode(), f"{category}\x1f{key}".encode(), hashlib.sha256).hexdigest()[:24]

    def _count(self, d: Decision) -> Decision:
        self.stats["allowed" if d.allowed else "denied"] += 1
        if d.degraded:
            self.stats["degraded_decisions"] += 1
        return d

    def _store_failed(self, e: BaseException, now: float) -> None:
        self.stats["store_errors"] += 1
        self.stats["last_error"] = type(e).__name__
        self._open_until = now + self.s.cooldown
        if self._degraded_since is None:
            self._degraded_since = now
            self._emit("degraded", {"error": type(e).__name__})
        if now - self._last_alert >= 30:                   # one alert per 30 s, not one per request
            self._last_alert = now
            slog.event("ratelimit_store_unavailable", level="error", error=type(e).__name__,
                       fallback="deny critical, strict local for the rest", cooldown=self.s.cooldown)

    def _recovered(self, now: float) -> None:
        since, self._degraded_since = self._degraded_since or now, None
        slog.event("ratelimit_store_recovered", downtime_s=round(now - since, 1))
        self._emit("recovered", {"downtime_s": round(now - since, 1)})

    def _emit(self, state: str, info: dict) -> None:
        cb = self.on_state
        if cb is None:
            return
        try:
            cb(state, info)
        except Exception:  # noqa: BLE001 - monitoring must never break request handling
            pass

    @property
    def degraded(self) -> bool:
        return self._degraded_since is not None

    def _degraded(self, category: str, key, lim: Limit, now: float) -> Decision:
        if lim.fail_mode == DENY:
            return Decision(False, max(1, math.ceil(self.s.cooldown) or 1), True, "store_error_deny")
        ok, wait = self._local.hit((category, key), lim.limit, lim.window)
        return Decision(ok, 0 if ok else max(1, math.ceil(wait)), True, "store_error_local")

    async def _shared(self, category: str, h: str, lim: Limit, now: float) -> Decision:
        coll = self._coll()
        w = lim.window
        idx = int(now // w)
        win_start = idx * w
        frac = (now - win_start) / w
        window_end = win_start + w
        cur_id, prev_id = f"{category}|{h}|{idx}", f"{category}|{h}|{idx - 1}"

        prev = await coll.find_one({"_id": prev_id})
        prev_n = int((prev or {}).get("n") or 0)
        carry = int(prev_n * (1.0 - frac))                  # part of the previous window still inside the sliding window
        allowed_here = lim.limit - carry
        if allowed_here <= 0:
            # wait until the carried-over share decays enough that one request fits again
            t_free = win_start + w * (1.0 - lim.limit / prev_n) if prev_n else window_end
            return Decision(False, min(w, max(1, math.ceil(t_free - now))), False, "limit")

        expire = datetime.fromtimestamp(win_start + 2 * w, timezone.utc)
        for attempt in (0, 1):
            try:
                await coll.find_one_and_update(
                    {"_id": cur_id, "n": {"$lt": allowed_here}},
                    {"$inc": {"n": 1}, "$setOnInsert": {"expire_at": expire, "cat": category}},
                    upsert=True, return_document=_AFTER)
                return Decision(True)
            except DuplicateKeyError:
                # At the limit (doc exists, filter failed -> upsert collided on _id) OR two first inserts raced. Retry once to tell apart.
                continue
        return Decision(False, max(1, math.ceil(window_end - now)), False, "limit")


class Bound:
    """A limiter pinned to one category: `await bound.allow(key)` (what ads.postback expects from `fail_limiter`)."""

    def __init__(self, limiter: SharedLimiter, category: str):
        self.limiter, self.category = limiter, category

    async def allow(self, key) -> bool:
        return await self.limiter.allow(self.category, key)


# ------------------------------------------------------------------ construction
_DEFAULT: SharedLimiter | None = None


def build(env: Mapping | None = None, *, collection: Callable | None = None, clock: Callable[[], float] = time.time,
          salt: str = "") -> SharedLimiter:
    import os
    s = load_settings(os.environ if env is None else env, salt)
    if collection is None:
        from . import store
        collection = lambda: store._c(COLLECTION)          # noqa: E731  (lazy: the DB handle may not exist yet / may be swapped)
    return SharedLimiter(collection, s, clock)


def set_default(limiter: SharedLimiter | None) -> None:
    global _DEFAULT
    _DEFAULT = limiter


def default() -> SharedLimiter:
    """Process-wide limiter (production wiring sets it in main.py; handlers use it)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build()
    return _DEFAULT


# ------------------------------------------------------------------ legacy local limiter
class RateLimiter:
    """DEPRECATED, process-local sliding window. No production code path uses it any more (shared limiter above); it stays only
    so older callers/tests that construct `RateLimiter(max, window).allow(key)` keep working."""

    def __init__(self, max_events: int, window: float, clock=time.monotonic):
        self.max, self.window, self.clock = max_events, window, clock
        self._hits: dict = {}

    def allow(self, key) -> bool:
        now = self.clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        if len(self._hits) > 20000:           # crude memory cap
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window][:5000]:
                self._hits.pop(k, None)
        return True
