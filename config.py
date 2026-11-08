import os

import envcheck
from ads import security as _security

# Every missing / malformed required variable is reported at once (never with its value). Production also requires an https BASE_URL
# and a WEBHOOK_SECRET of at least 16 characters (the Telegram webhook secret header is derived from it).
envcheck.validate_required(os.environ)
# Operational knobs (logging, alerting, MongoDB timeouts, startup/shutdown budgets); invalid values stop the process.
RT = envcheck.parse_runtime(os.environ)


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


MAIN_BOT_TOKEN = _req("MAIN_BOT_TOKEN")
OWNER_ID = int(_req("OWNER_ID"))
MONGO_URI = _req("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "filebot")
WEBHOOK_SECRET = _req("WEBHOOK_SECRET")

BASE_URL = _req("BASE_URL").rstrip("/")
LINK_BASE_URL = os.getenv("LINK_BASE_URL", BASE_URL).rstrip("/")

PRIMARY_CHANNEL = int(_req("PRIMARY_CHANNEL"))
BACKUP_CHANNEL = int(_req("BACKUP_CHANNEL"))


# ---- optional buttons on the bot menu ----
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()

# ---- ad verification ----
VERIFY_REQUIRED = _bool("VERIFY_REQUIRED", True)
ADS_REQUIRED = int(os.getenv("ADS_REQUIRED", "1"))
MIN_AD_SECONDS = int(os.getenv("MIN_AD_SECONDS", "5"))
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))

# Delivered file itne second baad user ki chat se auto-delete (0 = band)
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "120"))

# ---- direct Mini App launch ----
# False (default) = link -> bot -> 'Verify & Get Video' button -> Mini App me turant ad.
# True = link seedha Mini App kholta hai (button nahi).
# Iske liye har delivery bot me BotFather -> Bot Settings -> Main Mini App set hona chahiye
# (URL: BASE_URL/app). Agar short name wala app bana hai (/newapp) to uska short name yahan daalo.
DIRECT_MINIAPP = _bool("DIRECT_MINIAPP", False)
MINIAPP_SHORT_NAME = os.getenv("MINIAPP_SHORT_NAME", "").strip()

# ---- Monetag (Rewarded Interstitial, Telegram Mini App SDK) ----
# Monetag dashboard -> Telegram Mini Apps -> apna zone ID
MONETAG_ZONE_ID = os.getenv("MONETAG_ZONE_ID", "12181474").strip()
MONETAG_SDK_URL = os.getenv("MONETAG_SDK_URL", "https://libtl.com/sdk.js").strip()
# True = Mini App me chhota event log + asli error dikhao (sirf test ke liye)
MONETAG_SHOW_LOG = _bool("MONETAG_SHOW_LOG", False)

# ---- Create Post (manager bot) ----
# Post ka default caption (image ke niche). \n se nayi line. Owner photo ke saath caption likhe to wahi use hota hai.
POST_CAPTION = os.getenv(
    "POST_CAPTION",
    "🎬 Watch this video for FREE!\n\n▶️ Tap the button below to play instantly",
).replace("\\n", "\n")
POST_BUTTON_TEXT = os.getenv("POST_BUTTON_TEXT", "👉 Play Online Free").strip()[:60] or "Play"
# Optional: channel jaha bot post seedha daal de (bot ko us channel ka admin banao). Khali = band.
_pc = os.getenv("POST_CHANNEL", "").strip()
POST_CHANNEL = (int(_pc) if _pc.lstrip("-").isdigit() else _pc) if _pc else None
POST_MAX_VIDEOS = int(os.getenv("POST_MAX_VIDEOS", "10"))
# "Aaj" ka din kis timezone me ginna hai (minutes, UTC se). India = 330
STATS_TZ_OFFSET_MIN = int(os.getenv("STATS_TZ_OFFSET_MIN", "330"))

# True = post ka button seedha t.me/<bot> link kholta hai (Telegram ka "Open Link" confirm box nahi aata).
# Har post par random active bot chuna jata hai. False = purana /f/ redirect link (confirm box aayega).
POST_DIRECT_LINK = _bool("POST_DIRECT_LINK", True)


# ======================================================================
# ---- Monetag Smart Monetization Engine (sab kuch OPTIONAL / backward-compatible) ----
# Default values = purana behaviour. Kuch bhi set na karo to app pehle jaisa hi chalta hai.
# ======================================================================
def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def _parse_zones(raw: str) -> dict:
    """'12181474,98765|https://x.com/sdk.js' -> {'12181474': <MONETAG_SDK_URL>, '98765': 'https://x.com/sdk.js'}"""
    out: dict = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        zone, _, url = part.partition("|")
        zone = zone.strip()
        if not zone.isdigit():
            raise RuntimeError(f"MONETAG_ZONES: '{zone}' is not a numeric zone ID")
        out[zone] = url.strip() or MONETAG_SDK_URL
    return out


# Sirf MAIN SDK zone IDs (Rewarded Interstitial). Format: zone[|sdk_url],zone[|sdk_url]
# Khali = sirf MONETAG_ZONE_ID. Sub-zones yahan mat daalo (Monetag docs: SDK me sirf main zone).
MONETAG_ZONES = _parse_zones(os.getenv("MONETAG_ZONES", ""))
MONETAG_DEFAULT_ZONE = os.getenv("MONETAG_DEFAULT_ZONE", MONETAG_ZONE_ID).strip() or MONETAG_ZONE_ID
MONETAG_ZONES.setdefault(MONETAG_ZONE_ID, MONETAG_SDK_URL)
MONETAG_ZONES.setdefault(MONETAG_DEFAULT_ZONE, MONETAG_SDK_URL)
if not MONETAG_DEFAULT_ZONE.isdigit():
    raise RuntimeError("MONETAG_DEFAULT_ZONE must be a numeric zone ID")

# True = historical eCPM ke hisaab se traffic zones me baantna. False (default) = hamesha default zone.
MONETAG_OPTIMIZE = _bool("MONETAG_OPTIMIZE", False)

# ---- server-side verification ----
# Monetag SSP -> main zone -> Postback URL:
#   https://<BASE_URL>/api/monetag/postback?secret=<MONETAG_POSTBACK_SECRET>&ymid={ymid}&event_type={event_type}&reward_event_type={reward_event_type}&zone_id={zone_id}&sub_zone_id={sub_zone_id}&estimated_price={estimated_price}&request_var={request_var}&telegram_id={telegram_id}
# ---- security profile (fail closed) ----
# APP_ENV defaults to "production": Monetag postback verification is then MANDATORY, MONETAG_POSTBACK_SECRET is REQUIRED and
# browser-only rewards (Tads.me) can never unlock a file. A missing/weak secret or AD_VERIFY_MODE=legacy stops the process at
# startup (see ads/security.py). Use APP_ENV=development only on a test deployment.
_SEC = _security.resolve(os.environ)
APP_ENV = _SEC.app_env
STRICT_ADS = _SEC.strict
SECURITY_WARNINGS = _SEC.warnings
# legacy   = development only: SDK promise resolve + MIN_AD_SECONDS (forgeable from the browser).
# postback = file only after Monetag's server-to-server 'valued' impression postback for this ad's ymid has arrived.
AD_VERIFY_MODE = _SEC.verify_mode
MONETAG_POSTBACK_SECRET = _SEC.postback_secret
POSTBACK_MAX_AGE = _SEC.postback_max_age                 # seconds after the ad was issued during which a postback is accepted
POSTBACK_REQUIRE_TELEGRAM_ID = _SEC.require_telegram_id
POSTBACK_WAIT_SECONDS = _int("POSTBACK_WAIT_SECONDS", 20)
# SDK ne reward_event_type='non_valued' diya (unpaid/fraud/fallback) to file mat do, dobara ad dikhao
BLOCK_NON_VALUED = _bool("BLOCK_NON_VALUED", True)
# True (default) = ad pehle background me preload, user "Watch ad & unlock" dabaye tab turant chale. False = preload ke baad ad khud chale.
AD_CLICK_TO_PLAY = _bool("AD_CLICK_TO_PLAY", True)
# True (default) = Monetag me ad hi nahi hai (4+ baar "Empty feed") aur user ka AdBlock/DNS off hai => file bina ad ke de do.
# AdBlock/DNS wale user ko yeh kabhi nahi milta. Koi time limit nahi.
# The legacy no-fill grace trusts CLIENT-reported "empty feed" events, so it is never enabled in the production profile (a forged client
# could get the file without an ad). Development/test keep it. The smart flow's server-verified gate (GATE_GRACE) is separate.
NOFILL_GRACE = _bool("NOFILL_GRACE", True) and not STRICT_ADS
NOFILL_GRACE_FAILS = max(2, _int("NOFILL_GRACE_FAILS", 4))   # itne "empty feed" reports ke baad hi (client MAX_TRIES=4 ke saath same rakho)

# ---- optimizer tuning (conservative defaults for 1k-5k impressions/day) ----
MONETAG_MIN_IMPRESSIONS = _int("MONETAG_MIN_IMPRESSIONS", 300)    # har zone ke itne impressions (window me) ke baad hi compare
MONETAG_WINDOW_DAYS = max(1, _int("MONETAG_WINDOW_DAYS", 7))      # rolling window
MONETAG_EXPLORE_SHARE = min(0.5, max(0.0, _float("MONETAG_EXPLORE_SHARE", 0.2)))  # leader ke alawa zones ko kul traffic
MONETAG_MIN_SHARE = min(0.2, max(0.0, _float("MONETAG_MIN_SHARE", 0.05)))         # har live zone ko minimum share
MONETAG_MIN_SWITCH_HOURS = _float("MONETAG_MIN_SWITCH_HOURS", 24)  # 2 leader-switch ke beech kam se kam
MONETAG_Z_SCORE = _float("MONETAG_Z_SCORE", 1.28)                 # ~90% confidence; badhao = aur conservative
MONETAG_OPT_INTERVAL = max(60, _int("MONETAG_OPT_INTERVAL", 900))  # optimizer kitni der baad chale (sec)
# auto-bench: zone ke ads lagatar fail ho rahe ho to thodi der ke liye band
MONETAG_BENCH_FAIL_RATE = _float("MONETAG_BENCH_FAIL_RATE", 0.6)
MONETAG_BENCH_MIN_ATTEMPTS = _int("MONETAG_BENCH_MIN_ATTEMPTS", 20)
MONETAG_BENCH_MINUTES = _int("MONETAG_BENCH_MINUTES", 30)

# ---- analytics access ----
# GET /admin/monetag (Authorization: Bearer <token>). Khali = HTTP endpoint band (bot panel phir bhi chalta hai).
MONETAG_ADMIN_TOKEN = os.getenv("MONETAG_ADMIN_TOKEN", "").strip()
# Optional: proxy ka country header (e.g. CF-IPCountry). Khali = country-wise stats band.
MONETAG_GEO_HEADER = os.getenv("MONETAG_GEO_HEADER", "").strip()
