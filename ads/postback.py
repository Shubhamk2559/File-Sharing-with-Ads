"""Monetag server-to-server postback endpoint logic (framework independent; main.py is a thin wrapper).

Order of checks (cheapest / least revealing first):
  1. a secret must be configured at all (empty secret never matches anything)         -> 503
  2. secret compared in constant time (SHA-256 digests, so length and non-ASCII are safe) -> 403 (+ per-IP failure limit -> 429;
     the limiter is shared across instances and fails CLOSED if its store is down: it only ever runs after a wrong secret)
  3. payload shape                                                                      -> 400
  4. store.record_postback(): the ymid must be an ad WE issued, bound to the same user, zone and a live session,
     not expired and not already closed; exact repeats are idempotent                   -> 200 ok | 200 dup | 403 | 410

The response body never says WHY a postback was rejected (that goes to the server log with no secret/ymid), so the endpoint
can't be used as an oracle. A correct secret is never rate limited, so an attacker can't lock Monetag out by failing from its IP.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import time
from typing import Mapping

import observability
from . import slog

MAX_PARAM_LEN = 200
OK, DUP = "ok", "dup"
# store.record_postback() reason -> HTTP status
STATUS = {OK: 200, DUP: 200, "malformed": 400, "unknown_ad": 403, "user_mismatch": 403, "zone_mismatch": 403,
          "session_invalid": 410, "expired": 410, "closed": 410}


def secret_ok(got, want: str) -> bool:
    if not want or not isinstance(got, str) or not got:
        return False
    return hmac.compare_digest(hashlib.sha256(got.encode("utf-8", "replace")).digest(),
                               hashlib.sha256(want.encode("utf-8")).digest())


async def handle(query: Mapping, *, secret: str, store, ip: str = "?", fail_limiter=None, max_age: int = 600,
                 session_ttl: int = 1800, require_telegram_id: bool = True, clock=time.time) -> tuple[int, str]:
    """-> (http_status, body). `store` needs `async record_postback(q, *, now, max_age, session_ttl, require_telegram_id)`."""
    if not secret:
        slog.event("postback_rejected", level="error", reason="no_secret_configured")
        await observability.note_postback_rejected("no_secret_configured")
        return 503, "unavailable"
    if not secret_ok(query.get("secret"), secret):
        if fail_limiter is not None:
            ok = fail_limiter.allow(ip)
            if inspect.isawaitable(ok):                    # shared (MongoDB) limiter is async; the legacy local one is sync
                ok = await ok
            if not ok:
                await observability.note_postback_rejected("rate_limited")
                return 429, "rate limited"
        slog.event("postback_rejected", level="warning", reason="bad_secret", ip=ip)
        await observability.note_postback_rejected("bad_secret")
        return 403, "forbidden"
    q = {k: v for k, v in query.items() if k != "secret"}       # the secret goes no further than this function
    if any(not isinstance(v, str) or len(v) > MAX_PARAM_LEN for v in q.values()):
        slog.event("postback_rejected", level="warning", reason="malformed", ip=ip)
        await observability.note_postback_rejected("malformed")
        return 400, "rejected"
    result = await store.record_postback(q, now=clock(), max_age=max_age, session_ttl=session_ttl,
                                         require_telegram_id=require_telegram_id)
    status = STATUS.get(result, 400)
    if result in (OK, DUP):
        observability.METRICS.gauge("postback.last_ok_at", int(clock()))
    if result == OK:
        observability.METRICS.inc("postback.accepted")
        slog.event("postback_accepted")                        # no ymid / user / zone: the counter is the evidence, not a user trail
    elif result == DUP:
        observability.METRICS.inc("postback.duplicate")
        slog.event("postback_duplicate", level="debug")
    else:
        slog.event("postback_rejected", level="warning", reason=result, ip=ip)
        await observability.note_postback_rejected(result)
    return status, result if result in (OK, DUP) else "rejected"
