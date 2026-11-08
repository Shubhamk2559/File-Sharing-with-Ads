"""HTTP surface. Handlers are plain async functions (unit-tested); register() wraps them in FastAPI routes.

IMPORTANT: do NOT add `from __future__ import annotations` to this module. register() imports FastAPI's Request
locally, and FastAPI resolves route-handler annotations from the module globals. With postponed evaluation the
annotation `request: Request` stays an unresolvable string, FastAPI treats `request` as a required *query
parameter*, and every call fails with HTTP 422 {"loc": ["query", "request"], "msg": "Field required"}.
(tests/test_routes_wiring.py guards this.)
"""
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import engine, gate, report, service, settings, slog, store
from . import ratelimit
import observability
from .settings import NETWORKS

HERE = Path(__file__).parent
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_REVENUE_USD = 1_000_000.0


@dataclass
class Ctx:
    auth: Callable                      # async (body) -> (session, application, err)
    identify: Callable                  # (init_data) -> (application, info)
    send_file: Callable
    db: Any
    token_re: Any
    telegram_errors: tuple = ()
    forbidden: tuple = ()
    ads_required: int = 1
    admin_token: str = ""
    manager: Any = None
    limiter: Any = None                 # ratelimit.SharedLimiter: MongoDB-backed, shared by every instance (see ads/ratelimit.py)
    trusted_proxies: int = None         # reverse proxies in front of the app (X-Forwarded-For handling)

    def __post_init__(self):
        self.limiter = self.limiter or ratelimit.build()
        if self.trusted_proxies is None:
            self.trusted_proxies = self.limiter.s.trusted_proxies


async def _limited(ctx: "Ctx", category: str, key, message: str | None = None):
    """None when the request may proceed, else the JSON payload to return. Categories + limits: ads/ratelimit.py DEFAULTS."""
    d = await ctx.limiter.hit(category, key)
    if d.allowed:
        return None
    out = {"ok": False, "code": "rate_limited"}
    if message:
        out["error"] = message
    if d.retry_after:
        out["retry_after"] = d.retry_after
    return out


# ---------------------------------------------------------------- request diagnostics
def summarize_validation(errors) -> list:
    """FastAPI/Pydantic error list -> safe, compact list. Never includes `input` (it may hold Telegram initData)."""
    out = []
    for e in list(errors or [])[:20]:
        loc = ".".join(str(x) for x in (e.get("loc") or ()))
        out.append({"field": loc, "type": str(e.get("type", "")), "msg": str(e.get("msg", ""))[:200]})
    return out


def validation_payload(errors) -> dict:
    items = summarize_validation(errors)
    missing = [i["field"] for i in items if i["type"] == "missing"]
    invalid = [i["field"] for i in items if i["type"] != "missing"]
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if invalid:
        parts.append("invalid: " + ", ".join(invalid))
    return {"ok": False, "code": "validation_error",
            "error": "The request was rejected (" + "; ".join(parts or ["invalid request"]) + ").", "details": items}


async def read_json_body(request) -> tuple:
    """-> (dict, None) or ({}, error_payload). Unlike a silent `{}` fallback, a malformed body is reported as such."""
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        raw = b""
    if not raw:
        return {}, None                              # empty body is allowed: sid/init_data checks give the precise error
    if ctype != "application/json":
        slog.event("bad_request", level="warning", path=str(request.url.path), reason="content_type", content_type=ctype or None)
        return {}, {"ok": False, "code": "bad_content_type",
                    "error": "Requests must be sent as application/json (got %s)." % (ctype or "no content-type")}
    try:
        b = json.loads(raw)
    except ValueError:
        slog.event("bad_request", level="warning", path=str(request.url.path), reason="invalid_json", bytes=len(raw))
        return {}, {"ok": False, "code": "invalid_json", "error": "The request body is not valid JSON."}
    if not isinstance(b, dict):
        slog.event("bad_request", level="warning", path=str(request.url.path), reason="not_object")
        return {}, {"ok": False, "code": "invalid_json", "error": "The request body must be a JSON object."}
    return b, None


def _session_label(body: dict) -> str:
    sid = body.get("sid")
    return "none" if not (isinstance(sid, str) and sid) else "present"


# ---------------------------------------------------------------- Mini App
async def _bootstrap(ctx: Ctx, body: dict):
    """Direct launch (no sid): same checks as the legacy /api/ad/start."""
    application, info = ctx.identify(body.get("init_data", ""))
    if not application:
        return None, {"ok": False, "error": "Please open this inside Telegram."}
    token = info.get("start_param") or ""
    if not ctx.token_re.match(token) or not await ctx.db.get_file(token):
        return None, {"ok": False, "error": "This link is invalid."}
    user_id = info["user"]["id"]
    try:
        await application.bot.send_chat_action(user_id, "upload_document")
    except ctx.forbidden:
        return None, {"ok": False, "code": "no_write", "bot": application.bot.username}
    except ctx.telegram_errors:
        pass
    sid = secrets.token_urlsafe(12)
    await ctx.db.create_session(sid=sid, token=token, user_id=user_id, chat_id=user_id,
                                bot_id=application.bot_data["bot_id"], required=ctx.ads_required)
    return await ctx.db.get_session(sid), None


async def h_start(ctx: Ctx, body: dict, ip: str, country=None) -> dict:
    if (lim := await _limited(ctx, "api_start_ip", ip, "Too many requests. Please wait a minute.")):
        return lim
    had_sid = _session_label(body)
    if had_sid == "present":
        s, _app, err = await ctx.auth(body)
        slog.event("smart_start", sid=had_sid, init_data_len=len(str(body.get("init_data") or "")),
                   session="ok" if not err else "rejected", reason=err)
        if err:
            return {"ok": False, "code": "session", "error": err}
    else:
        s, early = await _bootstrap(ctx, body)
        slog.event("smart_start", sid=had_sid, init_data_len=len(str(body.get("init_data") or "")),
                   session="created" if not early else "rejected", reason=(early or {}).get("error") or (early or {}).get("code"))
        if early:
            return early
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if (lim := await _limited(ctx, "api_start_user", s["user_id"], "Too many attempts. Please wait a minute.")):
        return lim
    return await service.start(s, s["user_id"], country)


async def h_report(ctx: Ctx, body: dict, ip: str) -> dict:
    if (lim := await _limited(ctx, "api_report_ip", ip)):
        return lim
    s, _app, err = await ctx.auth(body)
    if err:
        return {"ok": False, "error": err}
    if (lim := await _limited(ctx, "api_report_user", s["user_id"])):
        return lim
    ev = body.get("event")
    if ev not in ("shown", "skip", "fail"):
        return {"ok": False, "error": "Bad event."}
    return await service.report(s, body.get("ad_id"), ev, str(body.get("reason") or "")[:20])


async def h_done(ctx: Ctx, body: dict, ip: str) -> dict:
    if (lim := await _limited(ctx, "api_done_ip", ip, "Too many requests. Please wait a minute.")):
        return lim
    s, application, err = await ctx.auth(body)
    if err:
        return {"ok": False, "error": err}
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if (lim := await _limited(ctx, "api_done_user", s["user_id"], "Too many requests. Please wait a minute.")):
        return lim
    r = await service.done(s, body.get("ad_id"), body.get("completed") is True, body.get("non_valued") is True)
    if not r.get("granted"):
        return r
    s2 = r["s2"]
    base = {"ok": True, "ads_done": s2["ads_done"], "ads_required": s2["ads_required"]}
    if s2["ads_done"] < s2["ads_required"]:
        return dict(base, delivered=False)
    return await _deliver(ctx, s, s2, application, base)


async def _deliver(ctx: Ctx, s: dict, s2: dict, application, base: dict) -> dict:
    """Exactly-once delivery (claim_delivery is atomic) + prompt-message cleanup."""
    db = ctx.db
    if (lim := await _limited(ctx, "file_delivery_user", s["user_id"], "Too many files requested. Please try again later.")):
        return lim                                    # before the claim: the session stays deliverable, nothing to roll back
    if not await db.claim_delivery(s["sid"]):
        return {"ok": False, "error": "You already received this file."}
    doc = await db.get_file(s2["token"])
    ok = bool(doc) and await ctx.send_file(application.bot, s2["chat_id"], doc, ctx.manager)
    if not ok:
        if not doc:                                    # send_file records its own failures; a missing document never reaches it
            observability.METRICS.inc("delivery.failed")
            slog.event("delivery_failed", level="error", reason="file_missing", file=s2.get("token"))
        await db.unclaim_delivery(s["sid"])
        return {"ok": False, "error": "The file is unavailable right now. Please retry shortly."}
    if s2.get("prompt_msg"):
        try:
            await application.bot.delete_message(s2["chat_id"], s2["prompt_msg"])
        except ctx.telegram_errors:
            pass
    return dict(base, delivered=True)


async def h_challenge(ctx: Ctx, body: dict, ip: str) -> dict:
    """Ad-blocker challenge (see ads/gate.py). Cheap, rate limited, needs a valid Telegram session."""
    if (lim := await _limited(ctx, "gate_challenge_ip", ip, "Too many requests. Please wait a minute.")):
        return lim
    s, _app, err = await ctx.auth(body)
    if err:
        return {"ok": False, "code": "session", "error": err}
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if (lim := await _limited(ctx, "gate_challenge_user", s["user_id"], "Too many requests. Please wait a minute.")):
        return lim
    return await gate.issue(s)


async def h_grace(ctx: Ctx, body: dict, ip: str) -> dict:
    """All rounds failed to show any ad. Delivers only if the server-verified ad-blocker challenge passes."""
    if (lim := await _limited(ctx, "gate_grace_ip", ip, "Too many requests. Please wait a minute.")):
        return lim
    s, application, err = await ctx.auth(body)
    if err:
        return {"ok": False, "code": "session", "error": err}
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if (lim := await _limited(ctx, "gate_grace_user", s["user_id"], "Too many requests. Please wait a minute.")):
        return lim
    r = await gate.grant(s, body)
    if not r.get("ok"):
        return {k: v for k, v in r.items() if k != "why"}          # reason codes stay in the server log
    if not await ctx.db.grant_grace(s["sid"]):
        return {"ok": False, "error": "Duplicate request."}
    s2 = await ctx.db.get_session(s["sid"])
    return await _deliver(ctx, s, s2, application, {"ok": True, "ads_done": s2["ads_done"], "ads_required": s2["ads_required"]})


# ---------------------------------------------------------------- admin
async def admin_check(ctx: Ctx, authorization: str | None, ip: str):
    """None if allowed, else (status, detail). Failed-token counter fails CLOSED if the shared store is down (it only runs after a wrong token)."""
    if not ctx.admin_token:
        return 404, "not found"
    if not (await ctx.limiter.hit("admin_ip", ip)).allowed:
        return 429, "rate limited"
    got = (authorization or "").removeprefix("Bearer ").strip()
    if hmac.compare_digest(got.encode(), ctx.admin_token.encode()):
        return None
    if not (await ctx.limiter.hit("admin_fail_ip", ip)).allowed:
        slog.event("admin_lockout", level="warning", ip=ip)
        return 429, "too many failed attempts"
    slog.event("admin_auth_failed", level="warning", ip=ip)
    return 403, "forbidden"


async def h_admin_overview(days: int) -> dict:
    return await report.build(days)


async def h_admin_config() -> dict:
    cfg = await engine.get_config(force=True)
    return {"config": cfg, "secrets_set": {"monetag_postback": settings.RUNTIME["monetag_postback"]},
            "env_ids": {"tads_widget_id": bool(settings.TADS_WIDGET_ID)},
            "strict": bool(settings.RUNTIME["strict"]),
            # honest labels: Tads.me has no server-side verification, so it is never "server_postback"
            "reward_verification": {n: settings.trust_for(n) for n in NETWORKS}}


async def h_admin_patch_config(body: dict):
    new, errors = await engine.update_config(body)
    return (400, {"ok": False, "errors": errors}) if errors else (200, {"ok": True, "config": new})


async def h_admin_revenue(body: dict):
    day, net = str(body.get("day", "")), body.get("network")
    rev = body.get("revenue_usd")
    errors = []
    if not DAY_RE.match(day):
        errors.append("day must be YYYY-MM-DD")
    elif day > store.day_key():
        errors.append("day cannot be in the future")
    if net not in NETWORKS:
        errors.append("network must be one of " + ",".join(NETWORKS))
    if isinstance(rev, bool) or not isinstance(rev, (int, float)) or not (0 <= rev <= MAX_REVENUE_USD) or rev != rev:
        errors.append("revenue_usd must be a number between 0 and 1,000,000")
    if errors:
        return 400, {"ok": False, "errors": errors}
    await store.set_confirmed_revenue(day, net, float(rev))
    slog.event("confirmed_revenue_set", day=day, network=net, usd=float(rev))
    return 200, {"ok": True}


async def h_admin_rewards(limit: int) -> dict:
    return {"rewards": await store.recent_rewards(max(1, min(limit, 200)))}


# ---------------------------------------------------------------- FastAPI wiring (thin)
def register(app, *, auth, identify, send_file, db, token_re, telegram_errors, forbidden, policy, manager, ads_required,
             monetag_store=None, select_zone=None, sdk_url=None, country=None, clock=time.time, secret: str = "",
             limiter=None, long_poll: float = 0.0) -> Ctx:
    from fastapi import Header, HTTPException, Request
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

    settings.RUNTIME["monetag_postback"] = bool(policy.monetag_postback)
    settings.RUNTIME["strict"] = bool(policy.strict)
    engine.invalidate()                              # config cache embeds the strict-mode normalisation
    gate.configure(secret, clock)
    service.configure(service.Deps(sessions=db, policy=policy, monetag=monetag_store, select_zone=select_zone,
                                   sdk_url=sdk_url, clock=clock, long_poll=long_poll))
    ctx = Ctx(auth=auth, identify=identify, send_file=send_file, db=db, token_re=token_re, telegram_errors=telegram_errors,
              forbidden=forbidden, admin_token=settings.ADS_ADMIN_TOKEN,
              manager=manager, ads_required=ads_required, limiter=limiter)
    js = (HERE / "adapters.js").read_text(encoding="utf-8")
    guard_js = (HERE / "guard.js").read_text(encoding="utf-8")
    admin_html = (HERE / "admin_ads.html").read_text(encoding="utf-8")

    def ip_of(request: Request) -> str:
        # Only the entry added by OUR proxy is trusted (TRUSTED_PROXY_COUNT); a client-supplied X-Forwarded-For can't pick its own bucket.
        return ratelimit.client_ip(request.headers.get("x-forwarded-for"), request.client.host if request.client else None,
                                   ctx.trusted_proxies)

    async def body_of(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return b if isinstance(b, dict) else {}

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """Structured, secret-free 422 for /api/*; other paths keep FastAPI's default body."""
        path = request.url.path
        if not path.startswith("/api/"):
            return await request_validation_exception_handler(request, exc)
        payload = validation_payload(exc.errors())
        slog.event("validation_error", level="warning", path=path, method=request.method,
                   content_type=(request.headers.get("content-type") or "")[:60], details=payload["details"])
        return JSONResponse(payload, status_code=422)

    async def checked_body(request: Request):
        body, bad = await read_json_body(request)
        return body, (JSONResponse(bad, status_code=400) if bad else None)

    async def guard(request: Request, authorization):
        bad = await admin_check(ctx, authorization, ip_of(request))
        if bad:
            raise HTTPException(status_code=bad[0], detail=bad[1])

    js_headers = {"Cache-Control": "public, max-age=300"}

    @app.get("/ads/adapters.js")
    async def adapters_js():                         # legacy path (kept)
        return Response(js, media_type="application/javascript", headers=js_headers)

    # Neutral paths: "/ads/..." and "ad"-looking names are exactly what ad blockers block, which would break the page itself.
    @app.get("/assets/sa.js")
    async def sa_js():
        return Response(js, media_type="application/javascript", headers=js_headers)

    @app.get("/assets/sg.js")
    async def sg_js():
        return Response(guard_js, media_type="application/javascript", headers=js_headers)

    @app.post("/api/gate/challenge")
    async def gate_challenge(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_challenge(ctx, body, ip_of(request))

    @app.post("/api/smart/grace")
    async def smart_grace(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_grace(ctx, body, ip_of(request))

    def _bait(path: str):
        async def handler(request: Request):
            q = request.query_params
            c, k = q.get("c", ""), q.get("k", "")
            if not await ctx.limiter.allow("bait_ip", ip_of(request)):
                c = ""                                # over the limit: neutral answer, no DB work, no proof
            kind, out = await gate.bait_response(path, c, k)
            hdr = {"Cache-Control": "no-store, max-age=0", "Referrer-Policy": "no-referrer"}
            if kind == "img":
                return Response(out, media_type="image/gif", headers=hdr)
            return Response(out, media_type="application/javascript", headers=hdr)
        handler.__name__ = "bait_" + path.strip("/").replace("/", "_").replace(".", "_").replace("-", "_")
        return handler

    for _k, _kind, _path in gate.BAITS:
        app.get(_path)(_bait(_path))

    @app.post("/api/smart/start")
    async def smart_start(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_start(ctx, body, ip_of(request), country(request) if country else None)

    @app.post("/api/smart/report")
    async def smart_report(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_report(ctx, body, ip_of(request))

    @app.post("/api/smart/done")
    async def smart_done(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_done(ctx, body, ip_of(request))

    if ctx.admin_token:
        @app.get("/admin/ads", response_class=HTMLResponse)
        async def admin_page():
            return HTMLResponse(admin_html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                                     "X-Frame-Options": "DENY"})

        @app.get("/admin/ads/api/overview")
        async def admin_overview(request: Request, days: int = 7, authorization: str | None = Header(default=None)):
            await guard(request, authorization)
            return await h_admin_overview(days)

        @app.get("/admin/ads/api/config")
        async def admin_get_config(request: Request, authorization: str | None = Header(default=None)):
            await guard(request, authorization)
            return await h_admin_config()

        @app.post("/admin/ads/api/config")
        async def admin_set_config(request: Request, authorization: str | None = Header(default=None)):
            await guard(request, authorization)
            status, out = await h_admin_patch_config(await body_of(request))
            return JSONResponse(out, status_code=status)

        @app.post("/admin/ads/api/revenue")
        async def admin_revenue(request: Request, authorization: str | None = Header(default=None)):
            await guard(request, authorization)
            status, out = await h_admin_revenue(await body_of(request))
            return JSONResponse(out, status_code=status)

        @app.get("/admin/ads/api/rewards")
        async def admin_rewards(request: Request, limit: int = 50, authorization: str | None = Header(default=None)):
            await guard(request, authorization)
            return await h_admin_rewards(limit)

    return ctx
