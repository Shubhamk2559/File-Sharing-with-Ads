"""Security profile: production vs development, resolved ONCE at startup and failing closed.

Pure (no I/O, no import of config.py) so it is unit-testable and shared by config.py, ads/settings.py and main.py.

  APP_ENV=production (DEFAULT, also when unset)
      * Monetag reward verification MUST be the server-to-server postback (AD_VERIFY_MODE=postback; "legacy" is refused).
      * MONETAG_POSTBACK_SECRET is REQUIRED (>= 16 URL-safe chars). Missing/weak => the process refuses to start.
      * strict mode: only server-verified rewards can unlock a file. Browser-only ("client_attested") rewards, which is
        all Tads.me offers, never do, whatever the stored admin config says.
  APP_ENV=development | test
      * Legacy verification and client-attested rewards are allowed so the flow can be tried without provider accounts.
        Never use these values in production.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

ENVS = ("production", "development", "test")
MODES = ("legacy", "postback")
SECRET_MIN_LEN = 16
# The secret is pasted unescaped into Monetag's postback URL, so only RFC 3986 "unreserved" characters are allowed.
SECRET_RE = re.compile(r"^[A-Za-z0-9._~-]{%d,128}$" % SECRET_MIN_LEN)


class ConfigError(RuntimeError):
    """Raised at startup. The message never contains the secret value."""


@dataclass(frozen=True)
class Security:
    app_env: str = "production"
    verify_mode: str = "postback"
    postback_secret: str = field(default="", repr=False)          # repr=False: never printed by accident
    postback_max_age: int = 600                                    # a postback is only accepted this long after the ad was issued
    require_telegram_id: bool = True                               # postback must carry telegram_id equal to the session user
    warnings: tuple = ()

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def strict(self) -> bool:
        """Strict = browser-only rewards can never unlock a file. Always on in production."""
        return self.production


def _bool(env: Mapping, name: str, default: bool) -> bool:
    return str(env.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping, name: str, default: int, lo: int, hi: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer")
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return v


def resolve(env: Mapping) -> Security:
    """Validate the environment. Raises ConfigError instead of starting in a weaker mode than the operator asked for."""
    app_env = str(env.get("APP_ENV", "production")).strip().lower() or "production"
    if app_env not in ENVS:
        raise ConfigError("APP_ENV must be one of: " + ", ".join(ENVS))
    production = app_env == "production"

    mode = str(env.get("AD_VERIFY_MODE", "postback" if production else "legacy")).strip().lower()
    if mode not in MODES:
        raise ConfigError("AD_VERIFY_MODE must be 'legacy' or 'postback'")
    if production and mode != "postback":
        raise ConfigError("APP_ENV=production requires AD_VERIFY_MODE=postback (browser-only verification is not accepted in production)")

    secret = str(env.get("MONETAG_POSTBACK_SECRET", "")).strip()
    if (production or mode == "postback") and not secret:
        raise ConfigError("MONETAG_POSTBACK_SECRET is required (server-side Monetag verification cannot run without it)")
    if secret and (production or mode == "postback") and not SECRET_RE.match(secret):
        raise ConfigError(f"MONETAG_POSTBACK_SECRET must be {SECRET_MIN_LEN}-128 characters from A-Z a-z 0-9 . _ ~ -")

    sec = Security(
        app_env=app_env, verify_mode=mode, postback_secret=secret,
        postback_max_age=_int(env, "AD_ATTEMPT_MAX_AGE", 600, 30, 3600),
        require_telegram_id=_bool(env, "MONETAG_POSTBACK_REQUIRE_TELEGRAM_ID", True),
    )
    warns = []
    if not production:
        warns.append(f"APP_ENV={app_env}: development profile. Browser-only (client-attested) rewards may unlock files. Do NOT use in production.")
    if production and _bool(env, "TADS_ALLOW_CLIENT_REWARD", True):
        warns.append("TADS_ALLOW_CLIENT_REWARD=true: Tads.me rewards are browser-attested (no server-side proof) but are accepted "
                     "because you enabled it. Set TADS_ALLOW_CLIENT_REWARD=false for Monetag-only unlocking.")
    if not sec.require_telegram_id:
        warns.append("MONETAG_POSTBACK_REQUIRE_TELEGRAM_ID=false: postbacks without telegram_id are accepted (session binding still applies).")
    return Security(**{**sec.__dict__, "warnings": tuple(warns)})


# ------------------------------------------------------------------ session lifetime
def _ts(created) -> float | None:
    if isinstance(created, datetime):
        if created.tzinfo is None:                 # pymongo returns naive UTC datetimes by default
            created = created.replace(tzinfo=timezone.utc)
        return created.timestamp()
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return float(created)
    return None


def session_expired(session: dict | None, ttl: int, now: float | None = None) -> bool:
    """True when the session is missing, has no creation time, or is older than ttl. Fails closed: Mongo's TTL index only
    reaps documents about once a minute, so the age is also checked explicitly."""
    if not session:
        return True
    created = _ts(session.get("created"))
    if created is None:
        return True
    return (time.time() if now is None else now) - created > ttl
