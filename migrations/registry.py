"""The ordered list of schema migrations. APPEND ONLY: never edit or renumber a released migration, add a new version instead.

Every `up` is idempotent (index creation with identical key+options is a no-op in MongoDB, bookkeeping writes are upserts), additive
(no document is read, changed or deleted) and compatible with databases created by earlier releases. Index names are never customised
so they stay identical to the ones already in production.

  1 core_indexes          files / sessions / pending_deletes / stats  (previously db.init)
  2 monetag_indexes       Monetag analytics collections               (previously monetag_store.init) + monetag_ads.sid lookup index
  3 smart_ads_indexes     ad_* collections + gate (ads/migrate.INDEXES, previously ads.store.init) + legacy marker document
  4 rate_limit_indexes    TTL cleanup for the shared rate limiter (ads/ratelimit.py)
  5 query_indexes         indexes for queries that were collection scans (files listing sort, rewards listing)

Environment-dependent settings (the sessions TTL equals SESSION_TTL) are not versioned: `reconcilers()` runs on every start.
"""
from __future__ import annotations

from ads import migrate as ads_migrate
from migrator import Migration, ensure_index, index_migration

CORE_INDEXES = [
    ("files", "token", {"unique": True}),
    ("sessions", "sid", {"unique": True}),
    ("pending_deletes", "delete_at", {}),
    ("stats", "day", {"unique": True}),
    ("files", [("src_chat", 1), ("src_head", 1)], {"sparse": True}),
]

MONETAG_INDEXES = [
    ("monetag_ads", "created", {"expireAfterSeconds": 30 * 86400}),
    ("monetag_events", "created", {"expireAfterSeconds": 30 * 86400}),
    ("monetag_daily", "day", {}),
    ("monetag_buckets", "bucket", {}),
    ("monetag_buckets", "expire_at", {"expireAfterSeconds": 0}),
    ("monetag_country", "day", {}),
    ("monetag_history", "created", {"expireAfterSeconds": 180 * 86400}),
    ("monetag_ads", "sid", {}),                      # count_empty_fails(): {sid, outcome, reason}
]

RATE_LIMIT_INDEXES = [
    ("rate_limits", "expire_at", {"expireAfterSeconds": 0}),
]

QUERY_INDEXES = [
    ("files", [("created_at", -1)], {}),             # db.list_files / services.files: sort(created_at, -1) over the whole collection
    ("ad_rewards", [("t", -1)], {}),                 # store.recent_rewards: sort(t, -1)
]

MIGRATIONS = [
    index_migration(1, "core_indexes", CORE_INDEXES),
    index_migration(2, "monetag_indexes", MONETAG_INDEXES),
    index_migration(3, "smart_ads_indexes", ads_migrate.INDEXES, extra_up=ads_migrate.write_marker),
    index_migration(4, "rate_limit_indexes", RATE_LIMIT_INDEXES),
    index_migration(5, "query_indexes", QUERY_INDEXES),
]
assert [m.version for m in MIGRATIONS] == sorted({m.version for m in MIGRATIONS})


def reconcilers(session_ttl: int):
    """Idempotent, environment-dependent steps run on EVERY start (after the versioned migrations)."""
    async def sessions_ttl(c):
        # SESSION_TTL is configurable; if it changed since the index was created the TTL is updated in place (collMod).
        await ensure_index(c("sessions"), "created", expireAfterSeconds=int(session_ttl))
    return [sessions_ttl]
