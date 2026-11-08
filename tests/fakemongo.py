"""Minimal in-memory stand-in for the Motor collections used by ads.store / db sessions (test-only).
Supports: equality, None-matches-missing, $or, $lt/$lte/$gt/$gte/$ne, $expr{$gte}, $set/$inc/$setOnInsert, upsert, sort.

Added for the shared rate limiter / migrations tests (all opt-in, existing behaviour unchanged):
  * every operation is atomic (no await inside the critical section) like a single-document MongoDB operation; with
    FakeDB(yield_ops=True) each operation first yields to the event loop so concurrent tasks really interleave
  * upsert semantics incl. DuplicateKeyError when the generated _id already exists (the rate limiter's "at the limit" signal)
  * unique indexes, create_index / index_information with MongoDB's conflict errors (code 85 / 86 / 11000), collMod for TTL changes
  * delete_one, count_documents, TTL reaping (FakeDB.reap), and fault injection (FakeDB.down / FakeDB.fail_when)
"""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone

from ads.store import DuplicateKeyError


class OperationFailure(Exception):
    def __init__(self, msg="", code=None):
        super().__init__(msg)
        self.code = code


class ConnectionFailure(Exception):
    """Same class NAME as pymongo.errors.ConnectionFailure (migrator classifies transient errors by class name)."""

_MISSING = object()


def _get(doc, path):
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _cmp_ok(val, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        v = None if val is _MISSING else val
        for op, arg in cond.items():
            if op == "$ne" and not (v != arg):
                return False
            if op in ("$lt", "$lte", "$gt", "$gte"):
                if v is None:
                    return False
                if not {"$lt": v < arg, "$lte": v <= arg, "$gt": v > arg, "$gte": v >= arg}[op]:
                    return False
        return True
    if cond is None:
        return val is _MISSING or val is None
    return val is not _MISSING and val == cond


def matches(doc, flt):
    for k, cond in flt.items():
        if k == "$or":
            if not any(matches(doc, f) for f in cond):
                return False
        elif k == "$expr":
            (op, (a, b)), = cond.items()
            assert op == "$gte"
            va, vb = (doc.get(x[1:]) if isinstance(x, str) else x for x in (a, b))
            if not (va >= vb):
                return False
        elif not _cmp_ok(_get(doc, k), cond):
            return False
    return True


def _apply(doc, upd, inserting=False):
    for k, v in upd.get("$set", {}).items():
        doc[k] = v
    for k, v in upd.get("$inc", {}).items():
        doc[k] = doc.get(k, 0) + v
    if inserting:
        for k, v in upd.get("$setOnInsert", {}).items():
            doc[k] = v


class Cursor:
    def __init__(self, docs, coll=None): self.docs, self.coll = docs, coll
    def limit(self, n): self.docs = self.docs[:n]; return self
    def sort(self, key, direction=1):
        self.docs = sorted(self.docs, key=lambda d: d.get(key, 0), reverse=direction < 0); return self
    async def to_list(self, length=None):
        if self.coll is not None:
            await self.coll._enter("find")
        return [copy.deepcopy(d) for d in self.docs]


def _key_list(keys):
    return [(keys, 1)] if isinstance(keys, str) else [(k, int(d)) for k, d in keys]


class UpdateResult:
    def __init__(self, matched=0, upserted=False): self.matched_count, self.upserted_id = matched, (True if upserted else None)


class Collection:
    def __init__(self, name="", db=None):
        self.docs = []
        self.name = name
        self.database = db
        self.indexes = {"_id_": {"v": 2, "key": [("_id", 1)]}}

    # ---- fault injection / scheduling
    async def _enter(self, op):
        if self.database is not None:
            if self.database.yield_ops:
                await asyncio.sleep(0)
            self.database._check(op, self.name)

    def _find(self, flt): return [d for d in self.docs if matches(d, flt)]

    def _unique_conflict(self, doc, skip=None):
        for ix in self.indexes.values():
            if not ix.get("unique"):
                continue
            keys = [k for k, _ in ix["key"]]
            val = tuple(doc.get(k, None) for k in keys)
            if ix.get("sparse") and all(v is None for v in val):
                continue
            for d in self.docs:
                if d is not skip and tuple(d.get(k, None) for k in keys) == val:
                    return True
        return False

    def _insert(self, doc):
        if "_id" in doc and any(d.get("_id") == doc["_id"] for d in self.docs):
            raise DuplicateKeyError(str(doc["_id"]))
        if self._unique_conflict(doc):
            raise DuplicateKeyError("unique index")
        self.docs.append(doc)

    async def insert_one(self, doc):
        await self._enter("insert_one")
        self._insert(copy.deepcopy(doc))

    async def find_one(self, flt=None, **_):
        await self._enter("find_one")
        r = self._find(flt or {})
        return copy.deepcopy(r[0]) if r else None

    def find(self, flt=None): return Cursor(self._find(flt or {}), self)

    async def count_documents(self, flt=None, **_):
        await self._enter("count_documents")
        return len(self._find(flt or {}))

    def _upsert_doc(self, flt, upd):
        d = {k: v for k, v in flt.items() if not k.startswith("$") and not isinstance(v, dict)}
        _apply(d, upd, inserting=True)
        return d

    async def update_one(self, flt, upd, upsert=False):
        await self._enter("update_one")
        r = self._find(flt)
        if r:
            _apply(r[0], upd)
            return UpdateResult(1)
        if upsert:
            self._insert(self._upsert_doc(flt, upd))
            return UpdateResult(0, True)
        return UpdateResult(0)

    async def find_one_and_update(self, flt, upd, upsert=False, return_document=None, sort=None, **_):
        await self._enter("find_one_and_update")
        r = self._find(flt)
        if sort:
            for key, direction in reversed(sort):
                r.sort(key=lambda d: d.get(key, 0), reverse=direction < 0)
        if not r:
            if not upsert:
                return None
            d = self._upsert_doc(flt, upd)
            self._insert(d)                                  # DuplicateKeyError when the filter missed an EXISTING _id (MongoDB does the same)
            return copy.deepcopy(d) if return_document else None
        before = copy.deepcopy(r[0])
        _apply(r[0], upd)
        # pymongo ReturnDocument.AFTER == True; default BEFORE
        return copy.deepcopy(r[0]) if return_document else before

    async def delete_one(self, flt):
        await self._enter("delete_one")
        r = self._find(flt)
        if r:
            self.docs.remove(r[0])
        return UpdateResult(1 if r else 0)

    # ---- indexes
    async def create_index(self, keys, **opts):
        await self._enter("create_index")
        kl = _key_list(keys)
        name = "_".join(f"{k}_{d}" for k, d in kl)
        wanted = {k: v for k, v in opts.items() if v is not None and v is not False}   # expireAfterSeconds=0 must be kept
        for n, ix in self.indexes.items():
            if [(k, int(d)) for k, d in ix["key"]] == kl:
                have = {k: v for k, v in ix.items() if k not in ("v", "key")}
                if have == wanted:
                    return n
                raise OperationFailure(f"Index already exists with different options: {n}", code=85)
        if wanted.get("unique"):
            seen = set()
            for d in self.docs:
                t = tuple(d.get(k) for k, _ in kl)
                if t in seen:
                    raise OperationFailure("E11000 duplicate key error building index", code=11000)
                seen.add(t)
        self.indexes[name] = {"v": 2, "key": kl, **wanted}
        return name

    async def index_information(self):
        await self._enter("index_information")
        return copy.deepcopy(self.indexes)


class FakeDB:
    def __init__(self, yield_ops=False):
        self.c = {}
        self.yield_ops = yield_ops
        self.down = False                 # True => every operation raises ConnectionFailure (database unreachable / restarting)
        self.fail_when = None             # callable(op, collection) -> Exception | None

    def __getitem__(self, name):
        if name not in self.c:
            self.c[name] = Collection(name, self)
        return self.c[name]

    def _check(self, op, coll):
        if self.down:
            raise ConnectionFailure("simulated: database unreachable")
        if self.fail_when:
            e = self.fail_when(op, coll)
            if e:
                raise e

    async def command(self, name, coll, **kw):
        """Only collMod {index: {keyPattern, expireAfterSeconds}} (what migrator.ensure_index issues)."""
        self._check("command", coll)
        assert name == "collMod"
        spec = kw["index"]
        kl = [(k, int(d)) for k, d in spec["keyPattern"].items()]
        for ix in self[coll].indexes.values():
            if [(k, int(d)) for k, d in ix["key"]] == kl:
                ix["expireAfterSeconds"] = spec["expireAfterSeconds"]

    def reap(self, now_ts):
        """MongoDB's TTL monitor: delete documents whose TTL-indexed datetime + expireAfterSeconds has passed."""
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        n = 0
        for coll in self.c.values():
            for ix in coll.indexes.values():
                if "expireAfterSeconds" not in ix:
                    continue
                field = ix["key"][0][0]
                keep = []
                for d in coll.docs:
                    v = d.get(field)
                    if isinstance(v, datetime):
                        v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                        if (now - v).total_seconds() >= ix["expireAfterSeconds"]:
                            n += 1
                            continue
                    keep.append(d)
                coll.docs = keep
        return n


class FakeSessions:
    """Same behaviour as db.py's session helpers (start_ad / complete_ad / get_session / create_session / claim_delivery)."""
    def __init__(self, fdb: FakeDB): self.col = fdb["sessions"]

    async def create_session(self, sid, token, user_id, chat_id, bot_id, required, prompt_msg=None):
        await self.col.insert_one({"sid": sid, "token": token, "user_id": user_id, "chat_id": chat_id, "bot_id": bot_id,
                                   "ads_done": 0, "ads_required": required, "ad_started_at": None, "delivered": False,
                                   "prompt_msg": prompt_msg, "created": datetime.now(timezone.utc)})

    async def get_session(self, sid): return await self.col.find_one({"sid": sid})

    async def get_file(self, token): return {"token": token}

    async def start_ad(self, sid, ymid=None, zone=None):
        f = {"ad_started_at": time.time()}
        if ymid:
            f.update(cur_ymid=ymid, cur_zone=zone)
        await self.col.update_one({"sid": sid, "delivered": False}, {"$set": f})

    async def complete_ad(self, sid, min_seconds):
        cutoff = time.time() - min_seconds
        return await self.col.find_one_and_update(
            {"sid": sid, "delivered": False, "ad_started_at": {"$ne": None, "$lte": cutoff}},
            {"$inc": {"ads_done": 1}, "$set": {"ad_started_at": None}}, return_document=True)

    async def grant_grace(self, sid):
        r = await self.col.find_one_and_update({"sid": sid, "delivered": False}, {"$set": {"grace": True}})
        if r is None:
            return False
        await self.col.update_one({"sid": sid}, {"$set": {"ads_done": r["ads_required"]}})
        return True

    async def claim_delivery(self, sid):
        r = await self.col.find_one_and_update(
            {"sid": sid, "delivered": False, "$expr": {"$gte": ["$ads_done", "$ads_required"]}}, {"$set": {"delivered": True}})
        return r is not None

    async def unclaim_delivery(self, sid): await self.col.update_one({"sid": sid}, {"$set": {"delivered": False}})
