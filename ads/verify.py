"""Reward decision. Pure. A client 'completed' flag is NEVER enough on its own unless the network has no
documented server-side signal AND the owner explicitly allowed client-attested rewards for it."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .settings import CLIENT_ATTESTED, SERVER_POSTBACK


@dataclass
class Facts:
    network: str
    elapsed: float                       # server clock: now - ad start
    client_completed: bool               # client says the network's completion callback fired
    client_non_valued: bool = False      # Monetag SDK: reward_event_type == 'non_valued'
    claim_age: float | None = None       # seconds since the first completion claim (for server-signal waiting)
    postback_seen: bool = False          # Monetag impression postback arrived
    postback_valued: bool = False


@dataclass
class Policy:
    monetag_postback: bool = False
    min_seconds: int = 5
    wait_seconds: int = 20
    block_non_valued: bool = True
    # Strict (production): ONLY a server-verified signal can grant. Browser-only completion is never enough, whatever
    # the per-network "allow client reward" setting says. Defaults to True (fail closed); dev/test fixtures opt out explicitly.
    strict: bool = True


@dataclass
class Decision:
    action: str                          # grant | wait | deny
    trust: str | None = None
    reason: str = ""
    wait: int = 0


def _wait_or_deny(f: Facts, pol: Policy, reason: str) -> Decision:
    if f.claim_age is None or f.claim_age < pol.wait_seconds:
        return Decision("wait", reason="waiting_for_server_signal", wait=1)
    return Decision("deny", reason=reason)


def decide(f: Facts, pol: Policy, allow_client: bool) -> Decision:
    if not f.client_completed:
        return Decision("deny", reason="not_completed")
    if f.elapsed < pol.min_seconds:
        return Decision("wait", reason="too_fast", wait=max(1, math.ceil(pol.min_seconds - f.elapsed)))
    if f.network == "monetag":
        if f.client_non_valued and pol.block_non_valued:
            return Decision("deny", reason="non_valued")
        if pol.monetag_postback:
            if not f.postback_seen:
                return _wait_or_deny(f, pol, "unverified")
            if not f.postback_valued:
                return Decision("deny", reason="non_valued")
            return Decision("grant", SERVER_POSTBACK, "postback_valued")
    elif f.network != "tads":
        return Decision("deny", reason="unknown_network")
    # Reaching here means no server-side proof exists for this network (Tads.me, or Monetag without postback verification).
    # Tads.me: the owner's explicit opt-in (allow_client, already normalised by settings.merged) decides, also in strict mode.
    # Monetag without postback: strict mode never accepts a browser-only reward.
    if f.network != "tads" and pol.strict:
        return Decision("deny", reason="client_attested_not_allowed")
    if not allow_client:
        return Decision("deny", reason="client_attested_not_allowed")
    return Decision("grant", CLIENT_ATTESTED, "client_callback")
