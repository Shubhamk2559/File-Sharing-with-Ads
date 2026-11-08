"""Runs the REAL main.py with Telegram / FastAPI / Mongo stubbed out, then prints a JSON report. Used by test_main_wiring.py
(subprocess, so stubs and config import state never leak into the other tests). Not a test module itself."""
import asyncio
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/tests")


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class Resp:
    def __init__(self, content="", status_code=200, headers=None, media_type=None, **_):
        self.body, self.status_code, self.headers = content, status_code, headers or {}


class HTTPException(Exception):
    def __init__(self, status_code=500, detail=None): self.status_code = status_code


class FastAPI:
    def __init__(self, **_): self.routes = {}
    def _reg(self, method):
        def deco(path, **kw):
            def wrap(fn): self.routes[(method, path)] = fn; return fn
            return wrap
        return deco
    get = property(lambda s: s._reg("GET")); post = property(lambda s: s._reg("POST"))
    def api_route(self, path, methods=("GET",), **kw): return self._reg(methods[0])(path)
    def exception_handler(self, exc):
        return lambda fn: fn
    def add_middleware(self, cls, **kw): self.middleware = (cls, kw)


class Request:  # only used as an annotation
    pass


mod("pymongo")
mod("pymongo.errors", PyMongoError=type("PyMongoError", (Exception,), {}))
mod("fastapi", FastAPI=FastAPI, Header=lambda default=None: default, HTTPException=HTTPException, Request=Request)
mod("fastapi.responses", HTMLResponse=Resp, PlainTextResponse=Resp, JSONResponse=Resp, Response=Resp)
mod("fastapi.exceptions", RequestValidationError=type("RVE", (Exception,), {}))
mod("fastapi.exception_handlers", request_validation_exception_handler=None)
class _Update:
    @classmethod
    def de_json(cls, data, bot):
        if data == "boom":
            raise ValueError("bad payload")
        return cls() if data else None


mod("telegram", Update=_Update)
mod("telegram.error", Forbidden=type("Forbidden", (Exception,), {}), TelegramError=type("TelegramError", (Exception,), {}))

from tests.fakemongo import FakeDB, FakeSessions  # noqa: E402

fdb = FakeDB()
sess = FakeSessions(fdb)


class Bot:
    token, username = "123456:TEST-TOKEN", "bot"


import observability as _obs  # noqa: E402


class Manager:
    queue: list = []
    main = types.SimpleNamespace(bot=Bot(), bot_data={"bot_id": 1}, update_queue=types.SimpleNamespace(put=None))
    secret = "s"
    alerts = _obs.ALERTS
    db_dep = _obs.Dependency("mongodb", 3)
    webhook_dep = _obs.Dependency("telegram_webhook", 3)
    webhook_info: dict = {}
    tasks = types.SimpleNamespace(snapshot=lambda: {})
    async def start(self): pass
    async def stop(self): pass
    async def background_loop(self): pass
    async def active_usernames(self): return ["bot"]


mod("manager", BotManager=Manager)
mod("delivery", send_file=None)
mod("db", get_session=sess.get_session, get_file=sess.get_file, create_session=sess.create_session, start_ad=sess.start_ad,
    complete_ad=sess.complete_ad, claim_delivery=sess.claim_delivery, unclaim_delivery=sess.unclaim_delivery,
    grant_grace=sess.grant_grace, _db=fdb)


async def _ping():
    if fdb.down:
        raise type("ConnectionFailure", (Exception,), {})("db down")


sys.modules["db"].ping = _ping
_queued = []


async def _put(u): _queued.append(u)


Manager.main.update_queue.put = _put

import main  # noqa: E402  (the real thing)
import monetag_store  # noqa: E402
from ads import settings  # noqa: E402

monetag_store._DB = fdb
out = {"secret_in_html": bool(main.MONETAG_POSTBACK_SECRET) and main.MONETAG_POSTBACK_SECRET in main._HTML,
       "routes": sorted(p for (_m, p) in main.app.routes), "strict": settings.RUNTIME["strict"],
       "probe": json.loads(main._CFG.replace("<\\/", "</"))["probe"]}


class Req:
    def __init__(self, q): self.query_params, self.headers, self.client = q, {}, types.SimpleNamespace(host="9.9.9.9")


async def scenario():
    SECRET = main.MONETAG_POSTBACK_SECRET
    await sess.create_session("sid1", "tok1", 777, 777, 1, 1)
    await monetag_store.record_attempt("sid1.abcd1234", "sid1", main.MONETAG_DEFAULT_ZONE, None, user_id=777)
    route = main.app.routes.get(("GET", "/api/monetag/postback"))
    out["postback_mounted"] = route is not None
    if route:
        base = {"ymid": "sid1.abcd1234", "event_type": "impression", "reward_event_type": "valued",
                "zone_id": main.MONETAG_DEFAULT_ZONE, "telegram_id": "777"}
        r = await route(Req(dict(base, secret="wrong")));            out["bad_secret"] = [r.status_code, r.body]
        r = await route(Req(dict(base, secret=SECRET, telegram_id="1")));  out["wrong_user"] = [r.status_code, r.body]
        r = await route(Req(dict(base, secret=SECRET)));             out["valid"] = [r.status_code, r.body]
        r = await route(Req(dict(base, secret=SECRET)));             out["duplicate"] = [r.status_code, r.body]
        ad = await monetag_store.get_ad("sid1.abcd1234")
        out["verified_flags"] = [ad["pb_seen"], ad["pb_valued"]]
    # _auth: fresh session passes the expiry gate (then fails on initData), an old one is reported as expired
    s, _a, err = await main._auth({"sid": "sid1", "init_data": ""})
    out["auth_fresh"] = err
    sess.col.docs[0]["created"] = datetime.now(timezone.utc) - timedelta(seconds=main.SESSION_TTL + 5)
    s, _a, err = await main._auth({"sid": "sid1", "init_data": ""})
    out["auth_expired"] = err


class Req2:
    def __init__(self, q=None, headers=None, body=None, ip="9.9.9.9"):
        self.query_params, self.headers, self.client = q or {}, headers or {}, types.SimpleNamespace(host=ip)
        self._body = body if body is not None else {}
        self.url, self.method = types.SimpleNamespace(path="/x"), "GET"
    async def json(self): return self._body


async def batch2_scenario():
    import migrator
    from migrations import registry
    # --- /readyz: 503 until every migration is recorded + verified, 503 again while the database is unreachable
    import health
    main.readiness.cache_seconds = 0                                   # the real endpoint caches for a few seconds; the scenario flips state quickly
    ready = main.app.routes[("GET", "/readyz")]
    health.LIFECYCLE.begin_start()                                     # startup running: not ready, and the database is not even touched
    out["readyz_starting"] = (await ready()).status_code
    health.LIFECYCLE.mark_ready()
    out["readyz_before"] = (await ready()).status_code                 # started, but migrations not recorded yet
    await migrator.ensure_schema(lambda n: fdb[n], registry.MIGRATIONS, reconcile=registry.reconcilers(1800), poll=0.01, wait_timeout=5)
    out["readyz_after"] = (await ready()).status_code
    fdb.down = True
    out["readyz_down"] = (await ready()).status_code
    fdb.down = False
    out["readyz_recovered"] = (await ready()).status_code

    # --- client IP: a forged leftmost X-Forwarded-For does not pick the bucket (TRUSTED_PROXY_COUNT=1: the entry our proxy appended)
    out["ip_spoof"] = sorted({main._client_ip(Req2(headers={"x-forwarded-for": f"10.1.1.{i}, 203.0.113.5"})) for i in range(20)})

    # --- postback guessing from rotating fake X-Forwarded-For values is still ONE bucket => locked out after 20 failures
    route = main.app.routes.get(("GET", "/api/monetag/postback"))      # not mounted without a secret (development)
    if route:
        codes = []
        for i in range(30):
            r = await route(Req2({"secret": "guess-%d-guess-guess-guess" % i, "ymid": "a.bcde"}, headers={"x-forwarded-for": f"10.2.2.{i}, 198.51.100.9"}))
            codes.append(r.status_code)
            if r.status_code == 429:
                out["postback_429_retry_after"] = r.headers.get("Retry-After")
        out["postback_codes"] = [codes.count(403), codes.count(429)]
        r = await route(Req2({"secret": main.MONETAG_POSTBACK_SECRET, "ymid": "nope.nope"}, headers={"x-forwarded-for": "198.51.100.9"}))
        out["postback_real_secret_after_lockout"] = r.status_code != 429           # Monetag is never locked out

    # --- legacy Mini App endpoint: IP limit applies before the body is read
    done = main.app.routes[("POST", "/api/ad/done")]
    res = [await done(Req2(headers={"x-forwarded-for": "203.0.113.50"}, body={})) for _ in range(5)]
    out["legacy_done"] = [r.get("code") for r in res]

    # --- permanent link flood
    link = main.app.routes[("GET", "/f/{token}")]
    codes = [(await link("abcd1234", Req2(headers={"x-forwarded-for": "203.0.113.60"}))).status_code for _ in range(5)]
    out["link_codes"] = codes

    # --- admin/monetag brute force (token configured by the test environment)
    adm = main.app.routes.get(("GET", "/admin/monetag"))
    if adm and main.MONETAG_ADMIN_TOKEN:
        st = []
        for _ in range(5):
            try:
                await adm(Req2(headers={"x-forwarded-for": "203.0.113.70"}), 7, "Bearer wrong")
            except Exception as e:  # noqa: BLE001
                st.append(getattr(e, "status_code", None))
        out["admin_codes"] = st

    # --- webhook secret guessing
    hook = main.app.routes[("POST", "/webhook")]
    st = []
    for _ in range(4):
        try:
            await hook(Req2(headers={"x-forwarded-for": "203.0.113.80"}), "wrong")
        except Exception as e:  # noqa: BLE001
            st.append(getattr(e, "status_code", None))
    out["webhook_codes"] = st


async def batch3_scenario():
    import health
    import observability
    sent = []

    async def sender(text): sent.append(text)

    # --- Telegram webhook failures (state: READY from the batch2 scenario)
    health.LIFECYCLE.state = health.State.READY
    hook = main.app.routes[("POST", "/webhook")]
    r = await hook(Req2(headers={"x-forwarded-for": "198.51.100.200"}, body={"update_id": 1}), "s")
    out["wh_ok"] = [r, len(_queued)]
    r = await hook(Req2(headers={"x-forwarded-for": "198.51.100.200"}, body="boom"), "s")
    out["wh_bad_payload"] = r.status_code
    r = await hook(Req2(headers={"x-forwarded-for": "198.51.100.200"}, body={}), "s")
    out["wh_empty_update"] = r.status_code
    health.LIFECYCLE.state = health.State.STARTING                     # authentic update while we are not ready: Telegram must retry later
    r = await hook(Req2(headers={"x-forwarded-for": "198.51.100.200"}, body={"update_id": 2}), "s")
    out["wh_not_ready"] = [r.status_code, r.headers.get("Retry-After"), len(_queued)]
    health.LIFECYCLE.state = health.State.READY
    out["wh_counters"] = {k: v for k, v in observability.METRICS.snapshot()["counters"].items() if k.startswith("webhook.")}

    # --- traffic gate: not ready => every route except / and /readyz answers 503 (ASGI middleware that main.py installed)
    cls, kw = main.app.middleware
    sent_msgs = []

    async def asend(m): sent_msgs.append(m)

    async def inner(scope, receive, send): await send({"type": "http.response.start", "status": 200, "headers": []})

    gate = cls(inner, **kw)
    res = {}
    for st in (health.State.STARTING, health.State.FAILED, health.State.STOPPING, health.State.READY):
        health.LIFECYCLE.state = st
        row = []
        for path in ("/api/smart/start", "/f/abc", "/webhook", "/admin/health", "/", "/readyz"):
            sent_msgs.clear()
            await gate({"type": "http", "path": path}, None, asend)
            row.append(sent_msgs[0]["status"])
        res[st.value] = row
    out["gate"] = res
    health.LIFECYCLE.state = health.State.READY

    # --- /admin/health: auth + content never contains a secret or an identifier
    adm = main.app.routes.get(("GET", "/admin/health"))
    if adm and main.MONETAG_ADMIN_TOKEN:
        codes = []
        for _ in range(3):
            try:
                await adm(Req2(headers={"x-forwarded-for": "203.0.113.90"}), "Bearer wrong")
                codes.append(200)
            except Exception as e:  # noqa: BLE001
                codes.append(getattr(e, "status_code", None))
        out["health_bad_token"] = codes
        r = await adm(Req2(headers={"x-forwarded-for": "203.0.113.91"}), "Bearer " + main.MONETAG_ADMIN_TOKEN)
        body = json.dumps(r.body, default=str)
        out["health_status"] = r.status_code
        out["health_keys"] = sorted(r.body)
        out["health_leaks"] = [x for x in (main.MONETAG_POSTBACK_SECRET, main.MONETAG_ADMIN_TOKEN, "123456:TEST-TOKEN", "test-webhook-secret-0123456789",
                                           "mongodb://unused", "203.0.113.91") if x in body]

    # --- unhandled errors: generic 500 body, no detail
    r = await main._unhandled(Req2(), RuntimeError("secret detail 123456:TEST-TOKEN"))
    out["unhandled"] = [r.status_code, r.body]
    # --- MongoDB errors: 503 + Retry-After
    class AutoReconnect(main.PyMongoError): pass
    r = await main._db_error(Req2(), AutoReconnect("db down"))
    out["db_error"] = [r.status_code, r.headers.get("Retry-After"), r.body]


asyncio.run(scenario())
asyncio.run(batch2_scenario())
asyncio.run(batch3_scenario())
print(json.dumps(out))
