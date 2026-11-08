"""Idempotent migration 001: creates the smart-ads collections' indexes and seeds the default config.
Only NEW collections are touched; existing data (files, sessions, bots, stats, monetag_*) is never modified.

Runs automatically on startup as part of the versioned migration set (see migrator.py, migrations/registry.py),
or manually:   python -m migrator     (python -m ads.migrate is kept as an alias)
"""
from __future__ import annotations

import logging

log = logging.getLogger("ads.migrate")
VERSION = 1

# (collection, keys, kwargs)
INDEXES = [
    ("ad_requests", "created", {"expireAfterSeconds": 90 * 86400}),
    ("ad_requests", [("sid", 1)], {}),
    ("ad_requests", [("user_id", 1), ("t", -1)], {}),
    ("ad_requests", [("network", 1), ("t", -1)], {}),
    ("ad_requests", [("outcome", 1), ("t", 1)], {}),
    ("ad_events", "created", {"expireAfterSeconds": 30 * 86400}),
    ("ad_events", [("ad_id", 1)], {}),
    ("ad_rewards", [("sid", 1)], {}),
    ("ad_rewards", [("user_id", 1), ("t", -1)], {}),
    ("ad_rewards", [("network", 1), ("t", -1)], {}),
    ("ad_stats_daily", [("day", 1)], {}),
    ("ad_buckets", [("bucket", 1)], {}),
    ("ad_buckets", "expire_at", {"expireAfterSeconds": 0}),
    ("ad_revenue", [("day", 1)], {}),
    ("ad_plan_history", "created", {"expireAfterSeconds": 180 * 86400}),
    ("ad_gate", "created", {"expireAfterSeconds": 3600}),
    ("ad_grace", "created", {"expireAfterSeconds": 90 * 86400}),
    ("ad_grace", [("user_id", 1), ("t", -1)], {}),
]


async def run(c) -> None:
    """c(name) -> collection. Idempotent. Kept for compatibility (ads.store.init, older callers): the application itself now runs the
    versioned, lock-protected migration set in migrations/registry.py through migrator.ensure_schema(), which includes exactly these indexes."""
    from migrator import ensure_index
    for coll, keys, kw in INDEXES:
        await ensure_index(c(coll), keys, **kw)
    await write_marker(c)
    log.info("smart-ads migration %s applied", VERSION)


async def write_marker(c) -> None:
    """Legacy marker document (ad_config/_id=migration). Still written so tooling that read it keeps working."""
    await c("ad_config").update_one({"_id": "migration"}, {"$set": {"version": VERSION}}, upsert=True)


def main() -> None:  # pragma: no cover - needs real MongoDB
    """Same as `python -m migrator` (the full, versioned, lock-protected set)."""
    from migrator import main as run_all
    run_all()


if __name__ == "__main__":
    main()
