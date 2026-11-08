"""Network scoring + selection. Pure functions (no I/O) so they are unit-testable.

Honest scope
  * Only MEASURED data is used: our own request/fill/completion/error counters, plus revenue the owner
    confirmed from each network's dashboard (ad_revenue). Nothing is invented or defaulted.
  * Monetag's postback `estimated_price` is shown in reports but is NOT compared against other networks'
    confirmed revenue (different basis). Revenue only influences ranking when >= 2 networks have confirmed
    revenue with enough impressions; otherwise the revenue weight is dropped and reliability metrics decide.
  * A network with fewer than `min_requests` requests in the window is "unmeasured": it is explored with a
    small share of traffic but never ranked on a handful of impressions.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class NetStats:
    network: str
    requests: int = 0
    shown: int = 0
    completed: int = 0
    verified: int = 0
    denied: int = 0
    failed_nofill: int = 0
    failed_error: int = 0
    abandoned: int = 0
    conf_revenue: float = 0.0     # revenue the owner confirmed, only for days that have a confirmed entry
    conf_requests: int = 0        # our requests on those same days
    conf_verified: int = 0        # our verified impressions on those same days

    @property
    def fill_rate(self):
        return self.shown / self.requests if self.requests else None

    @property
    def completion_rate(self):
        return self.completed / self.shown if self.shown else None

    @property
    def error_rate(self):
        return self.failed_error / self.requests if self.requests else None

    @property
    def verified_rate(self):
        return self.verified / self.requests if self.requests else None


@dataclass
class Params:
    min_requests: int = 100
    min_rev_imp: int = 100
    explore: float = 0.15
    weights: dict = None

    def __post_init__(self):
        self.weights = self.weights or {"revenue": 0.55, "fill": 0.15, "completion": 0.15, "errors": 0.05, "recent": 0.10}


def _div(a, b, default=0.0):
    return a / b if b else default


def compute_scores(window: dict, recent: dict, p: Params) -> dict:
    """window/recent: {network: NetStats}. Returns {network: {measured, score, components, rpr, ...}}."""
    out: dict = {}
    k = max(1, p.min_requests)
    measured = [n for n, s in window.items() if s.requests >= p.min_requests]

    # ---- revenue per request on confirmed revenue only, shrunk toward the pooled mean ----
    have_rev = [n for n, s in window.items() if s.conf_requests and s.conf_verified >= p.min_rev_imp]
    comparable = len(have_rev) >= 2
    prior = (_div(sum(window[n].conf_revenue for n in have_rev), sum(window[n].conf_requests for n in have_rev))
             if comparable else 0.0)
    rpr = {}
    for n in measured:
        s = window[n]
        if comparable and n in have_rev:
            rpr[n] = (s.conf_revenue + k * prior) / (s.conf_requests + k)      # shrunk
        elif comparable:
            rpr[n] = prior                                                       # unknown revenue == average
    best_rpr = max(rpr.values(), default=0.0)

    # ---- recent yield (verified / request), shrunk toward the window yield so a quiet hour can't swing it ----
    ry = {}
    for n in measured:
        s, r = window[n], recent.get(n) or NetStats(n)
        ry[n] = (r.verified + 20 * _div(s.verified, s.requests)) / (r.requests + 20)
    best_ry = max(ry.values(), default=0.0)

    w = dict(p.weights)
    if not comparable:
        w["revenue"] = 0.0
    wsum = sum(w.values()) or 1.0
    for n, s in window.items():
        if n not in measured:
            out[n] = {"measured": False, "score": None, "requests": s.requests, "components": {}, "revenue_basis": None}
            continue
        comp = {
            "revenue": _div(rpr.get(n, 0.0), best_rpr) if comparable else None,
            "fill": s.fill_rate or 0.0,
            "completion": s.completion_rate or 0.0,
            "errors": 1.0 - (s.error_rate or 0.0),
            "recent": _div(ry[n], best_ry),
        }
        score = sum(w[c] * (v or 0.0) for c, v in comp.items()) / wsum
        out[n] = {
            "measured": True, "score": round(score, 6), "requests": s.requests,
            "components": {c: (round(v, 4) if v is not None else None) for c, v in comp.items()},
            "revenue_basis": "confirmed" if (comparable and n in have_rev) else None,
            "revenue_per_request_usd": round(rpr[n], 8) if n in rpr and n in have_rev else None,
        }
    return out


def choose_order(eligible: list, scores: dict, priority: list, explore: float, rng=random):
    """Returns (order, weights, reason). `order[0]` is the first pick, the rest is the fallback chain."""
    prio = {n: i for i, n in enumerate(priority)}
    elig = sorted(eligible, key=lambda n: prio.get(n, 99))
    if not elig:
        return [], {}, "no eligible network"
    if len(elig) == 1:
        return elig, {elig[0]: 1.0}, "single eligible network"
    meas = [n for n in elig if (scores.get(n) or {}).get("measured")]
    if not meas:
        raw = {n: 1.0 / (i + 1) for i, n in enumerate(elig)}
        tot = sum(raw.values())
        weights = {n: v / tot for n, v in raw.items()}
        reason = "cold start: no network has enough requests yet, priority-weighted exploration"
    else:
        leader = max(meas, key=lambda n: (scores[n]["score"], -prio.get(n, 99)))
        others = [n for n in elig if n != leader]
        raw = {n: (2.0 if n not in meas else 1.0) for n in others}       # unmeasured nets reach the sample size sooner
        tot = sum(raw.values())
        weights = {n: explore * v / tot for n, v in raw.items()}
        weights[leader] = 1.0 - explore
        reason = f"leader {leader} (score {scores[leader]['score']:.3f}); {explore:.0%} exploration across others"
    names = list(weights)
    first = rng.choices(names, weights=[weights[n] for n in names], k=1)[0]

    def rank(n):
        sc = (scores.get(n) or {}).get("score")
        return (0, -sc, prio.get(n, 99)) if sc is not None else (1, 0.0, prio.get(n, 99))

    rest = sorted((n for n in elig if n != first), key=rank)
    return [first] + rest, {n: round(v, 6) for n, v in weights.items()}, reason


def find_bench(bucket_stats: dict, min_attempts: int, fail_rate: float) -> set:
    """bucket_stats: {network: (attempts, hard_fails)} over the cooldown window."""
    return {n for n, (a, f) in bucket_stats.items() if a >= min_attempts and f / a >= fail_rate}
