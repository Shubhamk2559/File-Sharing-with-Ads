# HelpBot — file delivery bot with Smart Ads (Monetag + Tads.me)

Users open a link, watch **one rewarded ad** in the Telegram Mini App, and the file is delivered to their chat.
The smart multi-network layer + **Ad gate** is **ON by default** (`SMART_ADS_ENABLED=true`); set it to `false` for the old Monetag-only flow.

## Ad gate (production policy)
```
user opens file link
  └─ pre-check: ad hosts reachable? cosmetic bait visible?   blocked -> "Ad blocker detected", NO file
  └─ ROUND 1..3  (GATE_ROUNDS):  Monetag  ──no ad──▶  Tads.me
        ad watched to the end ─────────────────────────────────────────────▶ FILE (server-verified)
  └─ all 3 rounds showed NO ad at all
        server-verified ad-blocker challenge
          blocker found / can't be proven clean / blocked SDK ───────────────▶ NO file ("Ad blocker detected")
          clean device ───────────────────────────────────────────────────────▶ FILE (grace, logged in `ad_grace`)
```
* **Monetag is first.** Tads.me is a fallback **only in the development profile**: in production (`APP_ENV=production`, the default) Tads has no server-side verification, so it is never served and can never unlock a file (see "Security profile").
  Change order/mode in 🧠 Smart Ads or `/admin/ads`.
* **The server is the judge, not the browser.** `/api/gate/challenge` issues a one-time challenge; the page must fetch 4 bait resources on *our own origin* under ad-looking URLs
  (`/js/adframe.js`, `/pagead/js/adsbygoogle.js`, `/ads/banner/ad-banner.js`, `/banners/ads/ad-banner-300x250.gif`) - what EasyList/uBlock/AdGuard/Brave rules block.
  The server records each hit and the script baits return an HMAC the page must send back. A blocked/stubbed bait can never produce that, so `/api/smart/grace` refuses.
  The challenge is single-use, bound to session+user, expires after `GATE_CHALLENGE_TTL` and must be issued **after** the last failed ad attempt (no pre-solving while the blocker is off).
* **Grace needs the server's own records**: 3 complete rounds where every attempt was a genuine `no_fill`/`timeout` (never `blocked`/`error`, never an ad that was displayed),
  >= `GATE_MIN_TOTAL_SECONDS` since the first attempt, no attempt still in flight, <= `GATE_GRACE_MAX_PER_HOUR` per user.
* An ad script that loads but never defines its global (blocker returning an empty 200) is classified **blocked**, not "slow". Timeouts/unreachable hosts are never treated as clean.
* Client also checks ad hosts (Monetag SDK host + Tads host), a cosmetic bait element and tampered `fetch`/natives; these only add blockers, never grant anything.
* Rate limits for the gate are separate and generous (many users share one carrier-NAT IP; a refusal would look like a blocker).

**Honest limits.** A blocker-bypass that works from inside Telegram would need to fetch the bait URLs and replay the HMACs by hand (custom code, not a browser extension / DNS filter / VPN). That is not preventable in any browser-side scheme;
mitigations: Telegram `initData` signature, one live ad slot per session, 3-round + time gate, hourly cap, and `AD_VERIFY_MODE=postback` for Monetag (server-verified watches).
Tads rewards are `client_attested` at best (Tads documents no postback) and are refused in production. Bait paths were chosen from common generic filter rules but **not tested against live filter lists here** - verify with uBlock Origin / AdGuard on Telegram Web after deploy,
and adjust `ads/gate.py:BAITS` if one isn't blocked. Users on privacy browsers/DNS that block Tads will see "Ad blocker detected" (set `GATE_PROBE_TADS=false` if that hurts your region).


> Status: unit-tested (Python + JS) and exercised end-to-end in headless Chromium with mocked ad SDKs (watched ad, Monetag->Tads fallback, 3 no-ad rounds + clean device,
> blocked bait/image/hosts/neutered SDK). **Not verified against live Tads / Monetag accounts** (no credentials). Follow "First live test" below before relying on it.

## What was inspected / what is reused
* "Reward" in this project = **file delivery** (there are no balances/referrals to protect). Reward state still lives in your
  existing `sessions` collection (`ads_done`, `delivered`); the smart layer reuses `db.start_ad / complete_ad / claim_delivery`.
* Existing Monetag integration (zone, SDK, `ymid`, postback endpoint, zone optimizer, `AD_VERIFY_MODE`) is reused as-is.
  Monetag attempts made through the smart flow still feed `monetag_*` analytics and its postback verification.
* New files: `ads/` package, `tests/`, `migrations/`, `run_tests.sh`. Edited (additive, gated): `main.py`, `manager.py`,
  `handlers.py`, `miniapp.html`, `_.env.example`.

## Setup per network

### Monetag (already working)
Server postback verification is mandatory in production: set `MONETAG_POSTBACK_SECRET` and the postback URL in Monetag (see `KOYEB.md`); the app will not start without the secret.
Reward trust: `server_postback` in postback mode, otherwise `client_attested` (the existing behaviour, allowed by default).

### Security profile (production is the default)
| | `APP_ENV=production` (default) | `APP_ENV=development` / `test` |
|---|---|---|
| Monetag verification | server postback only, **mandatory** | legacy browser promise allowed |
| `MONETAG_POSTBACK_SECRET` | **required**, 16-128 chars `A-Za-z0-9._~-`, else the app won't start | optional |
| Tads.me | never serves, never unlocks (admin toggles are ignored/rejected) | client-attested fallback (labelled `client_attested`) |
Postback endpoint `GET /api/monetag/postback`: constant-time secret check, per-IP limit on *failed* secrets, payload validation, then `ymid` must be one we issued for the same user (`telegram_id`), zone and a live session; not expired (`AD_ATTEMPT_MAX_AGE`), not already settled; exact repeats are idempotent. Rejections never say why in the response (server log only, never the secret).

### Tads.me
Checked on 2026-11-07 (tads.me/publisher-doc, tads.me/docs, github.com/TADS-me/tads-examples): only browser callbacks `onShowReward` / `onClickReward` / `onAdsNotFound` are documented. **No postback, webhook, signature, API key or reporting API.** The GitBook pages and `llms-full.txt` could not be fetched from this environment; re-check them or ask Tads support before relaxing this.
1. Register at https://tads.me, add your Mini App (Publisher → Sites), create a **fullscreen** widget (Publisher → Widgets), copy the ID → `TADS_WIDGET_ID` (enables Tads automatically).
2. Tads documents only browser callbacks (`onShowReward`, `onClickReward`, `onAdsNotFound`) — no postback, no signature, no reporting API that I could find.
   Therefore Tads rewards are `client_attested`: honoured only in the development profile (`TADS_ALLOW_CLIENT_REWARD=true`), never in production.
   A click is never treated as completion; only `onShowReward`.

### Credentials / IDs you must obtain
| Item | Where | Env / setting |
|---|---|---|
| Monetag zone ID | existing | `MONETAG_ZONE_ID` (already set) |
| Tads widget ID (fullscreen) | tads.me | `TADS_WIDGET_ID` (or dashboard field) |
| Admin token | you invent it | `ADS_ADMIN_TOKEN` |

No Tads API key is needed (no reporting API is documented); the widget ID is a public identifier, secrets stay in env.

## Turning it on
1. `SMART_ADS_ENABLED=true`, `ADS_ADMIN_TOKEN=...`, plus the IDs above. Redeploy (the migration runs on startup; or `python -m ads.migrate`).
2. Owner bot → **🧠 Smart Ads** (toggle networks / mode / fallback) and `https://<BASE_URL>/admin/ads` (analytics, config, revenue entry).
3. Enter each network's real daily revenue from its own dashboard (Confirmed revenue form). This is what makes eCPM comparisons possible.

### First live test (do this before trusting it)
1. Start with `mode=manual` + one network, `TADS_DEBUG=true` on a test platform; confirm the ad shows and the file arrives.
2. Production IDs, watch one ad per network yourself; check **Latest verified rewards** in the dashboard shows the right `trust`.
3. Close/skip an ad and confirm **no file** arrives and the attempt shows as abandoned.

## How smart rotation works
* **Request**: `/api/smart/start` builds an ordered chain of eligible networks (enabled, configured, reward-trust allowed, not cooling down).
* **Modes**: `auto` (performance-based), `priority` (fixed order), `manual` (forced first pick; others remain fallback). Configurable fallback and attempts (1–3).
* **Automatic strategies** (`strategy`):
  * **`thompson` — Max earnings (default).** Goal: maximise `P(verified) × value of one verified impression`.
    * `P(verified)` is a Beta posterior over **time-decayed** evidence (`half_life_days`, default 3), shrunk toward the pooled rate (`prior_strength`) so a few impressions can't swing the ranking.
    * Value per impression, best real source first: **confirmed** (revenue you entered ÷ our verified impressions, shrunk toward your prior) → **your eCPM** (`networks.<n>.ecpm_usd`, labelled "your figure") →
      optional **Monetag postback estimate** (`use_monetag_estimate`, labelled estimate) → **assumed** (borrows the average of known networks, never higher) → none (ranks on conversion only).
    * Thompson sampling explores in proportion to uncertainty: a probably-worse network fades out on its own, a possibly-better one keeps being tested. `min_share` (3%) guarantees nobody is starved.
    * The fallback chain is ordered by expected value, not by static priority.
  * `weighted` — the previous score blend (revenue, fill, completion, errors, recent) with `explore_share`. Kept for people who want it.
* **Circuit breaker**: ≥`fail_min_attempts` attempts with ≥`fail_threshold` hard failures in `cooldown_minutes` ⇒ network benched. Repeat offences **back off ×2, ×4, ×8**. When a cool-down ends the network
  re-enters on **probation** (share capped at `probation_share`) and strikes are forgiven after a clean, busy window. If everything is benched the least-bad still serve.
* **One ad per request**: one atomic "ad slot" per session; a fallback is issued **only if the previous attempt never displayed an ad**. An ad that displayed and then
  failed/was closed never chains another ad; the user retries manually.
* The optimizer runs every `ADS_OPT_INTERVAL`s under a DB lease (safe with multiple instances), stores the model + simulated traffic shares in the `plan` document and expires stuck attempts.

### "Rotation does nothing" — check this first
Tads is only eligible when `TADS_WIDGET_ID` is set and Tads isn't switched off / client-attested rewards aren't disallowed. With one eligible network there is nothing to rotate.
The admin dashboard (**Earnings engine** panel) now lists, per network, exactly why it gets no traffic (`switched off`, `no block/widget ID set`, `reward can't be server-verified…`, `cooling down`) and prints hints.

### Getting the most out of it
1. Enable and configure at least two networks (Networks box).
2. Tell it what each network pays: type your **eCPM** per network, and/or enter **daily revenue** from each network's dashboard (Revenue box). Real data replaces your figure automatically.
3. Leave strategy on *Max earnings*. Expect the first hundreds of requests to be spread out while it learns; the dashboard shows the expected earnings per 1,000 requests and the uplift vs an equal split.

## Reward verification
| Network | Signal | Trust label | Notes |
|---|---|---|---|
| Monetag, `AD_VERIFY_MODE=postback` | `valued` impression postback for this ad's `ymid` | `server_postback` | strongest |
| Monetag, legacy | SDK promise + min seconds | `client_attested` | existing behaviour |
| Tads | `onShowReward` only | `client_attested` | opt-in |

Always enforced: Telegram `initData` signature + session ownership, unique `ad_id` per attempt (also Monetag `ymid`), exactly-once outcome and reward
record (`ad_rewards._id = ad_id`), client can send only `completed`/`non_valued` (revenue/trust/network are ignored), min-seconds, rate limits.
Every reward record stores its trust level so you can audit it.

## Data model (all new, MongoDB)
`ad_config` (settings, plan, health, leases) · `ad_requests` (attempts) · `ad_events` (audit, 30d TTL) · `ad_rewards` (verified rewards, kept) ·
`ad_revenue` (owner-confirmed revenue) · `ad_stats_daily` · `ad_buckets` (10-min rollups, 2d TTL) · `ad_plan_history`.
Indexes/TTLs: `ads/migrate.py` (idempotent; touches no existing collection). Existing sessions get optional `smart_*` fields only.

## Admin
* **Dashboard** `/admin/ads` (Bearer token typed into the page, kept only in sessionStorage; no secret is ever served to the browser):
  KPIs, daily chart, per-network table (status, last success, today/total verified, fill, completion, failures, confirmed vs estimated revenue/eCPM, trust), routing,
  network toggles, optimization settings, confirmed-revenue entry, latest rewards.
* **API** (Bearer `ADS_ADMIN_TOKEN`; 404 if unset; 10 failed attempts / 10 min / IP ⇒ 429): `GET /admin/ads/api/overview?days=`, `GET|POST /admin/ads/api/config`,
  `POST /admin/ads/api/revenue {"day","network","revenue_usd"}`, `GET /admin/ads/api/rewards`.
* **Telegram** 🧠 Smart Ads (owner only).

## Tests — what was actually run
`./run_tests.sh` (tests/ was missing from the zip you sent and has been recreated). Covered: scoring/selection, verification decisions, config validation,
start/fallback/one-ad-per-request, duplicate and concurrent reward claims, wrong user/session, invalid postbacks,
revenue/eCPM math (confirmed vs estimated vs unavailable), admin auth/lockout/validation, HTTP handlers + delivery tail, route registration, panel toggles, migration idempotency,
both browser adapters with mocked SDKs (completion, skip, no-fill, errors, timeouts, click ≠ completion),
max-earnings engine (`tests/test_bandit.py`: time decay, value sources, Thompson picks, per-network floor, probation cap, a closed-loop simulation that must beat an equal split by ≥40%,
back-off ×2 → probation → forgiveness, "why no traffic" diagnostics, earnings report).
**Not run here**: real FastAPI, real MongoDB (an in-memory fake was used, so atomicity of `find_one_and_update` is relied upon, not proven), python-telegram-bot callback glue
(only the pure panel builders are tested), any live ad network.

## Rate limiting (Batch 2)
Shared by every instance through MongoDB (`rate_limits`, TTL-cleaned): one atomic conditional upsert per request, sliding-window counter, denied attempts don't consume budget.
Limits are env-tunable per category: `RL_<NAME>=<max>/<seconds>` (full list + defaults in `_.env.example`; code: `ads/ratelimit.py` `DEFAULTS`). Layers: per IP (coarse, pre-auth),
per authenticated Telegram user (tight), brute-force counters for admin tokens / postback secret / webhook secret (fail closed), per-user hourly file-delivery cap, bot-user limits.
* **MongoDB down:** brute-force counters deny; everything else falls back to a strict per-process window with the same limit (bounded memory). Never "unlimited". Logged as `ratelimit_store_unavailable`; recovers by itself.
* **Client IP:** set `TRUSTED_PROXY_COUNT` to the number of proxies that append to `X-Forwarded-For` (default 1). Wrong value => all users look like one IP (too strict) or the header is spoofable (too lax). Check the `ip=` field of a `postback_rejected` log line after deploying.
* Approximation: sliding-window *counter* (not an exact log): a burst right before a window edge can allow up to ~1.5x the limit over the following half window.

## Migrations (Batch 2)
`migrator.py` + `migrations/registry.py`: ordered, versioned (`schema_migrations`), idempotent, lock/lease protected (`schema_lock`), verified before the app serves anything.
Startup: verify (read-only no-op when current) -> one instance takes the lock and migrates, others wait -> any failure aborts the process. `/readyz` is 503 until startup finished and the schema is verified, and while MongoDB is unreachable (`/` stays a plain liveness check).
Add a schema change by APPENDING a migration to the registry (never edit a released one). Manual / release-step run: `python -m migrator`. `MIGRATE_ON_STARTUP=false` makes the app verify only.
A conflicting existing index (same key, different options) is a hard error and is never dropped automatically; a changed `SESSION_TTL` is applied in place.

## Monitoring, logging and alerts (Batch 3)
**Logs**: one JSON object per line on stdout: `{"ts":"2026-11-08T04:49:12.345Z","severity":"WARNING","logger":"ads","event":"postback_rejected","reason":"bad_secret",...}`.
All loggers (uvicorn, telegram, ours) go through one handler (`observability.py`), so the rules below cannot be bypassed.
* **Secrets never appear**: the exact values of `MAIN_BOT_TOKEN`, `WEBHOOK_SECRET` (and the derived Telegram header secret), `MONETAG_POSTBACK_SECRET`,
  `ADS_ADMIN_TOKEN`, `MONETAG_ADMIN_TOKEN`, `RATE_LIMIT_SALT` and the MongoDB URI/password are registered and replaced by `***`, URL-encoded forms too. Pattern rules
  also mask bot tokens in URLs, `secret=`/`token=`/`hash=`/`init_data=`, `Authorization: Bearer ...` and `user:pass@` in URIs; tracebacks are redacted. httpx stays at WARNING
  (the bot token is in Telegram API URLs). The access log masks query secrets and does not log successful `/` and `/readyz` probes.
* **No private user data**: user/chat ids and IPs are logged as keyed hashes (`h:3f9a...`, stable inside one deployment), file link tokens appear as `file=`; initData, postback `ymid`/`telegram_id` and message text are never logged.
* **No log floods**: identical WARNING+ lines inside `LOG_DEDUP_SECONDS` are collapsed (next line carries `suppressed: N`); the rate-limit store outage logs once per 30 s.
**Events** (grep `"event":"..."`): `app_starting`, `startup_complete`, `startup_failed` (phase, error), `shutdown_started/complete`, `background_task_started/duplicate_ignored`,
`webhook_registered`, `webhook_registration_failed`, `telegram_webhook_error` (Telegram's own last error), `telegram_webhook_rejected`, `mongodb_unavailable/recovered`,
`ratelimit_store_unavailable/recovered`, `postback_accepted/rejected`, `reward_granted/denied`, `ad_fail`, `grace_granted/denied`, `delivery_failed`, `bot_handler_error`, `unhandled_exception`.
**Owner alerts** (Telegram message to `OWNER_ID`, no user data in them): startup failure / failed migration, MongoDB down (+ recovered), rate-limit store degraded (+ recovered), webhook cannot be registered or Telegram reports delivery errors,
a burst of rejected Monetag postbacks (default 20 in 5 min), all delivery locations failed for a file, a background task crashing repeatedly, bursts of bot-handler / HTTP errors.
Each alert key is sent at most once per `ALERT_COOLDOWN` (repeats are counted and reported in the next one), all alerts are capped at `ALERT_MAX_PER_HOUR`, they are queued until the bot exists, and a failing alert never breaks the request.
**Diagnostics**: `GET /admin/health` (Bearer admin token): lifecycle, MongoDB / webhook / rate-limit-store status, background tasks, counters (`postback.accepted/rejected.*`, `delivery.ok/failed`, `webhook.*`, `log.error`, ...). Counters reset on restart.
**Recovery**: MongoDB (Motor reconnects by itself; timeouts are bounded, requests get 503 + Retry-After meanwhile, a background ping reports the outage/recovery once), Telegram startup and `set_webhook` (exponential backoff, flood-wait aware, a webhook monitor re-registers if the URL ever differs),
crashing background loops (restart with backoff), rate limiter (degrades to strict local windows, resumes by itself).
**Limits**: alerts need Telegram to work, so a Telegram outage alerts nobody (see logs / Koyeb). Counters are per process. Not tested against a live Telegram, MongoDB Atlas or Koyeb.

## Limitations / manual steps
* No live publisher verification. Tads SDK usage follows their public docs as of 2026-11-05; the Tads vanilla-JS page was only partly readable (init key `widgetId`,
  script `https://w.tads.me/widget.js` taken from docs + the official `TADS-me/tads-examples` repo). Re-check on first live test.
* Revenue: no documented reporting API for Tads (and none for Monetag) ⇒ revenue/eCPM is **owner-entered** or "unavailable"; nothing is estimated for them.
* Tads cannot be server-verified.
* Monetag has no first-frame callback, so "displayed" is inferred (failure after ≥3 s counts as displayed → no fallback). Tads has no documented close callback: a closed overlay
  ends via a 90 s timeout (abandoned).
* India/device segmentation is **not** used for ranking: none of the networks send country in callbacks; only Monetag zone stats can use `MONETAG_GEO_HEADER`.
* Rate limits are now shared across instances (MongoDB, see "Rate limiting"). One live ad is enforced per session, not across multiple simultaneous sessions of the same user.
* The Monetag default zone SDK tag is still loaded statically (existing behaviour); the Tads SDK loads lazily, only when selected.


## Troubleshooting: `POST /api/smart/start → 422` and "Session expired."
**Cause (fixed):** `ads/routes.py` had `from __future__ import annotations` while FastAPI's `Request` is imported *inside* `register()`.
FastAPI resolves handler annotations from module globals, so `request: Request` stayed an unresolved string and FastAPI treated `request` as a
required **query parameter**. Every call - whatever the body - returned `422 {"detail":[{"type":"missing","loc":["query","request"],"msg":"Field required"}]}`.
The 422 JSON has no `ok`/`error`, so the Mini App fell through to its `|| "Session expired."` fallback. MongoDB TTL, sid and payload were never the problem
(the payload `{sid, init_data}` matches what the backend reads). Fix: the future-import is removed (with a warning comment) and a regression test guards it.

Also added: structured `{"ok":false,"code":...,"error":...}` for validation errors (422), invalid JSON / wrong content-type (400); safe logs (`validation_error`,
`bad_request`, `smart_start` with session lookup result - never initData, tokens or secrets); the Mini App now shows the real category (network, timeout, HTTP status,
server message) and only says "Session expired" when the server says so; 20 s request timeout.

| Symptom | Meaning / action |
|---|---|
| `validation_error` in UI/logs | Read `details[].field`; log line `{"ev":"validation_error",...}` |
| `bad_content_type` / `invalid_json` | Client must send `Content-Type: application/json` and a JSON object |
| "Session expired. Please open the link again." (`code: session`) | Real: sid unknown or older than `SESSION_TTL` (default 30 min) - reopen from the bot |
| "Verification failed" | initData invalid/older than 1 h, or opened by a different Telegram user |
| "No ad provider is available" (`no_network`) | No network is enabled+configured+trusted. Networks are enabled in **/admin/ads** (Tads defaults **off**); Tads is always excluded in production (and in development while `TADS_ALLOW_CLIENT_REWARD=false`) |
| "An ad is already loading" (`busy`) | One live ad per session; clears after `ADS_UNSHOWN_LOCK_SECONDS` |

### API smoke test
```
curl -s -X POST $BASE_URL/api/smart/start -H 'Content-Type: application/json' -d '{}'
  -> {"ok":false,"error":"Please open this inside Telegram."}            (200, understandable, not 422)
curl -s -X POST $BASE_URL/api/smart/start -d 'sid=1'
  -> 400 {"ok":false,"code":"bad_content_type",...}
curl -s -X POST $BASE_URL/api/smart/start -H 'Content-Type: application/json' -d '{oops'
  -> 400 {"ok":false,"code":"invalid_json",...}
```
A fully valid call needs real Telegram `initData` (HMAC-signed with your bot token) - test inside the Mini App.

### Deploy (Koyeb)
Full procedure, recommended settings, health checks and the first-deployment checklist are in **KOYEB.md** (single source of truth).
Short version: Dockerfile builder, port 8000, health check path `/readyz` (readiness; `/` is liveness only), secrets as Koyeb Secrets, then
follow the checklist. Logs must show `startup_complete` and `POST /api/smart/start 200`. **Production deployment has not been verified on a real
Koyeb account by the author of this batch.**
