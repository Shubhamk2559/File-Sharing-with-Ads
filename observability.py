"""Operational monitoring primitives (Batch 3): structured logs, secret redaction, pseudonymisation, log de-duplication, counters and
owner alerts. Standard library only (no config.py / telegram / motor), so every piece is unit-testable.

* Every log line is ONE JSON object: {"ts": ISO-8601 UTC, "severity": ..., "logger": ..., "msg"|"event": ...}.
* Secrets never reach a log line: exact values of every configured secret are registered with the Redactor and pattern rules catch the rest
  (bot tokens in API URLs, secret=/token=/hash= query strings, Authorization: Bearer, credentials inside connection URIs). Tracebacks too.
* User/chat ids and client IPs become short keyed hashes (`h:3f9a...`).
* Repeated WARNING+ records are collapsed per (logger, event, reason) inside a window; the next line carries `suppressed: N`.
* Owner alerts are de-duplicated per key (cooldown), capped per hour, queued until the bot exists, redacted and user-free.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import quote, unquote

MASK = "***"
_log = logging.getLogger("observability")

# ====================================================================== redaction
_P_URI_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^@\s/]+)@")
_P_BOT_TOKEN = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_\-]{30,}")
_P_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}")
_P_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9])(secret|token|password|passwd|pwd|api[_-]?key|apikey|access[_-]?key|init[_-]?data|hash|signature|"
    r"authorization|x-telegram-bot-api-secret-token)(\"?\s*[=:]\s*\"?)(?!\*\*\*)([^\s&\"',;}\]]+)")
SENSITIVE_KEYS = frozenset({"secret", "token", "password", "passwd", "api_key", "apikey", "init_data", "initdata", "authorization",
                            "hash", "signature", "postback_secret", "bot_token"})


class Redactor:
    MIN_SECRET_LEN = 6

    def __init__(self):
        self._secrets: list = []

    def add(self, *values) -> None:
        for v in values:
            v = str(v or "").strip()
            if len(v) < self.MIN_SECRET_LEN:
                continue
            for variant in {v, quote(v, safe=""), unquote(v)}:
                if len(variant) >= self.MIN_SECRET_LEN and variant not in self._secrets:
                    self._secrets.append(variant)
        self._secrets.sort(key=len, reverse=True)

    def add_uri(self, uri: str) -> None:
        self.add(uri)
        m = _P_URI_CREDS.search(uri or "")
        if m:
            self.add(m.group(3))

    def redact(self, text) -> str:
        s = text if isinstance(text, str) else str(text)
        for sec in self._secrets:
            if sec in s:
                s = s.replace(sec, MASK)
        s = _P_URI_CREDS.sub(r"\1***:***@", s)
        s = _P_BOT_TOKEN.sub(MASK, s)
        s = _P_BEARER.sub(r"\1 ***", s)
        return _P_KV.sub(r"\1\2***", s)

    @property
    def secret_count(self) -> int:
        return len(self._secrets)


# ====================================================================== pseudonymisation
IP_FIELDS = frozenset({"ip", "client_ip", "remote_ip", "peer"})
PII_FIELDS = frozenset({"user_id", "telegram_id", "chat_id", "uid"}) | IP_FIELDS


class Pseudonymizer:
    def __init__(self, key: bytes | None = None, enabled: bool = True):
        self.enabled = enabled
        self._key = key or os.urandom(32)

    def tag(self, kind: str, value) -> str:
        if not self.enabled:
            return str(value)
        return "h:" + hmac.new(self._key, f"{kind}\x1f{value}".encode(), hashlib.sha256).hexdigest()[:10]


REDACTOR = Redactor()
PSEUDO = Pseudonymizer()


def redact(text) -> str:
    return REDACTOR.redact(text)


def _clean(value, depth: int = 0):
    if isinstance(value, str):
        return REDACTOR.redact(value)[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return REDACTOR.redact(str(value))[:200]
    if isinstance(value, dict):
        return {str(k): (MASK if str(k).lower() in SENSITIVE_KEYS else _clean(v, depth + 1)) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v, depth + 1) for v in list(value)[:50]]
    return REDACTOR.redact(str(value))[:200]


def sanitize_fields(fields: dict) -> dict:
    """Sensitive keys masked, PII pseudonymised, strings redacted."""
    out = {}
    for k, v in fields.items():
        lk = str(k).lower()
        if v is None:
            continue
        if lk in SENSITIVE_KEYS:
            out[k] = MASK
        elif lk in PII_FIELDS:
            out[k] = PSEUDO.tag("ip" if lk in IP_FIELDS else "tg", v)
        else:
            out[k] = _clean(v)
    return out


# ====================================================================== formatters / filters
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    MAX_EXC = 4000

    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": _iso(record.created), "severity": record.levelname, "logger": record.name}
        ev = getattr(record, "event_fields", None)
        if ev:
            d["event"] = ev.get("ev")
            d.update({k: v for k, v in ev.items() if k != "ev"})
        else:
            d["msg"] = REDACTOR.redact(record.getMessage())[:2000]
        if record.exc_info and record.exc_info[0] is not None:
            d["exc_type"] = record.exc_info[0].__name__
            d["exc"] = REDACTOR.redact("".join(traceback.format_exception(*record.exc_info)))[-self.MAX_EXC:]
        sup = getattr(record, "suppressed", 0)
        if sup:
            d["suppressed"] = sup
        return json.dumps(d, default=str, ensure_ascii=False, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    MAX_EXC = 4000

    def format(self, record: logging.LogRecord) -> str:
        ev = getattr(record, "event_fields", None)
        msg = json.dumps(ev, default=str, separators=(",", ":")) if ev else REDACTOR.redact(record.getMessage())
        line = f"{_iso(record.created)} {record.levelname:<8} {record.name}: {msg}"
        if record.exc_info and record.exc_info[0] is not None:
            line += "\n" + REDACTOR.redact("".join(traceback.format_exception(*record.exc_info)))[-self.MAX_EXC:]
        sup = getattr(record, "suppressed", 0)
        return line + (f" (+{sup} similar suppressed)" if sup else "")


class DedupFilter(logging.Filter):
    """Collapses identical WARNING+ records inside `window` seconds. INFO/DEBUG are never touched."""

    def __init__(self, window: float = 60.0, clock: Callable[[], float] = time.monotonic, max_keys: int = 2000):
        super().__init__()
        self.window, self.clock, self.max_keys = float(window), clock, max_keys
        self._seen: dict = {}

    @staticmethod
    def _ident(record: logging.LogRecord):
        ev = getattr(record, "event_fields", None)
        if ev:
            return ("ev", ev.get("ev"), str(ev.get("reason")), str(ev.get("error")), str(ev.get("network")), str(ev.get("path")))
        exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
        return ("msg", str(record.msg)[:200], exc)

    def filter(self, record: logging.LogRecord) -> bool:
        if self.window <= 0 or record.levelno < logging.WARNING:
            return True
        now = self.clock()
        key = (record.name, record.levelno, self._ident(record))
        hit = self._seen.get(key)
        if hit and now - hit[0] < self.window:
            hit[1] += 1
            METRICS.inc("log.suppressed")
            return False
        if len(self._seen) >= self.max_keys:
            self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self.window}
            if len(self._seen) >= self.max_keys:
                self._seen.clear()
        if hit and hit[1]:
            record.suppressed = hit[1]
        self._seen[key] = [now, 0]
        return True


class CountFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        METRICS.inc("log." + record.levelname.lower())
        return True


HEALTH_PATHS = ("/", "/readyz")


class AccessLogFilter(logging.Filter):
    """uvicorn.access: successful health probes are not logged; client address pseudonymised; secrets in the query string masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        if isinstance(a, tuple) and len(a) >= 5:
            client, method, path, ver, status = a[0], a[1], str(a[2]), a[3], a[4]
            try:
                ok = int(status) < 400
            except (TypeError, ValueError):
                ok = False
            if ok and path.split("?", 1)[0] in HEALTH_PATHS:
                return False
            c = str(client).rsplit(":", 1)[0] if ":" in str(client) else str(client)
            record.args = (PSEUDO.tag("ip", c) if c else "-", method, REDACTOR.redact(path), ver, status)
        elif isinstance(a, tuple):
            record.args = tuple(REDACTOR.redact(x) if isinstance(x, str) else x for x in a)
        return True


def setup_logging(level: str = "INFO", fmt: str = "json", dedup_seconds: float = 60.0, stream=None) -> logging.Handler:
    """Idempotent. Everything flows through ONE handler so redaction/dedup cannot be bypassed by a logger with its own handler."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_obs_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler._obs_handler = True                       # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(CountFilter())
    handler.addFilter(DedupFilter(dedup_seconds))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "telegram", "httpx", "httpcore", "asyncio", "pymongo"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
    acc = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in acc.filters):
        acc.addFilter(AccessLogFilter())
    for name in ("httpx", "httpcore", "hpack"):       # bot tokens live in Telegram API URLs
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def configure(*, secrets=(), uris=(), pseudonym_key: str = "", pseudonymize: bool = True) -> None:
    global PSEUDO
    REDACTOR.add(*secrets)
    for u in uris:
        REDACTOR.add_uri(u)
    key = hashlib.sha256(("log-pseudonym:" + (pseudonym_key or "")).encode()).digest() if pseudonym_key else None
    PSEUDO = Pseudonymizer(key, pseudonymize)


# ====================================================================== metrics
class Metrics:
    MAX_NAMES = 500

    def __init__(self):
        self._c: dict = defaultdict(int)
        self._g: dict = {}
        self.started_at = time.time()

    def inc(self, name: str, n: int = 1) -> None:
        if name not in self._c and len(self._c) >= self.MAX_NAMES:
            name = "overflow"
        self._c[name] += n

    def gauge(self, name: str, value) -> None:
        if name in self._g or len(self._g) < self.MAX_NAMES:
            self._g[name] = value

    def get(self, name: str) -> int:
        return self._c.get(name, 0)

    def snapshot(self) -> dict:
        return {"counters": dict(sorted(self._c.items())), "gauges": dict(sorted(self._g.items()))}

    def reset(self) -> None:
        self._c.clear()
        self._g.clear()


METRICS = Metrics()


class RateWatch:
    """Sliding-window event counter ("N rejections in 5 minutes")."""

    def __init__(self, threshold: int, window: float, clock: Callable[[], float] = time.time):
        self.threshold, self.window, self.clock = threshold, window, clock
        self._ev: deque = deque()

    def hit(self, label: str = "") -> int:
        now = self.clock()
        self._ev.append((now, label))
        while self._ev and now - self._ev[0][0] > self.window:
            self._ev.popleft()
        while len(self._ev) > 10_000:
            self._ev.popleft()
        return len(self._ev)

    def breakdown(self) -> dict:
        out: dict = defaultdict(int)
        for _t, lab in self._ev:
            out[lab or "other"] += 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def crossed(self) -> bool:
        return len(self._ev) >= self.threshold


# ====================================================================== dependency state
class Dependency:
    """`failure()` returns "down" ONCE when the consecutive-failure threshold is crossed; `success()` returns "recovered" ONCE."""

    def __init__(self, name: str, fail_threshold: int = 3, clock: Callable[[], float] = time.time):
        self.name, self.fail_threshold, self.clock = name, max(1, fail_threshold), clock
        self.status = "unknown"
        self.consecutive_failures = 0
        self.total_failures = 0
        self.last_error: str | None = None
        self.since: float | None = None
        self.last_ok: float | None = None
        self.last_failure: float | None = None
        self.last_downtime: float | None = None

    def success(self) -> str | None:
        now = self.clock()
        ev = None
        if self.status == "down":
            self.last_downtime = now - (self.since if self.since is not None else now)
            ev = "recovered"
        if self.status != "ok":
            self.since = now
        self.status, self.consecutive_failures, self.last_ok = "ok", 0, now
        return ev

    def failure(self, err: BaseException | str = "") -> str | None:
        now = self.clock()
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure = now
        self.last_error = err if isinstance(err, str) else type(err).__name__
        if self.status != "down" and self.consecutive_failures >= self.fail_threshold:
            self.status, self.since = "down", now
            return "down"
        return None

    def snapshot(self) -> dict:
        return {"status": self.status, "consecutive_failures": self.consecutive_failures, "total_failures": self.total_failures,
                "last_error": self.last_error, "since": _iso(self.since) if self.since else None,
                "last_ok": _iso(self.last_ok) if self.last_ok else None,
                "last_downtime_s": round(self.last_downtime, 1) if self.last_downtime is not None else None}


# ====================================================================== owner alerts
Sender = Callable[[str], Awaitable[None]]
_ICON = {"critical": "🔴 CRITICAL", "warning": "⚠️ WARNING", "recovered": "✅ RECOVERED", "info": "ℹ️ INFO"}


class _KeyState:
    __slots__ = ("next_ok", "suppressed", "active")

    def __init__(self):
        self.next_ok, self.suppressed, self.active = 0.0, 0, False


class AlertManager:
    """Owner notifications with duplicate prevention: per-key cooldown (suppressed repeats counted), global hourly cap, queue until a sender
    exists, a failing sender never raises into the caller, texts redacted and truncated."""

    def __init__(self, *, enabled: bool = True, cooldown: float = 3600.0, max_per_hour: int = 10, clock: Callable[[], float] = time.time,
                 send_timeout: float = 10.0, queue_max: int = 20, max_len: int = 3500):
        self.enabled, self.cooldown, self.max_per_hour, self.clock = enabled, cooldown, max_per_hour, clock
        self.send_timeout, self.queue_max, self.max_len = send_timeout, queue_max, max_len
        self._keys: dict = {}
        self._sent: deque = deque()
        self._queue: deque = deque()
        self._sender: Sender | None = None
        self._cap_notified = False
        self.stats = {"sent": 0, "suppressed": 0, "capped": 0, "queued": 0, "failed": 0}

    def configure(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def set_sender(self, sender: Sender | None) -> None:
        self._sender = sender

    def _compose(self, severity: str, text: str, suppressed: int = 0) -> str:
        msg = f"{_ICON.get(severity, severity.upper())}: {REDACTOR.redact(text)}"
        if suppressed:
            msg += f"\n(+{suppressed} similar alert{'s' if suppressed != 1 else ''} suppressed)"
        return msg[: self.max_len]

    def _prune(self, now: float) -> None:
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        if len(self._sent) < self.max_per_hour:
            self._cap_notified = False

    async def _deliver(self, msg: str) -> bool:
        if self._sender is None:
            return False
        await asyncio.wait_for(self._sender(msg), self.send_timeout)
        return True

    async def alert(self, key: str, text: str, *, severity: str = "critical", cooldown: float | None = None) -> bool:
        """True = a message was handed to Telegram now."""
        if not self.enabled:
            return False
        now = self.clock()
        st = self._keys.setdefault(key, _KeyState())
        if now < st.next_ok:
            st.suppressed += 1
            self.stats["suppressed"] += 1
            METRICS.inc("alerts.suppressed")
            return False
        self._prune(now)
        if len(self._sent) >= self.max_per_hour:
            st.suppressed += 1
            self.stats["capped"] += 1
            METRICS.inc("alerts.capped")
            if not self._cap_notified:
                self._cap_notified = True
                _log.error("alert limit of %d per hour reached; further owner alerts are suppressed", self.max_per_hour)
                try:
                    await self._deliver(self._compose("warning", f"Alert limit reached ({self.max_per_hour}/hour). Further alerts are "
                                                                 "suppressed for now: check the logs / Koyeb dashboard."))
                except Exception:  # noqa: BLE001
                    pass
            return False
        suppressed = st.suppressed
        cd = self.cooldown if cooldown is None else cooldown
        st.suppressed, st.next_ok, st.active = 0, now + cd, True        # claimed BEFORE awaiting: concurrent callers see the cooldown
        self._sent.append(now)
        msg = self._compose(severity, text, suppressed)
        if self._sender is None:
            if len(self._queue) < self.queue_max:
                self._queue.append((key, msg))
                self.stats["queued"] += 1
            _log.warning("owner alert queued (no sender yet): %s", key)
            return False
        try:
            await self._deliver(msg)
        except Exception as e:  # noqa: BLE001 - alerting must never break the caller
            self.stats["failed"] += 1
            st.next_ok = now + min(cd, 60.0)
            st.suppressed += suppressed
            if self._sent:
                self._sent.pop()
            _log.warning("owner alert could not be delivered (%s): %s", type(e).__name__, key)
            return False
        self.stats["sent"] += 1
        METRICS.inc("alerts.sent")
        return True

    async def recover(self, key: str, text: str) -> bool:
        """"Recovered" once, and only if this key actually alerted before."""
        st = self._keys.get(key)
        if not self.enabled or st is None or not st.active:
            return False
        st.active = False
        st.next_ok = self.clock() + 300.0            # flap guard
        self._prune(self.clock())
        if len(self._sent) >= self.max_per_hour or self._sender is None:
            return False
        self._sent.append(self.clock())
        try:
            await self._deliver(self._compose("recovered", text))
            self.stats["sent"] += 1
            return True
        except Exception:  # noqa: BLE001
            self.stats["failed"] += 1
            return False

    async def flush(self) -> int:
        n = 0
        while self._queue and self._sender is not None:
            _key, msg = self._queue.popleft()
            try:
                await self._deliver(msg)
                self.stats["sent"] += 1
                n += 1
            except Exception:  # noqa: BLE001
                self.stats["failed"] += 1
                break
        return n

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "sender_ready": self._sender is not None, "queued": len(self._queue), **self.stats,
                "sent_last_hour": len(self._sent)}


ALERTS = AlertManager()
POSTBACK_WATCH = RateWatch(20, 300)
HTTP_ERROR_WATCH = RateWatch(30, 300)
BOT_ERROR_WATCH = RateWatch(30, 300)

_KNOWN_REASONS = frozenset({"bad_secret", "no_secret_configured", "malformed", "unknown_ad", "user_mismatch", "zone_mismatch",
                            "session_invalid", "expired", "closed", "rate_limited"})


def configure_alerts(*, enabled: bool, cooldown: float, max_per_hour: int, postback_threshold: int, postback_window: int) -> None:
    global POSTBACK_WATCH
    ALERTS.configure(enabled=enabled, cooldown=cooldown, max_per_hour=max_per_hour)
    POSTBACK_WATCH = RateWatch(postback_threshold, postback_window)


async def note_postback_rejected(reason: str) -> None:
    """Counts every rejected Monetag postback and raises ONE alert when the rate crosses the threshold."""
    reason = reason if reason in _KNOWN_REASONS else "other"
    METRICS.inc("postback.rejected")
    METRICS.inc("postback.rejected." + reason)
    POSTBACK_WATCH.hit(reason)
    if POSTBACK_WATCH.crossed():
        br = ", ".join(f"{k}={v}" for k, v in list(POSTBACK_WATCH.breakdown().items())[:6])
        await ALERTS.alert("postback_rejections",
                           f"{len(POSTBACK_WATCH._ev)} Monetag postbacks rejected in the last {int(POSTBACK_WATCH.window)}s ({br}). "
                           "Likely a wrong secret/URL in the Monetag zone settings (users would not receive files), or probing. "
                           "See KOYEB.md 'Monetag postback'.", severity="warning")


SECRET_ENV_NAMES = ("MAIN_BOT_TOKEN", "WEBHOOK_SECRET", "MONETAG_POSTBACK_SECRET", "ADS_ADMIN_TOKEN", "MONETAG_ADMIN_TOKEN", "RATE_LIMIT_SALT")


def bootstrap(env, rt) -> None:
    """Registers every configured secret with the redactor, derives the pseudonymisation key, configures alerting, installs the log handler."""
    secrets = [env.get(n, "") for n in SECRET_ENV_NAMES]
    ws = str(env.get("WEBHOOK_SECRET", "")).strip()
    if ws:
        secrets.append(hashlib.sha256(f"{ws}:main".encode()).hexdigest())          # the Telegram webhook header secret (manager.secret)
    configure(secrets=secrets, uris=[env.get("MONGO_URI", "")], pseudonym_key=ws, pseudonymize=rt.log_pseudonymize)
    configure_alerts(enabled=rt.alerts_enabled, cooldown=rt.alert_cooldown, max_per_hour=rt.alert_max_per_hour,
                     postback_threshold=rt.alert_postback_threshold, postback_window=rt.alert_postback_window)
    setup_logging(rt.log_level, rt.log_format, rt.log_dedup_seconds)


def diagnostics() -> dict:
    """Secret-free, user-free snapshot for /admin/health."""
    return {"metrics": METRICS.snapshot(), "alerts": ALERTS.snapshot(),
            "redaction": {"secrets_registered": REDACTOR.secret_count, "pseudonymized_ids": PSEUDO.enabled}}
