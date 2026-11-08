import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import random
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from telegram import Update
from telegram.error import Forbidden, TelegramError

import db
import monetag_engine as engine
import monetag_store as mstore
import health
import observability
from ads import postback as ads_postback, routes as ads_routes, security as ads_security, settings as ads_settings
from ads import ratelimit as ads_ratelimit, slog as ads_slog
from ads.verify import Policy as AdsPolicy
from config import (
    ADS_REQUIRED,
    AD_VERIFY_MODE,
    APP_ENV,
    SECURITY_WARNINGS,
    SESSION_TTL,
    STRICT_ADS,
    POSTBACK_MAX_AGE,
    POSTBACK_REQUIRE_TELEGRAM_ID,
    BLOCK_NON_VALUED,
    AD_CLICK_TO_PLAY,
    NOFILL_GRACE,
    NOFILL_GRACE_FAILS,
    DIRECT_MINIAPP,
    MINIAPP_SHORT_NAME,
    MIN_AD_SECONDS,
    MONETAG_ADMIN_TOKEN,
    MONETAG_DEFAULT_ZONE,
    MONETAG_GEO_HEADER,
    MONETAG_POSTBACK_SECRET,
    MONETAG_SHOW_LOG,
    MONETAG_WINDOW_DAYS,
    MONETAG_ZONES,
    POSTBACK_WAIT_SECONDS,
    RT,
    WEBHOOK_SECRET,
)
from delivery import send_file
from manager import BotManager
import migrator
from migrations import registry as migration_registry

# Structured JSON logs + secret redaction + pseudonymised ids (observability.py), installed BEFORE anything else can log. serve.py already
# did this (idempotent); repeating it makes plain `uvicorn main:app` safe too. httpx stays at WARNING (bot tokens live in Telegram API URLs).
observability.bootstrap(os.environ, RT)
for _w in SECURITY_WARNINGS:
    logging.getLogger("security").warning(_w)
logging.getLogger("security").info("APP_ENV=%s strict_ads=%s verify_mode=%s", APP_ENV, STRICT_ADS, AD_VERIFY_MODE)

manager = BotManager()

# ---- shared (MongoDB) rate limiting: one limiter for every instance, limits from RL_* env vars (see README "Rate limiting") ----
limiter = ads_ratelimit.build(salt=WEBHOOK_SECRET)      # invalid RL_* / TRUSTED_PROXY_COUNT => the process refuses to start
ads_ratelimit.set_default(limiter)                      # handlers.py (Telegram bot) uses the same instance
logging.getLogger("security").info("rate limiting: shared store=%s collection=%s trusted_proxies=%s",
                                   "mongodb", ads_ratelimit.COLLECTION, limiter.s.trusted_proxies)

_bg: set = set()


def _limiter_state(state: str, info: dict) -> None:
    """Rate-limit store degraded / recovered -> ONE owner alert per transition (de-duplicated by the AlertManager)."""
    observability.METRICS.inc("ratelimit.store_" + state)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if state == "degraded":
        coro = manager.alerts.alert("ratelimit_degraded", "Rate-limit store (MongoDB) is unavailable: limits run in strict local fallback "
                                    "mode (brute-force counters deny). Requests are still limited, never unlimited.", severity="warning")
    else:
        coro = manager.alerts.recover("ratelimit_degraded", "Shared rate limiting is back on MongoDB.")
    t = loop.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


limiter.on_state = _limiter_state


def _client_ip(request: Request) -> str:
    """Address used for IP limits. Only the X-Forwarded-For entry appended by OUR proxy is trusted (TRUSTED_PROXY_COUNT)."""
    return ads_ratelimit.client_ip(request.headers.get("x-forwarded-for"),
                                   request.client.host if request.client else None, limiter.s.trusted_proxies)


async def _limited(category: str, key, message: str = "Too many requests. Please wait a minute."):
    """None = proceed; else the JSON payload for the Mini App (same shape as the smart-flow endpoints)."""
    d = await limiter.hit(category, key)
    if d.allowed:
        return None
    out = {"ok": False, "code": "rate_limited", "error": message}
    if d.retry_after:
        out["retry_after"] = d.retry_after
    return out


TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


readiness = health.Readiness(
    health.LIFECYCLE, lambda: db.ping(),
    lambda: migrator.is_complete(lambda name: db._db[name], migration_registry.MIGRATIONS),
    cache_seconds=RT.readyz_cache_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # start() is NON-blocking: migrations -> Telegram -> webhook -> background tasks run as one supervised task, so the server already
    # answers GET / (liveness) while /readyz says "not ready". It is idempotent: a repeated lifespan never starts anything twice.
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(lifespan=lifespan)
# While the lifecycle is not READY (starting, FAILED migration, shutting down) every route except / and /readyz answers 503.
app.add_middleware(health.HealthGate, lifecycle=health.LIFECYCLE)

try:
    from pymongo.errors import PyMongoError
except ImportError:  # tests run without pymongo
    class PyMongoError(Exception):
        pass


@app.exception_handler(PyMongoError)
async def _db_error(_request: Request, exc: PyMongoError):
    """MongoDB unreachable => 503 + Retry-After (not a 500 stack trace); other database errors => generic 500. Never leaks details."""
    if health.is_transient(exc):
        observability.METRICS.inc("http.db_unavailable")
        ads_slog.event("db_unavailable_request", level="error", error=type(exc).__name__)
        if manager.db_dep.failure(exc) == "down":
            await manager.alerts.alert("mongodb_down", "MongoDB is unreachable (requests are failing with 503). Check Atlas status / "
                                       "network allow-list. Rate limits run in degraded (local) mode.")
        return JSONResponse({"ok": False, "code": "unavailable", "error": "Service temporarily unavailable. Please retry shortly."},
                            status_code=503, headers={"Retry-After": "5", "Cache-Control": "no-store"})
    return await _unhandled(_request, exc)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    observability.METRICS.inc("http.unhandled_exception")
    ads_slog.event("unhandled_exception", level="error", path=request.url.path, method=request.method, error=type(exc).__name__)
    observability.HTTP_ERROR_WATCH.hit(type(exc).__name__)
    if observability.HTTP_ERROR_WATCH.crossed():
        top = ", ".join(f"{k}={v}" for k, v in list(observability.HTTP_ERROR_WATCH.breakdown().items())[:5])
        await manager.alerts.alert("http_error_burst", f"{len(observability.HTTP_ERROR_WATCH._ev)} unhandled server errors in 5 minutes "
                                   f"({top}). Check the logs.", severity="warning")
    return JSONResponse({"ok": False, "error": "Internal error. Please retry shortly."}, status_code=500, headers={"Cache-Control": "no-store"})


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    """LIVENESS: the process is up and its event loop answers. Independent of MongoDB / Telegram, so a database outage can never make
    the platform restart a healthy process."""
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


@app.api_route("/readyz", methods=["GET", "HEAD"])
async def readyz():
    """READINESS: startup finished AND MongoDB answers AND every schema migration is recorded + verified. 503 otherwise.
    The body names the failing check only (no hosts, versions, errors or secrets)."""
    ok, body = await readiness.check()
    return JSONResponse(body, status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", manager.secret):
        # brute-force guard on the webhook secret (counter fails closed if the shared store is down; a correct secret is never limited)
        observability.METRICS.inc("webhook.rejected")
        ip = _client_ip(request)
        ads_slog.event("telegram_webhook_rejected", level="warning", reason="bad_secret", ip=ip)
        if not await limiter.allow("webhook_fail_ip", ip):
            raise HTTPException(status_code=429)
        raise HTTPException(status_code=403)

    application = manager.main
    if not application or not health.LIFECYCLE.accepting:
        # Authentic update but we can't process it yet / any more: 503 makes Telegram retry later (it keeps the update queued).
        observability.METRICS.inc("webhook.not_ready")
        return JSONResponse({"ok": False}, status_code=503, headers={"Retry-After": "5"})

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        if update is None:
            raise ValueError("empty update")
    except Exception as e:  # noqa: BLE001
        observability.METRICS.inc("webhook.bad_payload")
        ads_slog.event("telegram_webhook_bad_payload", level="warning", error=type(e).__name__)
        return JSONResponse({"ok": False}, status_code=400)
    await application.update_queue.put(update)
    observability.METRICS.inc("webhook.accepted")
    return {"ok": True}


_REDIRECT_TPL = Path(__file__).with_name("redirect.html").read_text(encoding="utf-8")


def _redirect_page(target: str = "", message: str = "", status: int = 200) -> HTMLResponse:
    page = (
        _REDIRECT_TPL.replace(
            "__META__",
            f'<meta http-equiv="refresh" content="8;url={html.escape(target, quote=True)}">' if target else "",
        )
        .replace("__TARGET_ATTR__", html.escape(target, quote=True))
        .replace("__TARGET_JSON__", json.dumps(target).replace("</", "<\\/"))
        .replace("__MSG_JSON__", json.dumps(message).replace("</", "<\\/"))
    )
    return HTMLResponse(
        page,
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _telegram_target(username: str, token: str) -> str:
    if DIRECT_MINIAPP:
        # Direct Mini App link: bina button dabaye app khulta hai, token start_param me jata hai
        if MINIAPP_SHORT_NAME:
            return f"https://t.me/{username}/{MINIAPP_SHORT_NAME}?startapp={token}"
        return f"https://t.me/{username}?startapp={token}"
    return f"https://t.me/{username}?start={token}"


@app.get("/f/{token}")
async def permanent_link(token: str, request: Request):
    if not await limiter.allow("link_ip", _client_ip(request)):
        return _redirect_page(message="Too many requests. Please wait a minute and try again.", status=429)
    if not TOKEN_RE.match(token):
        return _redirect_page(message="This link doesn't look right.", status=404)
    names = await manager.active_usernames()
    if not names:
        return _redirect_page(message="Service is unavailable right now. Please try again shortly.", status=503)
    return _redirect_page(_telegram_target(random.choice(names), token))


# ===================== WORKER only: Mini App =====================
def validate_init_data(init_data: str, bot_token: str, max_age: int = 3600):
    """Signature sahi ho to {"user": {...}, "start_param": str|None} warna None."""
    try:
        parsed = dict(parse_qsl(init_data or "", keep_blank_values=True))
        received = parsed.pop("hash", None)
        if not received:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            return None
        return {"user": json.loads(parsed["user"]), "start_param": parsed.get("start_param")}
    except Exception:  # noqa: BLE001
        return None


# Default zone ka SDK tag pehle jaisa static rehta hai (koi extra latency nahi).
# Doosre zones sirf tab dynamically load hote hain jab optimizer unhe chune.
_DEFAULT_ZONE = MONETAG_DEFAULT_ZONE
_CFG = json.dumps(
    {
        "zoneId": _DEFAULT_ZONE,
        "showLog": MONETAG_SHOW_LOG,
        "required": ADS_REQUIRED,
        "blockNonValued": BLOCK_NON_VALUED,
        "clickPlay": AD_CLICK_TO_PLAY,
        "grace": bool(NOFILL_GRACE and not ads_settings.SMART_ADS_ENABLED),
        "smart": ads_settings.SMART_ADS_ENABLED,   # multi-network flow (default off = legacy flow)
        "sdk": engine.sdk_url(_DEFAULT_ZONE),
        # Ad gate: how many Monetag->Tads rounds before the ad-blocker check, and whether "no ad + clean device" delivers the file
        "gate": {"rounds": ads_settings.GATE_ROUNDS, "grace": bool(ads_settings.GATE_GRACE)},
        # Ad-blocker / Private DNS check: agar inme se koi host reach na ho to user ko "DNS/AdBlock off karo" screen dikhti hai
        "probe": sorted(
            {engine.sdk_url(z) for z in engine.ZONES}
            | {engine.sdk_url(_DEFAULT_ZONE)}
            | ({ads_settings.TADS_SDK_URL} if (ads_settings.SMART_ADS_ENABLED and ads_settings.TADS_WIDGET_ID and ads_settings.GATE_PROBE_TADS and not STRICT_ADS) else set())
        ),
    }
).replace("</", "<\\/")

def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url or "")
    return m.group(1) if m else ""


# Speed: SDK host se connection pehle hi khol lo (DNS + TLS) aur script ko async rakho,
# taaki sync <script> page render na roke. window.show_<zone> aane tak miniapp.html khud wait karta hai.
_SDK_ORIGINS = sorted({o for o in (_origin(engine.sdk_url(z)) for z in engine.ZONES) if o})
_TAG = "".join(
    f'<link rel="preconnect" href="{html.escape(o, quote=True)}" crossorigin>'
    f'<link rel="dns-prefetch" href="{html.escape(o, quote=True)}">'
    for o in _SDK_ORIGINS
) + (
    f'<script async src="{html.escape(engine.sdk_url(_DEFAULT_ZONE), quote=True)}" '
    f'data-zone="{html.escape(_DEFAULT_ZONE, quote=True)}" '
    f'data-sdk="show_{html.escape(_DEFAULT_ZONE, quote=True)}" '
    f'onerror="window.__sdkErr=1"></script>'
)

_HTML = (
    Path(__file__)
    .with_name("miniapp.html")
    .read_text(encoding="utf-8")
    .replace("__CFG__", _CFG)
    .replace("<!--AD_TAG-->", _TAG)
)

@app.get("/app", response_class=HTMLResponse)
async def miniapp():
    return HTMLResponse(_HTML, headers={"Cache-Control": "no-store"})

@app.get("/api/ping")
async def ping():
    """Mini App ka connectivity check: yeh chale aur ad host na chale => user ke device par AdBlock/Private DNS hai."""
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})

async def _auth(body: dict):
    """Existing session: sid present (from the bot button or a retry)."""
    sid = body.get("sid")
    s = await db.get_session(sid) if isinstance(sid, str) and sid else None
    if not s or ads_security.session_expired(s, SESSION_TTL):     # Mongo's TTL reaper lags by up to a minute: check the age here too
        return None, None, "Session expired. Please open the link again."
    application = manager.main
    info = validate_init_data(body.get("init_data", ""), application.bot.token)
    if not info or info["user"].get("id") != s["user_id"]:
        return None, None, "Verification failed. Please open the link again."
    return s, application, None

def _identify(init_data: str):
    """Direct launch: initData ka signature bot token se verify karo."""
    info = validate_init_data(init_data, manager.main.bot.token)
    return (manager.main, info) if info else (None, None)


def _country(request: Request):
    if not MONETAG_GEO_HEADER:
        return None
    c = (request.headers.get(MONETAG_GEO_HEADER) or "").strip().upper()
    return c if re.fullmatch(r"[A-Z]{2}", c) and c not in ("XX", "T1") else None

# ---- Smart multi-network rotation (Monetag + Tads). Only mounted when SMART_ADS_ENABLED=true ----
if ads_settings.SMART_ADS_ENABLED:
    ads_routes.register(
        app,
        auth=_auth,
        identify=_identify,
        send_file=send_file,
        db=db,
        token_re=TOKEN_RE,
        telegram_errors=(TelegramError,),
        forbidden=(Forbidden,),
        policy=AdsPolicy(
            monetag_postback=(AD_VERIFY_MODE == "postback"),
            min_seconds=MIN_AD_SECONDS,
            wait_seconds=POSTBACK_WAIT_SECONDS,
            block_non_valued=BLOCK_NON_VALUED,
            strict=STRICT_ADS,
        ),
        manager=manager,
        ads_required=ADS_REQUIRED,
        monetag_store=mstore,
        select_zone=engine.select_zone,
        sdk_url=engine.sdk_url,
        country=_country,
        secret=WEBHOOK_SECRET,
        limiter=limiter,
    )

@app.post("/api/ad/start")
async def ad_start(request: Request):
    if (lim := await _limited("legacy_start_ip", _client_ip(request))):
        return lim
    body = await request.json()

    if isinstance(body.get("sid"), str) and body["sid"]:
        s, _application, err = await _auth(body)
        if err:
            return {"ok": False, "error": err}
        if (lim := await _limited("legacy_start_user", s["user_id"], "Too many attempts. Please wait a minute.")):
            return lim
    else:
        application, info = _identify(body.get("init_data", ""))
        if not application:
            return {"ok": False, "error": "Please open this inside Telegram."}
        token = info.get("start_param") or ""
        if not TOKEN_RE.match(token) or not await db.get_file(token):
            return {"ok": False, "error": "This link is invalid."}

        user_id = info["user"]["id"]
        if (lim := await _limited("legacy_start_user", user_id, "Too many attempts. Please wait a minute.")):
            return lim
        # bot user ko message bhej sakta hai ya nahi (pehle check, ad dikhane se pehle)
        try:
            await application.bot.send_chat_action(user_id, "upload_document")
        except Forbidden:
            return {"ok": False, "code": "no_write", "bot": application.bot.username}
        except TelegramError:
            pass

        new_sid = secrets.token_urlsafe(12)
        await db.create_session(
            sid=new_sid,
            token=token,
            user_id=user_id,
            chat_id=user_id,
            bot_id=application.bot_data["bot_id"],
            required=ADS_REQUIRED,
        )
        s = await db.get_session(new_sid)

    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}

    # Har ad show ka apna unique ymid (Monetag docs: "unique ymid per ad show") + zone choice.
    zone = await engine.select_zone()
    ymid = f"{s['sid']}.{secrets.token_hex(4)}"
    await db.start_ad(s["sid"], ymid, zone)
    try:
        await mstore.record_attempt(ymid, s["sid"], zone, _country(request), user_id=s["user_id"])
    except Exception:  # noqa: BLE001 — analytics kabhi delivery na roke
        logging.getLogger("monetag").exception("record_attempt failed")
    return {
        "ok": True,
        "sid": s["sid"],
        "ymid": ymid,
        "zone": zone,
        "sdk": engine.sdk_url(zone),
        "ads_done": s["ads_done"],
        "ads_required": s["ads_required"],
    }

@app.post("/api/ad/fail")
async def ad_fail(request: Request):
    """Client SDK error report (stats + auto-bench). Reward par koi asar nahi."""
    if await _limited("legacy_fail_ip", _client_ip(request)):
        return {"ok": False}
    body = await request.json()
    s, _application, err = await _auth(body)
    if err:
        return {"ok": False}
    if await _limited("legacy_fail_user", s["user_id"]):
        return {"ok": False}
    ymid = body.get("ymid")
    if isinstance(ymid, str) and ymid == s.get("cur_ymid"):
        try:
            await mstore.record_fail(ymid, s["sid"], str(body.get("reason") or "error")[:20])
        except Exception:  # noqa: BLE001
            logging.getLogger("monetag").exception("record_fail failed")
    return {"ok": True}

async def _deliver(s2, application):
    # per-user hourly cap on files actually sent; checked BEFORE the exactly-once claim, so a refusal leaves nothing to roll back
    if (lim := await _limited("file_delivery_user", s2["user_id"], "Too many files requested. Please try again later.")):
        return lim
    if not await db.claim_delivery(s2["sid"]):
        return {"ok": False, "error": "You already received this file."}

    doc = await db.get_file(s2["token"])
    ok = bool(doc) and await send_file(application.bot, s2["chat_id"], doc, manager)
    if not ok:
        if not doc:                                    # send_file records its own failures; a missing document never reaches it
            observability.METRICS.inc("delivery.failed")
            ads_slog.event("delivery_failed", level="error", reason="file_missing", file=s2["token"])
        await db.unclaim_delivery(s2["sid"])
        return {"ok": False, "error": "The file is unavailable right now. Please retry shortly."}

    # ad verify ho gaya: "Almost there! Verify & Get Video" wala message hata do
    prompt_msg = s2.get("prompt_msg")
    if prompt_msg:
        try:
            await application.bot.delete_message(s2["chat_id"], prompt_msg)
        except TelegramError:
            pass
    return {
        "ok": True,
        "delivered": True,
        "ads_done": s2["ads_done"],
        "ads_required": s2["ads_required"],
    }

@app.post("/api/ad/done")
async def ad_done(request: Request):
    if (lim := await _limited("legacy_done_ip", _client_ip(request))):
        return lim
    body = await request.json()
    s, application, err = await _auth(body)
    if err:
        return {"ok": False, "error": err}
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if (lim := await _limited("legacy_done_user", s["user_id"])):
        return lim

    # Yeh wahi ad show hona chahiye jo is session ke liye abhi start hua tha
    ymid = body.get("ymid") if isinstance(body.get("ymid"), str) else None
    expected = s.get("cur_ymid")
    if (expected or AD_VERIFY_MODE == "postback") and ymid != expected:
        return {"ok": False, "error": "This ad session is outdated. Please try again."}

    started = s.get("ad_started_at")
    if not started:
        return {"ok": False, "error": "The ad didn't start. Please try again."}
    elapsed = time.time() - started
    if elapsed < MIN_AD_SECONDS:
        return {"ok": True, "wait": math.ceil(MIN_AD_SECONDS - elapsed)}

    # ---- server-side confirmation by Monetag postback (AD_VERIFY_MODE=postback) ----
    if AD_VERIFY_MODE == "postback":
        ad = await mstore.get_ad(ymid)
        if (not ad or ad.get("sid") != s["sid"] or ad.get("user_id") != s["user_id"]
                or time.time() - float(ad.get("t") or 0) > POSTBACK_MAX_AGE):
            return {"ok": False, "error": "Verification failed. Please try again."}
        if not ad.get("pb_seen"):
            first = ad.get("done_first_at") or await mstore.mark_done_first(ymid)
            if time.time() - first < POSTBACK_WAIT_SECONDS:
                return {"ok": True, "wait": 2}       # Monetag postback abhi aaya nahi, thoda ruko
            await mstore.record_fail(ymid, s["sid"], "unverified")
            return {"ok": False, "error": "We couldn't confirm the ad view. Please try again."}
        if not ad.get("pb_valued"):
            await mstore.record_fail(ymid, s["sid"], "non_valued")
            return {"ok": False, "error": "That ad didn't count. Please try again."}

    s2 = await db.complete_ad(s["sid"], MIN_AD_SECONDS)
    if not s2:
        return {"ok": False, "error": "Duplicate request."}
    if ymid:
        try:
            await mstore.record_completion(ymid)
        except Exception:  # noqa: BLE001
            logging.getLogger("monetag").exception("record_completion failed")
    if s2["ads_done"] < s2["ads_required"]:
        return {
            "ok": True,
            "delivered": False,
            "ads_done": s2["ads_done"],
            "ads_required": s2["ads_required"],
        }

    return await _deliver(s2, application)

@app.post("/api/ad/grace")
async def ad_grace(request: Request):
    """Monetag ke paas ad hi nahi (4+ 'Empty feed') => file bina ad ke. Server khud empty_feed reports ginta hai."""
    if not NOFILL_GRACE or ads_settings.SMART_ADS_ENABLED:
        return {"ok": False, "error": "Not available."}
    if (lim := await _limited("legacy_grace_ip", _client_ip(request))):
        return lim
    body = await request.json()
    s, application, err = await _auth(body)
    if err:
        return {"ok": False, "error": err}
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if (lim := await _limited("legacy_grace_user", s["user_id"])):
        return lim
    if await mstore.count_empty_fails(s["sid"]) < NOFILL_GRACE_FAILS:
        return {"ok": False, "error": "No ad confirmation yet."}
    if not await db.grant_grace(s["sid"]):
        return {"ok": False, "error": "Duplicate request."}
    logging.getLogger("monetag").info("nofill grace delivery sid=%s", s["sid"])
    s2 = await db.get_session(s["sid"])
    return await _deliver(s2, application)


# ---------------- Monetag postback (server-to-server) ----------------
# Mounted only when a secret exists. Production cannot start without one (config.py), so there it is always mounted.
# Failed-secret attempts per IP (shared across instances, fails CLOSED if the store is down). A CORRECT secret is never limited.
_postback_fail_limiter = limiter.bind("postback_fail_ip")

if MONETAG_POSTBACK_SECRET:

    @app.get("/api/monetag/postback")
    async def monetag_postback(request: Request):
        try:
            status, body = await ads_postback.handle(
                dict(request.query_params), secret=MONETAG_POSTBACK_SECRET, store=mstore,
                ip=_client_ip(request), fail_limiter=_postback_fail_limiter,
                max_age=POSTBACK_MAX_AGE, session_ttl=SESSION_TTL, require_telegram_id=POSTBACK_REQUIRE_TELEGRAM_ID)
        except Exception:  # noqa: BLE001
            observability.METRICS.inc("postback.error")
            logging.getLogger("monetag").exception("postback store failed")
            raise HTTPException(status_code=500)   # Monetag baad me retry karega
        return PlainTextResponse(body, status_code=status, headers={"Retry-After": "60"} if status == 429 else None)

# ---------------- admin analytics ----------------
def _admin_ok(authorization: str | None) -> bool:
    if not MONETAG_ADMIN_TOKEN:
        return False
    got = (authorization or "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(got, MONETAG_ADMIN_TOKEN)

async def _admin_guard(request: Request, authorization: str | None):
    """404 without a configured token; per-IP flood limit; failed-token counter (fails closed). Same policy as /admin/ads."""
    if not MONETAG_ADMIN_TOKEN:
        raise HTTPException(status_code=404)
    ip = _client_ip(request)
    if not await limiter.allow("monetag_admin_ip", ip):
        raise HTTPException(status_code=429)
    if _admin_ok(authorization):
        return
    if not await limiter.allow("monetag_admin_fail_ip", ip):
        raise HTTPException(status_code=429)
    raise HTTPException(status_code=403)

@app.get("/admin/monetag")
async def admin_monetag(request: Request, days: int = MONETAG_WINDOW_DAYS, authorization: str | None = Header(default=None)):
    await _admin_guard(request, authorization)
    return await mstore.build_report(max(1, min(days, 90)))

@app.post("/admin/monetag/confirmed")
async def admin_monetag_confirmed(request: Request, authorization: str | None = Header(default=None)):
    """Monetag dashboard se dekha hua asli revenue yahan likho: {"day":"2026-11-03","revenue_usd":12.34}"""
    await _admin_guard(request, authorization)
    b = await request.json()
    day, zone = str(b.get("day", "")), str(b.get("zone", "all"))
    try:
        rev = float(b.get("revenue_usd"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="revenue_usd must be a number")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or rev < 0 or not (zone == "all" or zone in MONETAG_ZONES):
        raise HTTPException(status_code=400, detail="bad day/zone/revenue")
    await mstore.set_confirmed(day, rev, zone)
    return {"ok": True}


# ---------------- diagnostics (secret-free, user-free) ----------------
def _diag_ok(authorization: str | None) -> bool:
    got = (authorization or "").removeprefix("Bearer ").strip().encode()
    return any(t and hmac.compare_digest(got, t.encode()) for t in (ads_settings.ADS_ADMIN_TOKEN, MONETAG_ADMIN_TOKEN))


@app.get("/admin/health")
async def admin_health(request: Request, authorization: str | None = Header(default=None)):
    """Operational snapshot (Bearer ADS_ADMIN_TOKEN or MONETAG_ADMIN_TOKEN; 404 when neither is set). State, counters and dependency status
    only: no secrets, no user identifiers, no connection strings, no error messages (class names only)."""
    if not (ads_settings.ADS_ADMIN_TOKEN or MONETAG_ADMIN_TOKEN):
        raise HTTPException(status_code=404)
    ip = _client_ip(request)
    if not await limiter.allow("monetag_admin_ip", ip):
        raise HTTPException(status_code=429)
    if not _diag_ok(authorization):
        if not await limiter.allow("monetag_admin_fail_ip", ip):
            raise HTTPException(status_code=429)
        raise HTTPException(status_code=403)
    ok, ready_body = await readiness.check()
    return JSONResponse({
        "service": {**health.LIFECYCLE.snapshot(), "ready": ok, "checks": ready_body["checks"],
                    "build": (os.getenv("KOYEB_GIT_SHA") or os.getenv("GIT_SHA") or "")[:12] or None},
        "profile": {"app_env": APP_ENV, "strict_ads": STRICT_ADS, "verify_mode": AD_VERIFY_MODE,
                    "smart_ads": ads_settings.SMART_ADS_ENABLED, "log_format": RT.log_format},
        "dependencies": {"mongodb": manager.db_dep.snapshot(), "telegram_webhook": {**manager.webhook_dep.snapshot(), **manager.webhook_info},
                         "ratelimit_store": {"degraded": limiter.degraded, **{k: limiter.stats[k] for k in
                                             ("store_errors", "degraded_decisions", "last_error")}}},
        "background_tasks": manager.tasks.snapshot(),
        **observability.diagnostics(),
        "configured": {"monetag_postback": bool(MONETAG_POSTBACK_SECRET), "ads_admin": bool(ads_settings.ADS_ADMIN_TOKEN),
                       "monetag_admin": bool(MONETAG_ADMIN_TOKEN), "tads_widget": bool(ads_settings.TADS_WIDGET_ID)},
    }, headers={"Cache-Control": "no-store"})
