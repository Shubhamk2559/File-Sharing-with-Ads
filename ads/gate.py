"""Ad gate: (1) server-verified ad-blocker challenge, (2) "no ad could be shown" grace.

Policy (what the owner asked for)
---------------------------------
  * A user gets the file only after watching an ad (Monetag first, Tads.me as fallback, up to GATE_ROUNDS rounds).
  * If after every round NO ad was ever displayed, the user is checked for an ad blocker:
        blocker found / can't prove clean  -> NO file
        provably clean                     -> file is delivered ("grace")

Why the check is server-side
----------------------------
Client-side "am I blocked?" code can be switched off by whoever it is meant to catch, so the browser is never the
judge. The server issues a one-time challenge; the browser must fetch several *bait* resources that sit on our own origin
under ad-looking URLs (what EasyList/uBlock/AdGuard/Brave rules block). The bait handler records the hit **on the server**
and the script bait returns an HMAC the page must hand back. A blocked bait never reaches the server and never yields the
HMAC, so the grace request can't be satisfied. The challenge is single-use, bound to (session, user), short-lived and must
be issued AFTER the last failed ad attempt (so a pre-solved challenge can't be replayed later).

Grace is only given when the SERVER's own records (ads/service.py) show GATE_ROUNDS complete rounds in which every attempt
was a genuine no-ad failure (reason in GATE_GRACE_REASONS - never "blocked"/"error", never an ad that was shown).

Limits (be honest): a determined person who writes their own client can fetch the bait URLs by hand. That needs custom
code outside Telegram; the other defences (Telegram initData signature, one slot per session, timing, hourly cap,
Monetag postback verification) stay in force. Reward integrity for *watched* ads is unchanged (see ads/verify.py).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from . import settings, slog, store

# (id, kind, path). Paths deliberately look like ad assets so generic filter-list rules match them.
BAITS = (
    ("s1", "script", "/js/adframe.js"),
    ("s2", "script", "/pagead/js/adsbygoogle.js"),
    ("s3", "script", "/ads/banner/ad-banner.js"),
    ("i1", "img", "/banners/ads/ad-banner-300x250.gif"),
)
BAIT_BY_PATH = {p: (k, kind) for k, kind, p in BAITS}
# 1x1 transparent GIF
GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")

_SECRET = b""
_clock = time.time


def configure(secret: str, clock=time.time) -> None:
    global _SECRET, _clock
    _SECRET = (secret or "").encode()
    _clock = clock


def _proof(nonce: str, key: str) -> str:
    return hmac.new(_SECRET or b"unset", f"gate|{nonce}|{key}".encode(), hashlib.sha256).hexdigest()[:24]


# ------------------------------------------------------------------ challenge
async def issue(s: dict) -> dict:
    nonce = secrets.token_hex(12)
    now = _clock()
    await store.create_challenge({"_id": nonce, "sid": s["sid"], "user_id": s["user_id"], "iat": now, "used": None})
    return {"ok": True, "cid": nonce, "ttl": settings.GATE_CHALLENGE_TTL,
            "baits": [{"k": k, "t": kind, "u": f"{path}?c={nonce}&k={k}"} for k, kind, path in BAITS]}


async def bait_response(path: str, nonce: str, key: str) -> tuple[str, bytes | str]:
    """-> (kind, body). Always answers the same shape; only a live challenge gets a real proof and a recorded hit."""
    entry = BAIT_BY_PATH.get(path)
    kind = entry[1] if entry else "script"
    ch = await store.get_challenge(nonce) if (entry and nonce and len(nonce) <= 64) else None
    valid = bool(ch) and entry[0] == key and ch.get("used") is None
    if valid:
        await store.record_bait_hit(nonce, key)
    if kind == "img":
        return "img", GIF
    return "script", (f'(window.__gv=window.__gv||{{}})["{key}"]="{_proof(nonce, key)}";' if valid else "/*0*/")


async def verify_challenge(s: dict, cid, proofs, min_issue: float) -> tuple[bool, str]:
    """Pure check, does not consume. -> (ok, reason)."""
    if not isinstance(cid, str) or not cid or len(cid) > 64:
        return False, "no_challenge"
    ch = await store.get_challenge(cid)
    if not ch or ch.get("sid") != s["sid"] or ch.get("user_id") != s["user_id"]:
        return False, "unknown_challenge"
    if ch.get("used") is not None:
        return False, "replayed"
    now = _clock()
    if now - float(ch.get("iat", 0)) > settings.GATE_CHALLENGE_TTL:
        return False, "expired"
    if float(ch.get("iat", 0)) < min_issue:
        return False, "stale_challenge"
    proofs = proofs if isinstance(proofs, dict) else {}
    for k, kind, _ in BAITS:
        if not ch.get("hit_" + k):
            return False, f"bait_not_fetched:{k}"
        if kind == "script" and not hmac.compare_digest(str(proofs.get(k, "")), _proof(cid, k)):
            return False, f"bad_proof:{k}"
        if kind == "img" and proofs.get(k) is not True:
            return False, f"bait_not_rendered:{k}"
    return True, "ok"


# ------------------------------------------------------------------ rounds (from the server's own attempt records)
def _round_clean(attempts: list, allowed) -> bool:
    if not attempts or not any(a.get("round_exhausted") for a in attempts):
        return False
    for a in attempts:
        if a.get("outcome") != "failed" or a.get("shown_at") is not None or a.get("reason") not in allowed:
            return False
    return True


def clean_rounds(attempts: list, allowed=None) -> tuple[int, float, float]:
    """-> (number of complete no-ad rounds, time the last attempt finished, time the first attempt started)."""
    allowed = settings.GATE_GRACE_REASONS if allowed is None else allowed
    by_round: dict = {}
    for a in attempts:
        by_round.setdefault(int(a.get("round") or 1), []).append(a)
    n = sum(1 for v in by_round.values() if _round_clean(v, allowed))
    last = max((float(a.get("finished_at") or 0) for a in attempts), default=0.0)
    first = min((float(a.get("t") or 0) for a in attempts), default=0.0)
    return n, last, first


def _deny(code: str, error: str, why: str) -> dict:
    return {"ok": False, "code": code, "error": error, "why": why}


async def grant(s: dict, body: dict) -> dict[str, Any]:
    """All server-side checks for the no-ad grace. On success the session is marked complete (caller delivers)."""
    if not settings.GATE_GRACE:
        return _deny("disabled", "This isn't available right now.", "grace_disabled")
    attempts = await store.attempts_for_sid(s["sid"])
    if any(a.get("outcome") is None for a in attempts):
        return _deny("busy", "An ad is still loading. Please wait a moment.", "attempt_in_flight")
    rounds, last_fin, first_t = clean_rounds(attempts)
    if rounds < settings.GATE_ROUNDS:
        return _deny("rounds", "We still need to try loading an ad for you. Please try again.", f"rounds={rounds}")
    age = _clock() - first_t
    if age < settings.GATE_MIN_TOTAL_SECONDS:
        wait = int(settings.GATE_MIN_TOTAL_SECONDS - age) + 1
        return dict(_deny("too_soon", "Almost there, one moment please.", f"age={age:.1f}"), wait=wait)
    ok, why = await verify_challenge(s, body.get("cid"), body.get("proofs"), last_fin - 0.5)
    if body.get("tamper") is True or body.get("cosmetic") is False:
        ok, why = False, "client_flag"
    if not ok:
        slog.event("grace_denied", level="warning", sid=s["sid"], user_id=s["user_id"], why=why)
        return _deny("blocker", "Ad blocker detected.", why)
    if await store.count_recent_grace(s["user_id"], _clock() - 3600) >= settings.GATE_GRACE_MAX_PER_HOUR:
        return _deny("limited", "Too many requests. Please try again later.", "hourly_cap")
    if not await store.consume_challenge(body["cid"]):
        return _deny("blocker", "Ad blocker detected.", "replayed")
    await store.log_grace({"sid": s["sid"], "user_id": s["user_id"], "token": s.get("token"), "rounds": rounds, "cid": body["cid"]})
    slog.event("grace_granted", sid=s["sid"], user_id=s["user_id"], rounds=rounds)
    return {"ok": True}
