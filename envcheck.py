"""Startup environment validation + runtime tuning (Batch 3). Pure: no I/O, no import of config.py, so it is unit-testable.

* `validate_required(env)` reports EVERY missing / malformed required variable in one error (never with values).
* `parse_runtime(env)` parses the optional operational knobs. Invalid values stop the process instead of silently falling back.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

REQUIRED = ("MAIN_BOT_TOKEN", "OWNER_ID", "MONGO_URI", "WEBHOOK_SECRET", "BASE_URL", "PRIMARY_CHANNEL", "BACKUP_CHANNEL")
WEBHOOK_SECRET_MIN_LEN = 16


class EnvError(RuntimeError):
    """Invalid or missing environment. The message names variables, never values."""


def _production(env: Mapping) -> bool:
    return (str(env.get("APP_ENV", "production")).strip().lower() or "production") == "production"


def missing_required(env: Mapping) -> list:
    return [n for n in REQUIRED if not str(env.get(n, "")).strip()]


def _is_int(v) -> bool:
    try:
        int(str(v).strip())
        return True
    except ValueError:
        return False


def validate_required(env: Mapping) -> None:
    problems = []
    miss = missing_required(env)
    if miss:
        problems.append("missing: " + ", ".join(miss))
    for n in ("OWNER_ID", "PRIMARY_CHANNEL", "BACKUP_CHANNEL"):
        if n not in miss and not _is_int(env.get(n)):
            problems.append(f"{n} must be an integer")
    if _production(env):
        base = str(env.get("BASE_URL", "")).strip()
        if base and not base.lower().startswith("https://"):
            problems.append("BASE_URL must start with https:// (Telegram only delivers webhooks over HTTPS)")
        link = str(env.get("LINK_BASE_URL", "")).strip()
        if link and not link.lower().startswith("https://"):
            problems.append("LINK_BASE_URL must start with https:// in production")
        ws = str(env.get("WEBHOOK_SECRET", "")).strip()
        if ws and len(ws) < WEBHOOK_SECRET_MIN_LEN:
            problems.append(f"WEBHOOK_SECRET must be at least {WEBHOOK_SECRET_MIN_LEN} characters in production (e.g. `openssl rand -hex 24`)")
    if problems:
        raise EnvError("Invalid environment: " + "; ".join(problems))


@dataclass(frozen=True)
class Runtime:
    log_level: str = "INFO"
    log_format: str = "json"
    log_pseudonymize: bool = True
    log_dedup_seconds: float = 60.0
    alerts_enabled: bool = True
    alert_cooldown: float = 3600.0
    alert_max_per_hour: int = 10
    alert_postback_threshold: int = 20
    alert_postback_window: int = 300
    mongo_selection_timeout_ms: int = 5000
    mongo_connect_timeout_ms: int = 5000
    mongo_socket_timeout_ms: int = 30000
    mongo_max_pool_size: int = 50
    db_health_interval: float = 15.0
    db_fail_threshold: int = 3
    startup_timeout: float = 300.0
    webhook_register_timeout: float = 60.0
    webhook_check_interval: float = 300.0
    shutdown_timeout: float = 15.0
    http_graceful_timeout: int = 10
    readyz_cache_seconds: float = 3.0


def _num(env, name, default, lo, hi, cast=float):
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except ValueError:
        raise EnvError(f"{name} must be a number")
    if not lo <= v <= hi:
        raise EnvError(f"{name} must be between {lo} and {hi}")
    return v


def _bool(env, name, default):
    raw = str(env.get(name, "")).strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise EnvError(f"{name} must be true or false")


def parse_runtime(env: Mapping) -> Runtime:
    level = (str(env.get("LOG_LEVEL", "")).strip().upper() or "INFO")
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise EnvError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    fmt = (str(env.get("LOG_FORMAT", "")).strip().lower() or "json")
    if fmt not in ("json", "text"):
        raise EnvError("LOG_FORMAT must be json or text")
    return Runtime(
        log_level=level, log_format=fmt,
        log_pseudonymize=_bool(env, "LOG_PSEUDONYMIZE", True),
        log_dedup_seconds=_num(env, "LOG_DEDUP_SECONDS", 60.0, 0.0, 3600.0),
        alerts_enabled=_bool(env, "ALERTS_ENABLED", True),
        alert_cooldown=_num(env, "ALERT_COOLDOWN", 3600.0, 60.0, 86400.0),
        alert_max_per_hour=int(_num(env, "ALERT_MAX_PER_HOUR", 10, 1, 100, int)),
        alert_postback_threshold=int(_num(env, "ALERT_POSTBACK_REJECT_THRESHOLD", 20, 1, 10000, int)),
        alert_postback_window=int(_num(env, "ALERT_POSTBACK_REJECT_WINDOW", 300, 30, 3600, int)),
        mongo_selection_timeout_ms=int(_num(env, "MONGO_SERVER_SELECTION_TIMEOUT_MS", 5000, 500, 60000, int)),
        mongo_connect_timeout_ms=int(_num(env, "MONGO_CONNECT_TIMEOUT_MS", 5000, 500, 60000, int)),
        mongo_socket_timeout_ms=int(_num(env, "MONGO_SOCKET_TIMEOUT_MS", 30000, 1000, 300000, int)),
        mongo_max_pool_size=int(_num(env, "MONGO_MAX_POOL_SIZE", 50, 1, 500, int)),
        db_health_interval=_num(env, "DB_HEALTH_INTERVAL", 15.0, 2.0, 300.0),
        db_fail_threshold=int(_num(env, "DB_FAIL_THRESHOLD", 3, 1, 20, int)),
        startup_timeout=_num(env, "STARTUP_TIMEOUT", 300.0, 30.0, 1800.0),
        webhook_register_timeout=_num(env, "WEBHOOK_REGISTER_TIMEOUT", 60.0, 5.0, 600.0),
        webhook_check_interval=_num(env, "WEBHOOK_CHECK_INTERVAL", 300.0, 30.0, 3600.0),
        shutdown_timeout=_num(env, "SHUTDOWN_TIMEOUT", 15.0, 2.0, 120.0),
        http_graceful_timeout=int(_num(env, "HTTP_GRACEFUL_TIMEOUT", 10, 1, 120, int)),
        readyz_cache_seconds=_num(env, "READYZ_CACHE_SECONDS", 3.0, 0.0, 30.0),
    )


def parse_port(env: Mapping, default: int = 8000) -> int:
    """Koyeb injects PORT for the port configured on the service."""
    raw = str(env.get("PORT", "")).strip()
    if not raw:
        return default
    try:
        p = int(raw)
    except ValueError:
        raise EnvError("PORT must be an integer")
    if not 1 <= p <= 65535:
        raise EnvError("PORT must be between 1 and 65535")
    return p
