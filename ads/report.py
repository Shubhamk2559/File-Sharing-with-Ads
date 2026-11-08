"""Analytics from REAL records only. Confirmed revenue = what the owner entered from each network's dashboard.
Estimated revenue exists only for Monetag (postback estimated_price) and is always labelled as an estimate."""
from __future__ import annotations

import html
import time

from . import engine, settings, store
from .settings import NETWORKS, NET_LABEL


def _r(a, b):
    return (a / b) if b else None


def _round(v, n=4):
    return None if v is None else round(v, n)


async def build(days: int = 7) -> dict:
    days = max(1, min(int(days), 90))
    cfg = await engine.get_config(force=True)
    wdays = store.window_days(days)
    rows = await store.daily_rows(wdays)
    window = engine._fold(rows)
    rev_rows = await store.revenue_rows(wdays)
    est = await store.monetag_estimates(days)
    plan = await store.get_doc("plan")
    health = await store.get_doc("health")
    today = store.day_key()
    all_rows = await store.all_daily_rows()
    total_verified = {n: sum(int(r.get("verified", 0)) for r in all_rows if r.get("network") == n) for n in NETWORKS}
    now = time.time()
    benched = {n: u for n, u in (plan.get("benched") or {}).items() if u > now}

    blockers = engine.blockers(cfg, benched, now)
    model, shares = plan.get("model") or {}, plan.get("shares") or {}
    probation = {n for n, u in (plan.get("probation") or {}).items() if u > now}

    nets, conf_tot, conf_ver_tot = {}, 0.0, 0
    for n in NETWORKS:
        s = window[n]
        nrows = [r for r in rows if r.get("network") == n]
        conf = [r for r in rev_rows if r.get("network") == n]
        conf_days = {r["day"] for r in conf}
        conf_rev = sum(float(r.get("revenue_usd", 0.0)) for r in conf)
        conf_ver = sum(int(r.get("verified", 0)) for r in nrows if r.get("day") in conf_days)
        conf_ver_tot += conf_ver
        conf_tot += conf_rev
        est_rev = sum(v["revenue"] for v in est.values()) if n == "monetag" and est else None
        est_imp = sum(v["impressions"] for v in est.values()) if n == "monetag" and est else None
        c = cfg["networks"][n]
        configured = settings.is_configured(cfg, n)
        failed = s.failed_nofill + s.failed_error
        if not c["enabled"]:
            status = "disabled"
        elif not configured:
            status = "not_configured"
        elif n in benched:
            status = "benched"
        elif s.requests == 0:
            status = "idle"
        elif s.requests >= 10 and (failed / s.requests) > 0.5:
            status = "degraded"
        else:
            status = "healthy"
        h = health.get(n) or {}
        nets[n] = {
            "label": NET_LABEL[n], "status": status, "enabled": c["enabled"], "configured": configured,
            "trust": settings.trust_for(n), "trust_allowed": settings.trust_allowed(cfg, n),
            "requests": s.requests, "shown": s.shown, "completed": s.completed, "verified": s.verified,
            "verified_server": sum(int(r.get("verified_server", 0)) for r in nrows),
            "verified_client": sum(int(r.get("verified_client", 0)) for r in nrows),
            "denied": s.denied, "failed": failed, "failed_nofill": s.failed_nofill, "failed_error": s.failed_error,
            "abandoned": s.abandoned,
            "fill_rate": _round(s.fill_rate), "completion_rate": _round(s.completion_rate),
            "verified_rate": _round(s.verified_rate), "error_rate": _round(s.error_rate),
            "today_verified": sum(int(r.get("verified", 0)) for r in nrows if r.get("day") == today),
            "total_verified": total_verified[n],
            "revenue": {
                "confirmed_usd": _round(conf_rev, 6) if conf else None,
                "confirmed_days": len(conf_days),
                "estimated_usd": _round(est_rev, 6) if est_rev is not None else None,
                "basis": "confirmed" if conf else ("estimated" if est_rev is not None else "unavailable"),
            },
            "ecpm": {
                "confirmed_usd": _round(_r(conf_rev, conf_ver) * 1000, 4) if conf and conf_ver else None,
                "estimated_usd": _round(_r(est_rev, est_imp) * 1000, 4) if est_rev is not None and est_imp else None,
            },
            "score": (plan.get("scores") or {}).get(n, {}).get("score"),
            "measured": bool((plan.get("scores") or {}).get(n, {}).get("measured")),
            "benched_until": benched.get(n),
            "blockers": blockers[n],
            "blockers_text": [engine.BLOCKER_TEXT[b] for b in blockers[n]],
            "expected": _expected(n, model, shares, probation),
            "last_success": h.get("last_success"), "last_fail": h.get("last_fail"), "last_fail_reason": h.get("last_fail_reason"),
        }

    tot = {k: sum(nets[n][k] for n in NETWORKS) for k in ("requests", "shown", "completed", "verified", "denied", "failed", "abandoned")}
    tot.update(
        fill_rate=_round(_r(tot["shown"], tot["requests"])), completion_rate=_round(_r(tot["completed"], tot["shown"])),
        verified_rate=_round(_r(tot["verified"], tot["requests"])),
        confirmed_revenue_usd=_round(conf_tot, 6) if rev_rows else None,
        overall_ecpm_confirmed_usd=_round(_r(conf_tot, conf_ver_tot) * 1000, 4) if rev_rows and conf_ver_tot else None,
        estimated_revenue_usd=_round(sum(v["revenue"] for v in est.values()), 6) if est else None,
    )
    by_ecpm = max(((n, nets[n]["ecpm"]["confirmed_usd"]) for n in NETWORKS if nets[n]["ecpm"]["confirmed_usd"] is not None),
                  key=lambda x: x[1], default=(None, None))
    by_score = max(((n, nets[n]["score"]) for n in NETWORKS if nets[n]["score"] is not None), key=lambda x: x[1], default=(None, None))

    daily = []
    for d in reversed(wdays):
        daily.append({
            "day": d,
            "verified": {n: sum(int(r.get("verified", 0)) for r in rows if r.get("day") == d and r.get("network") == n) for n in NETWORKS},
            "confirmed_usd": {n: next((float(r["revenue_usd"]) for r in rev_rows if r["day"] == d and r["network"] == n), None) for n in NETWORKS},
            "estimated_usd": {"monetag": (est.get(d) or {}).get("revenue") if est else None},
        })
    earnings = _earnings(cfg, nets, plan)
    secrets_set = {"monetag_postback": settings.RUNTIME["monetag_postback"]}
    return {
        "generated_at": now, "window_days": days, "totals": tot, "networks": nets, "daily": daily,
        "best": {"by_confirmed_ecpm": by_ecpm[0], "by_score": by_score[0]},
        "plan": {"leader": plan.get("leader"), "benched": benched, "updated_at": plan.get("updated_at"),
                 "probation": sorted(probation), "bench_level": plan.get("bench_level") or {}},
        "earnings": earnings, "hints": _hints(cfg, nets),
        "config": {k: cfg[k] for k in ("mode", "strategy", "manual_network", "priority", "fallback_enabled", "max_attempts", "networks", "optimization")},
        "secrets_set": secrets_set,
        "notes": [
            "Confirmed revenue/eCPM = owner-entered dashboard revenue divided by OUR verified impressions on the same days.",
            "Estimated revenue (Monetag only) = sum of postback estimated_price. It is an estimate, not a payout.",
            "Tads exposes no documented reporting API: their revenue is 'unavailable' until you enter it.",
        ],
    }


def _expected(n, model, shares, probation) -> dict:
    m = model.get(n)
    if not m:
        return {"basis": "unavailable", "value_per_1000_usd": None, "p_verified": None, "ev_per_1000_requests_usd": None,
                "share": shares.get(n), "probation": n in probation}
    return {"basis": m["basis"], "value_per_1000_usd": _round(m["value"] * 1000, 4), "p_verified": _round(m["p_mean"]),
            "ev_per_1000_requests_usd": _round(m["ev_mean"] * 1000, 4), "share": shares.get(n), "probation": n in probation}


def _earnings(cfg, nets, plan) -> dict:
    """Expected earnings per 1000 requests under the current routing vs. an equal split across the same networks."""
    live = [n for n in NETWORKS if not nets[n]["blockers"]]
    evs = {n: nets[n]["expected"]["ev_per_1000_requests_usd"] for n in live}
    known = {n: v for n, v in evs.items() if v is not None}
    shares = plan.get("shares") or {}
    comparable = bool(known) and any(nets[n]["expected"]["basis"] in ("confirmed", "owner", "monetag_estimate") for n in known)
    opt = sum(shares.get(n, 0.0) * v for n, v in known.items()) if known and shares else None
    eq = (sum(known.values()) / len(known)) if known else None
    return {"strategy": cfg.get("strategy"), "comparable": comparable, "expected_per_1000_requests_usd": _round(opt, 4),
            "equal_split_per_1000_usd": _round(eq, 4),
            "uplift_pct": _round((opt / eq - 1.0) * 100.0, 1) if (comparable and opt is not None and eq) else None}


def _hints(cfg, nets) -> list:
    out = []
    live = [n for n in NETWORKS if not nets[n]["blockers"]]
    if len(live) <= 1:
        why = "; ".join(f"{nets[n]['label']}: {', '.join(nets[n]['blockers_text'])}" for n in NETWORKS if nets[n]["blockers"])
        out.append(f"Only {len(live)} network{'s' if len(live) != 1 else ''} can receive traffic, so there is nothing to rotate. {why}")
    if cfg.get("mode") != "auto":
        out.append(f"Mode is '{cfg.get('mode')}': smart rotation only runs in 'auto' mode.")
    if len(live) > 1 and not any(nets[n]["expected"]["basis"] in ("confirmed", "owner", "monetag_estimate") for n in live):
        out.append("No network has a revenue figure yet, so ranking uses fill/completion only. Enter each network's eCPM (Networks box) "
                   "or yesterday's revenue below so traffic goes where the money is.")
    stale = [n for n in live if nets[n]["expected"]["basis"] == "owner"]
    if stale:
        out.append("eCPM for " + ", ".join(nets[n]["label"] for n in stale) + " is your own figure, not measured. Add daily confirmed revenue to replace it with real data.")
    return out


def _usd(v):
    return "n/a" if v is None else f"${v:,.2f}"


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.0f}%"


def summary_html(rep: dict) -> str:
    t, cfg = rep["totals"], rep["config"]
    lines = [f"🧠 <b>Smart Ads — last {rep['window_days']}d</b>",
             f"Mode: <b>{html.escape(cfg['mode'])}</b> ({html.escape(str(cfg.get('strategy')))})   Fallback: <b>{'on' if cfg['fallback_enabled'] else 'off'}</b>",
             f"Requests {t['requests']} · shown {t['shown']} · verified <b>{t['verified']}</b> · failed {t['failed']}",
             f"Fill {_pct(t['fill_rate'])} · completion {_pct(t['completion_rate'])}",
             f"Confirmed revenue: <b>{_usd(t['confirmed_revenue_usd'])}</b> (eCPM {_usd(t['overall_ecpm_confirmed_usd'])})",
             f"Estimated (Monetag postbacks): {_usd(t['estimated_revenue_usd'])}\n"]
    icon = {"healthy": "🟢", "idle": "⚪", "degraded": "🟠", "benched": "⏸", "disabled": "⚫", "not_configured": "🔧"}
    for n in NETWORKS:
        b = rep["networks"][n]
        lines.append(f"{icon.get(b['status'], '•')} <b>{b['label']}</b> ({b['status']}): verified {b['verified']} (today {b['today_verified']}), "
                     f"fill {_pct(b['fill_rate'])}, eCPM {_usd(b['ecpm']['confirmed_usd'])}"
                     f"{' ~est ' + _usd(b['ecpm']['estimated_usd']) if b['ecpm']['estimated_usd'] is not None else ''}, "
                     f"trust: {b['trust']}")
    for h in rep.get("hints", [])[:3]:
        lines.append(f"\n💡 {html.escape(h)}")
    e = rep.get("earnings") or {}
    if e.get("expected_per_1000_requests_usd") is not None:
        up = f" ({e['uplift_pct']:+.0f}% vs equal split)" if e.get("uplift_pct") is not None else ""
        lines.append(f"\n📈 Expected per 1000 requests: <b>{_usd(e['expected_per_1000_requests_usd'])}</b>{up}")
    best = rep["best"]
    lines.append(f"\nBest by confirmed eCPM: <b>{best['by_confirmed_ecpm'] or 'n/a'}</b> · by score: <b>{best['by_score'] or 'n/a'}</b>")
    lines.append("<i>Revenue is only 'confirmed' if you entered it from the network dashboard.</i>")
    return "\n".join(lines)
