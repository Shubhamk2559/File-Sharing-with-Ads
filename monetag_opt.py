"""Monetag zone optimizer — pure logic, no I/O (so it is unit-testable).

Honest scope:
  * Monetag does NOT expose live bids / CPM before an ad is shown. This module only ever
    uses HISTORICAL measured revenue: sum(estimated_price from postbacks) / impressions.
  * estimated_price is Monetag's per-event USD estimate (not a CPM, not final payout).
  * Arms are independent MAIN SDK zones. Which sub-zone/advertiser serves inside a zone is
    Monetag's decision, not ours.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class Arm:
    zone: str
    attempts: int = 0        # /api/ad/start se bane ad attempts
    completions: int = 0     # server-validated completions (file unlock ho gaya)
    imp: int = 0             # impression postbacks (valued + non_valued)
    imp_valued: int = 0
    rev_imp: float = 0.0     # sum(estimated_price) of impression events
    rev_imp_sq: float = 0.0  # sum(price^2) of impression events (variance ke liye)
    clicks: int = 0
    rev_click: float = 0.0
    fail_empty: int = 0      # SDK 'Empty feed' (no fill)
    fail_other: int = 0

    @property
    def revenue(self) -> float:
        return self.rev_imp + self.rev_click

    @property
    def ecpm(self) -> float:
        return (self.revenue / self.imp * 1000.0) if self.imp else 0.0


@dataclass
class Params:
    min_imp: int = 300
    explore: float = 0.2
    min_share: float = 0.05
    min_switch_s: float = 24 * 3600
    z: float = 1.28


@dataclass
class Plan:
    leader: str
    weights: dict
    scores: dict = field(default_factory=dict)
    switched: bool = False
    reason: str = ""


def _var_per_imp(a: Arm, pooled_var: float) -> float:
    if a.imp < 2:
        return pooled_var
    m = a.rev_imp / a.imp
    return max(a.rev_imp_sq / a.imp - m * m, 0.0)


def compute_plan(arms: dict, zones: list, default: str, prev_leader, last_switch_ts: float,
                 now: float, p: Params) -> Plan:
    zones = list(dict.fromkeys(zones))
    if default not in zones:
        zones.insert(0, default)
    stats = {z: arms.get(z) or Arm(z) for z in zones}

    tot_imp = sum(a.imp for a in stats.values())
    tot_rev_imp = sum(a.rev_imp for a in stats.values())
    prior = sum(a.revenue for a in stats.values()) / tot_imp if tot_imp else 0.0
    pooled_var = 0.0
    if tot_imp:
        m = tot_rev_imp / tot_imp
        pooled_var = max(sum(a.rev_imp_sq for a in stats.values()) / tot_imp - m * m, 0.0)

    # --- scores: shrinkage toward the pooled mean protects against small-sample luck ---
    k = max(1, p.min_imp)
    shr, se, measured = {}, {}, set()
    scores = {}
    for z, a in stats.items():
        shr[z] = (a.revenue + k * prior) / (a.imp + k)           # USD / impression
        se[z] = math.sqrt(_var_per_imp(a, pooled_var) / max(a.imp, 1))
        if a.imp >= p.min_imp:
            measured.add(z)
        scores[z] = {
            "imp": a.imp,
            "ecpm": round(a.ecpm, 4),
            "shrunk_ecpm": round(shr[z] * 1000, 4),
            "se_ecpm": round(se[z] * 1000, 4),
            "measured": z in measured,
        }

    leader = prev_leader if prev_leader in stats else default
    switched, reason = False, "kept leader"

    if len(zones) == 1:
        return Plan(leader, {leader: 1.0}, scores, False, "single zone")

    if leader not in measured or len(measured) < 2:
        reason = "warm-up: not enough impressions on every zone yet (controlled rotation)"
    else:
        challengers = [z for z in measured if z != leader]
        best = max(challengers, key=lambda z: shr[z])
        diff = shr[best] - shr[leader]
        need = p.z * math.sqrt(se[best] ** 2 + se[leader] ** 2)
        if diff > 0 and diff > need:
            if now - last_switch_ts >= p.min_switch_s:
                reason = f"switched {leader}->{best}: +{diff * 1000:.3f} eCPM beyond noise band {need * 1000:.3f}"
                leader, switched = best, True
            else:
                reason = f"{best} looks better but switch cooldown is active"
        else:
            reason = "no zone is significantly better than the leader"

    # --- allocation: leader keeps most traffic, others keep a floor for exploration ---
    others = [z for z in zones if z != leader]
    base = p.explore / len(others)
    w = {}
    for o in others:
        share = max(p.min_share, base)
        if leader in measured and o in measured:
            gap = shr[leader] - shr[o]
            if gap > p.z * math.sqrt(se[leader] ** 2 + se[o] ** 2):   # clearly worse -> explore less
                share = max(p.min_share, base * 0.5)
        w[o] = share
    total_o = sum(w.values())
    if total_o > 0.5:                                  # leader hamesha >= 50% (conservative)
        w = {o: s * 0.5 / total_o for o, s in w.items()}
        total_o = 0.5
    w[leader] = 1.0 - total_o
    return Plan(leader, {z: round(w[z], 6) for z in zones}, scores, switched, reason)


def find_bench(bucket_stats: dict, min_attempts: int, fail_rate: float) -> set:
    """bucket_stats: {zone: (attempts, hard_fails)} over the recent window."""
    out = set()
    for z, (attempts, fails) in bucket_stats.items():
        if attempts >= min_attempts and fails / attempts >= fail_rate:
            out.add(z)
    return out


def pick_zone(weights: dict, benched: set, default: str, rng=random) -> str:
    cand = {z: w for z, w in weights.items() if z not in benched and w > 0}
    if not cand:
        return default
    zs = list(cand)
    return rng.choices(zs, weights=[cand[z] for z in zs], k=1)[0]
