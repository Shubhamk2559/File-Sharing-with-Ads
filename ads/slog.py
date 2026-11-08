"""One JSON object per log line (grep/ship friendly).

Batch 3: fields are sanitised before anything is written - sensitive keys (secret/token/hash/init_data/...) are masked, string values are
redacted, and user/chat ids and IP addresses are replaced by short keyed hashes (see observability.py). The log message stays the compact
`{"ev": ...}` JSON; the production formatter emits the same fields as the top-level JSON record.
"""
from __future__ import annotations

import json
import logging
import time

import observability

_log = logging.getLogger("ads")


def event(name: str, level: str = "info", **fields) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    if not _log.isEnabledFor(lvl):
        return
    clean = observability.sanitize_fields(fields)
    rec = {"ev": name, "t": round(time.time(), 3)}
    rec.update(clean)
    _log.log(lvl, json.dumps(rec, default=str, separators=(",", ":")), extra={"event_fields": {"ev": name, **clean}})
