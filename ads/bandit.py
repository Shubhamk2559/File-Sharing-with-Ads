"""Revenue-aware network selection (Thompson sampling). Pure functions: no I/O, fully unit-testable.

Goal: maximise expected earnings per user request.

    expected value of a request on network n  =  P(verified | request, n)  x  value of one verified impression (n)

  * P(verified) is modelled as a Beta posterior over time-decayed evidence (recent days count more), shrunk
    toward the pooled rate of all networks so a handful of impressions cannot swing the ranking.
  * Value per verified impression comes from the best REAL source available, in this order:
      confirmed   owner-entered dashboard revenue / our verified impressions (shrunk toward the owner prior or pooled mean)
      owner       eCPM the owner typed in for that network (clearly labelled, never presented as measured)
      monetag_estimate   Monetag postback estimated_price (only when the owner opts in)
      assumed     unknown networks borrow the mean of the known ones (so they still get explored, never invented upward)
      none        nothing is known anywhere -> ranking uses conversion only
  * Thompson sampling explores in proportion to uncertainty: a network that is probably worse stops getting traffic
    on its own, a network that might be better keeps being tested. A small per-network floor guarantees nobody starves.
  * A network coming back from a cool-down ("probation") only gets a capped share until it proves itself.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

BASES = ("confirmed", "owner", "monetag_estimate", "assumed", "none")


@dataclass
class Evidence:
    requests: float = 0.0
    verified: float = 0.0


def decay_fold(rows: list, ages: dict, half_life_days: float, networks) -> dict:
    """rows: daily rollups [{day, network, requests, verified}]. ages: {day: age_in_days (0 = today)}.
    Returns {network: Evidence} where every day is weighted 0.5 ** (age / half_life)."""
    hl = max(0.25, float(half_life_days))
    out = {n: Evidence() for n in networks}
    for r in rows:
        ev = out.get(r.get("network"))
        age = ages.get(r.get("day"))
        if ev is None or age is None:
            continue
        w = 0.5 ** (age / hl)
        ev.requests += w * int(r.get("requests", 0))
        ev.verified += w * int(r.get("verified", 0))
    return out


def estimate_values(inputs: dict, min_imp: int, use_estimate: bool = False) -> dict:
    """inputs: {network: {conf_revenue, conf_verified, owner_ecpm (USD per 1000 or None), est_revenue, est_impressions}}.
    Returns {network: {value (USD per verified impression), basis}}. Never invents a number out of nothing."""
    k = max(1, int(min_imp))
    pool_rev = sum(float(i.get("conf_revenue") or 0.0) for i in inputs.values() if (i.get("conf_verified") or 0) >= k)
    pool_ver = sum(int(i.get("conf_verified") or 0) for i in inputs.values() if (i.get("conf_verified") or 0) >= k)
    pooled = (pool_rev / pool_ver) if pool_ver else None
    out: dict = {}
    for n, i in inputs.items():
        owner = i.get("owner_ecpm")
        owner_v = (float(owner) / 1000.0) if owner is not None else None
        cv = int(i.get("conf_verified") or 0)
        if cv >= k:
            rev = float(i.get("conf_revenue") or 0.0)
            prior = owner_v if owner_v is not None else pooled
            v = (rev + k * prior) / (cv + k) if prior is not None else rev / cv
            out[n] = {"value": v, "basis": "confirmed"}
        elif owner_v is not None:
            out[n] = {"value": owner_v, "basis": "owner"}
        elif use_estimate and i.get("est_revenue") is not None and int(i.get("est_impressions") or 0) >= k:
            out[n] = {"value": float(i["est_revenue"]) / int(i["est_impressions"]), "basis": "monetag_estimate"}
        else:
            out[n] = {"value": None, "basis": "unknown"}
    known = [v["value"] for v in out.values() if v["value"] is not None]
    if known:
        avg = sum(known) / len(known)
        for v in out.values():
            if v["value"] is None:
                v["value"], v["basis"] = avg, "assumed"
    else:
        for v in out.values():
            v["value"], v["basis"] = 1.0, "none"        # relative units: only conversion differentiates networks
    return out


def build_model(evidence: dict, values: dict, prior_strength: float = 10.0) -> dict:
    """Beta posterior per network + value. JSON-serialisable (it is stored in the plan document)."""
    tot_r = sum(e.requests for e in evidence.values())
    tot_v = sum(e.verified for e in evidence.values())
    p0 = (tot_v + 1.0) / (tot_r + 2.0)
    s = max(1.0, float(prior_strength))
    model = {}
    for n, e in evidence.items():
        a = p0 * s + e.verified
        b = (1.0 - p0) * s + max(0.0, e.requests - e.verified)
        v = values.get(n) or {"value": 1.0, "basis": "none"}
        p = a / (a + b)
        model[n] = {"alpha": round(a, 4), "beta": round(b, 4), "value": v["value"], "basis": v["basis"],
                    "p_mean": round(p, 6), "ev_mean": p * v["value"], "requests": round(e.requests, 2), "verified": round(e.verified, 2)}
    return model


def _sample(n: str, model: dict, rng) -> float:
    m = model.get(n)
    if not m:
        return rng.betavariate(1.0, 1.0)
    return rng.betavariate(max(m["alpha"], 1e-3), max(m["beta"], 1e-3)) * m["value"]


def pick_order(elig: list, model: dict, priority: list, min_share: float = 0.03, probation: set | None = None,
               probation_share: float = 0.1, rng=random):
    """Returns (order, reason). order[0] is the first pick, the rest is the fallback chain (best expected value first)."""
    prio = {n: i for i, n in enumerate(priority)}
    elig = sorted(elig, key=lambda n: prio.get(n, 99))
    probation = probation or set()
    if not elig:
        return [], "no eligible network"
    if len(elig) == 1:
        return elig, "single eligible network"

    def ev(n):
        m = model.get(n)
        return m["ev_mean"] if m else 0.0

    floor = min(0.6, max(0.0, min_share) * len(elig))
    if floor and rng.random() < floor:
        first, reason = rng.choice(elig), f"floor exploration ({min_share:.0%} per network)"
    else:
        draws = {n: _sample(n, model, rng) for n in elig}
        first = max(elig, key=lambda n: (draws[n], -prio.get(n, 99)))
        reason = "thompson sample"
        if first in probation and rng.random() >= probation_share:
            others = [n for n in elig if n not in probation]
            if others:
                first = max(others, key=lambda n: (draws[n], -prio.get(n, 99)))
                reason = "thompson sample (probation network held back)"
            else:
                reason = "thompson sample (all on probation)"
    rest = sorted((n for n in elig if n != first), key=lambda n: (-ev(n), prio.get(n, 99)))
    return [first] + rest, reason


def expected_shares(elig: list, model: dict, priority: list, min_share: float, probation: set, probation_share: float,
                    draws: int = 3000, seed: int = 20260101) -> dict:
    """Monte-Carlo estimate of how traffic splits under the current model (for the dashboard)."""
    rng = random.Random(seed)
    counts = {n: 0 for n in elig}
    if not elig:
        return {}
    for _ in range(draws):
        order, _ = pick_order(elig, model, priority, min_share, probation, probation_share, rng)
        counts[order[0]] += 1
    return {n: round(c / draws, 4) for n, c in counts.items()}


def expected_value_per_1000(shares: dict, model: dict) -> float:
    return 1000.0 * sum(shares.get(n, 0.0) * (model.get(n) or {}).get("ev_mean", 0.0) for n in shares)
