# Koyeb deploy: ONE service

One bot (`MAIN_BOT_TOKEN`) + the Mini App run in one service. **Nothing here has been deployed or tested on a real Koyeb account by the
author of this batch; follow "First deployment checklist" and verify each step yourself.**

## Recommended Koyeb configuration
| Setting | Value | Why |
|---|---|---|
| Builder | **Dockerfile** | `python serve.py` as PID 1 (exec form) receives SIGTERM => graceful shutdown |
| Service type | **Web service** | public HTTPS endpoint (Telegram webhook, Mini App, Monetag postback) |
| Exposed port | **8000**, protocol HTTP, path `/` | Koyeb injects `PORT` with the port you configure; the app binds `0.0.0.0:$PORT`. Keep the two equal |
| Instances | **1** to start | several work (rate limits + migration lock are MongoDB-backed) but verify `TRUSTED_PROXY_COUNT` first |
| Health check | **HTTP**, port 8000, path **`/readyz`**, method GET | readiness: 503 until MongoDB is reachable and the schema is verified, so traffic only reaches a fully started instance |
| Health check timing | grace period **60 s**, interval **15 s**, timeout **5 s**, restart limit **3** | a boot runs migrations + Telegram init (typically < 30 s); do not set the grace period below 30 s |
| Env `BASE_URL` | `https://<app>-<org>.koyeb.app` (**https, no trailing slash**) | used for the Telegram webhook URL and the Mini App links |
| Env `APP_ENV` | `production` (or leave unset) | fail-closed ad verification |
| Secrets | add as **Koyeb Secrets** and reference them from env | never in the repo, the Dockerfile or build args |

### Health checks: ONE rule (this replaces every earlier, contradictory instruction)
* `GET /` = **liveness**. Always `200 ok` while the process answers. Touches neither MongoDB nor Telegram. Use it for "is the process alive"
  checks only (the Dockerfile `HEALTHCHECK` uses it). **Do not** point a Koyeb restart-on-failure check at it expecting database coverage.
* `GET /readyz` = **readiness**. `200 {"status":"ready"}` only when startup finished (migrations verified, Telegram initialised) **and** MongoDB
  answers **and** every migration is recorded and verified; otherwise `503 {"status":"not_ready","checks":{...}}` naming the failing check
  (`startup` / `database` / `migrations`), never secrets or error text. Cached 3 s, so probing it often is cheap.
* **Koyeb health check path = `/readyz`.** Caveat: a MongoDB outage then makes the instance unhealthy, and with a restart limit Koyeb may
  restart it repeatedly although a restart cannot fix Atlas. That is why the recommended restart limit is small. If you prefer the process never be
  restarted for a database outage, use `/` as the Koyeb check and watch `/readyz` + the owner alerts instead. Both are valid; pick one and do
  not mix them up.
* While the app is starting, has FAILED (e.g. a failed migration) or is shutting down, every other route answers `503` + `Retry-After`, so a
  half-started instance never serves users. `/readyz` stays 503 and the process exits non-zero after a fatal startup failure.
* `GET /admin/health` (Bearer `ADS_ADMIN_TOKEN`/`MONETAG_ADMIN_TOKEN`): lifecycle, dependency status (MongoDB, Telegram webhook, rate-limit
  store), background tasks and counters. No secrets, no user identifiers.

## First deployment checklist (do NOT skip; none of it was verified live)
1. Create the Telegram bot, owner id, two private channels (bot admin in both), MongoDB Atlas cluster (network access must allow Koyeb's egress).
2. Create the Koyeb secrets/env from the **Production environment checklist** in README.md. `BASE_URL` must already be the final https URL
   (choose the app name first; the URL is `https://<app>-<org>.koyeb.app`).
3. Deploy once. Watch the logs for, in order: `app_starting` -> `database schema ready` -> `webhook_registered` -> `startup_complete`.
   `/readyz` must return 200. If it returns 503, read the `checks` field and the `startup_failed` log line.
4. Set the Monetag postback URL (Step 1 below) **before** sending traffic; without it nobody receives files in production.
5. Send yourself a link: open it, watch an ad, confirm the file arrives. Check `/admin/health` and the log `postback_accepted` counter.
6. Check that different users show different `h:` ids in the logs (`TRUSTED_PROXY_COUNT` is right). Same id for everyone = wrong value.
7. Redeploy once and confirm in the logs: `shutdown_started` -> `shutdown_complete`, and that the new instance becomes ready (safe restarts).

## Safe restarts / deploys
* Koyeb sends SIGTERM when it stops an instance. `serve.py` stops accepting connections, gives in-flight requests `HTTP_GRACEFUL_TIMEOUT`
  (10 s), then stops background tasks, lets queued Telegram updates finish, stops the bot, and closes MongoDB within `SHUTDOWN_TIMEOUT` (15 s).
  The total must stay below Koyeb's termination grace period; **I could not verify Koyeb's exact value**. Check it in your service settings
  and lower the two variables if it is shorter than ~25 s.
* The Telegram webhook is **not** deleted on shutdown: updates sent while no instance is ready get `503`, and Telegram retries them.
* Rolling deploy: the new instance only receives traffic after `/readyz` = 200, so old and new overlap safely. Migrations run under a MongoDB
  lock (one instance migrates, the others wait up to `MIGRATION_WAIT_TIMEOUT`), they only add/verify indexes and never change documents.
* Background loops are started exactly once per process (task supervisor). Two instances running at the same time is already safe: the
  optimiser and the migrations take MongoDB leases.
* Release-step migrations: set `MIGRATE_ON_STARTUP=false` and run `python -m migrator` as a Koyeb job/pre-deploy command; the app then only
  verifies and waits for the schema.

## Docker image hygiene
`.dockerignore` excludes `.env*`, tests, docs and VCS data; the Dockerfile contains no `ENV`/`ARG` for credentials and never COPYs an env file; the
container runs as an unprivileged user. **Never** pass secrets as build args: they stay in the image layers. Verify with
`docker history --no-trunc <image> | grep -iE "token|secret|mongodb"` (must print nothing) and `docker run --rm <image> env` (no secrets,
only the defaults `PORT`, `PYTHONUNBUFFERED`, ...). The image cannot start without them being supplied at runtime.

## Logs and alerts
See README "Monitoring". Logs are one JSON object per line on stdout (`ts`, `severity`, `logger`, `event`, ...); filter in the Koyeb log view
with e.g. `"severity":"ERROR"` or `"event":"startup_failed"`. Owner alerts go to the owner chat in Telegram, de-duplicated and capped.

---

# Monetag Smart Monetization Engine

Pehle jaisa sab chalta rahega. Naya kuch bhi tab tak active nahi jab tak aap env set na karo.

## Kya Monetag officially support karta hai (docs.monetag.com se verify kiya)
- **Rewarded Interstitial** — `show_<ZONE>({ymid, requestVar})`, ad band hone par promise resolve. **Yahi gate hai.**
- **Rewarded Popup** (`type:'pop'`) — naye tab me advertiser page kholta hai, ~2s baad confirm. User ko ad "dekhna" nahi padta => reward semantics alag => **gate ke liye implement nahi kiya.**
- **In-App Interstitial** (`type:'inApp'`) — apne aap interval par dikhta hai, per-show promise nahi (`undefined`) => reward gate nahi ban sakta => **implement nahi kiya.**
- Postback macros: `ymid, zone_id, sub_zone_id, request_var, telegram_id, event_type, reward_event_type, estimated_price`. **Country/geo macro nahi hai.**
- "One Mini App = one SDK main zone = one postback setup." Main zone ke andar kaunsa sub-zone/advertiser chalega, ye **Monetag decide karta hai**, aap nahi.
- Koi documented publisher reporting API nahi mila => **confirmed revenue automatically fetch nahi hota** (neeche manual entry).

## Step 1 — Postback (analytics + secure verification ki neev)
1. Koyeb env: `MONETAG_POSTBACK_SECRET=<lamba random string>` (sirf letters/digits).
2. Monetag SSP -> apna **main zone** -> *Postback URL*:
```
https://<BASE_URL>/api/monetag/postback?secret=<MONETAG_POSTBACK_SECRET>&ymid={ymid}&event_type={event_type}&reward_event_type={reward_event_type}&zone_id={zone_id}&sub_zone_id={sub_zone_id}&estimated_price={estimated_price}&request_var={request_var}&telegram_id={telegram_id}
```
3. Har extra main zone me bhi yahi URL daalo.
4. 2-3 ads khud dekho. Owner bot -> **💰 Monetag**: impressions aane chahiye. Ya `GET /admin/monetag` (token ke saath).

## Step 2 — Server-side reward verification (MANDATORY in production)
`APP_ENV` default = `production`: `AD_VERIFY_MODE=postback` is the only accepted mode and `MONETAG_POSTBACK_SECRET` is required (16-128 chars: `A-Z a-z 0-9 . _ ~ -`). Without them the app **refuses to start** (fail closed).
File tabhi milegi jab is ad ke `ymid` ka Monetag `valued` impression postback server par aa chuka ho. The postback is accepted only if: secret matches (constant time), `ymid` was issued by us, `telegram_id` == the session's user, `zone_id` == the ad's zone, session + ad are not expired (`AD_ATTEMPT_MAX_AGE`, `SESSION_TTL`) and the ad is not already settled. Exact repeats are idempotent (`dup`); stale/forged ones get 4xx (reason only in server logs).
**Postback URL galat/missing ho to koi file nahi milegi** — deploy karne se PEHLE Monetag me postback URL set karo (Step 1) aur test karo.
`APP_ENV=development` (test deployment only) allows the old `legacy` mode (SDK promise + `MIN_AD_SECONDS`, forgeable from the browser).
Note: `telegram_id` macro must be in the postback URL (it is, in Step 1). If Monetag leaves it empty for your zone, set `MONETAG_POSTBACK_REQUIRE_TELEGRAM_ID=false` (the ymid/session/user binding on our side still applies).

## Step 3 — Multi-zone + auto optimization
```
MONETAG_ZONES=12181474,<second main zone>[|<its sdk url>]
MONETAG_DEFAULT_ZONE=12181474
MONETAG_OPTIMIZE=true
```
Dhyan: extra zone Monetag dashboard me **main SDK zone** hona chahiye. Docs ye confirm nahi karte ki ek hi Mini App URL par 2 main zones ban sakte hain; agar nahi bane to optimizer ka koi matlab nahi (sirf 1 zone). Ek zone ho to bhi analytics poora kaam karta hai.

### Optimizer kaise kaam karta hai
- Metric: `eCPM = sum(estimated_price) / impressions * 1000`, rolling `MONETAG_WINDOW_DAYS` din. Yeh **historical measured** hai; Monetag live CPM nahi deta.
- Warm-up: jab tak har zone par `MONETAG_MIN_IMPRESSIONS` impressions nahi => default zone ~80%, baaki `MONETAG_EXPLORE_SHARE` (20%) me barabar.
- Compare: small-sample se bachne ke liye scores pooled average ki taraf shrink hote hain; leader tabhi badalta hai jab challenger noise band (`MONETAG_Z_SCORE` x standard error) se zyada behtar ho **aur** pichhle switch ko `MONETAG_MIN_SWITCH_HOURS` ho chuke ho.
- Traffic: leader >= 50%; baaki zones ko `MONETAG_MIN_SHARE` floor; saaf taur par kharab zone ka exploration aadha.
- Auto-bench: kisi zone ke ads `MONETAG_BENCH_FAIL_RATE` se zyada fail (Empty feed/timeout/error, min `MONETAG_BENCH_MIN_ATTEMPTS`) => `MONETAG_BENCH_MINUTES` ke liye band. Sab fail ho to default zone.
- Har `MONETAG_OPT_INTERVAL` sec me ek round; multi-instance me lease se sirf ek chalta hai. Hot path me 30s cache, DB query nahi.
- 1k-5k/day par ~300 impressions/zone me kuch ghante-din lagte hain; chhota eCPM farq statistically detect nahi hoga — yeh jaan-boojh kar conservative hai.

## Environment variables
| Var | Default | Matlab |
|---|---|---|
| `MONETAG_ZONES` | khali | Main SDK zones `zone[\|sdk_url],...` |
| `MONETAG_DEFAULT_ZONE` | `MONETAG_ZONE_ID` | Fallback/warm-up zone |
| `MONETAG_OPTIMIZE` | `false` | Optimizer on/off |
| `APP_ENV` | `production` | `production` (strict, fail closed) / `development` / `test` (local only) |
| `MONETAG_POSTBACK_SECRET` | **required in production** | Postback secret, 16-128 chars `A-Za-z0-9._~-` |
| `AD_VERIFY_MODE` | `postback` (prod) / `legacy` (dev) | production accepts only `postback` |
| `AD_ATTEMPT_MAX_AGE` | `600` | Seconds an ad attempt / its postback stays valid (30..3600) |
| `MONETAG_POSTBACK_REQUIRE_TELEGRAM_ID` | `true` | Postback must carry `telegram_id` == session user |
| `POSTBACK_WAIT_SECONDS` | `20` | Postback ka intezaar |
| `BLOCK_NON_VALUED` | `true` | SDK `non_valued` bataye to file nahi |
| `MONETAG_MIN_IMPRESSIONS` | `300` | Compare se pehle min impressions/zone |
| `MONETAG_WINDOW_DAYS` | `7` | Rolling window |
| `MONETAG_EXPLORE_SHARE` | `0.2` | Non-leader zones ko kul traffic (max 0.5) |
| `MONETAG_MIN_SHARE` | `0.05` | Har zone ka floor |
| `MONETAG_MIN_SWITCH_HOURS` | `24` | 2 switches ke beech |
| `MONETAG_Z_SCORE` | `1.28` | Significance (~90%) |
| `MONETAG_OPT_INTERVAL` | `900` | Optimizer period (sec) |
| `MONETAG_BENCH_FAIL_RATE` / `_MIN_ATTEMPTS` / `_MINUTES` | `0.6` / `20` / `30` | Auto-bench |
| `MONETAG_ADMIN_TOKEN` | khali | `/admin/monetag` Bearer token |
| `MONETAG_GEO_HEADER` | khali | Proxy ka country header (country-wise eCPM) |
| `SMART_ADS_ENABLED` | `true` | Monetag + Tads.me gate (false = purana Monetag-only flow) |
| `TADS_WIDGET_ID` | khali | Tads.me fullscreen widget ID (set karte hi Tads fallback ban jata hai) |
| `TADS_ALLOW_CLIENT_REWARD` | `false` | Development only. Ignored in production: Tads has no server-side verification, so it never unlocks a file |
| `ADS_ADMIN_TOKEN` | khali | `/admin/ads` dashboard token |
| `GATE_ROUNDS` | `3` | Monetag->Tads ke kitne round, phir ad-blocker check |
| `GATE_GRACE` | `true` | 3 round me koi ad nahi + blocker nahi => file de do (false = bina ad kabhi file nahi) |
| `GATE_MIN_TOTAL_SECONDS` / `GATE_CHALLENGE_TTL` / `GATE_GRACE_MAX_PER_HOUR` | `8` / `90` / `20` | Gate tuning |
| `GATE_PROBE_TADS` | `true` | Tads host unreachable = blocker (region me Tads block ho to `false`) |

Secrets (`MONETAG_POSTBACK_SECRET`, `MONETAG_ADMIN_TOKEN`) frontend me kabhi nahi jaate; access log me `secret=***` ban kar chhapte hain.

## Analytics
- Owner bot -> **💰 Monetag**
- `curl -H "Authorization: Bearer $MONETAG_ADMIN_TOKEN" https://<BASE_URL>/admin/monetag?days=7`
- **Estimated** = postback `estimated_price` ka sum (andaza). **Confirmed** = sirf wo jo aap dashboard dekh kar dalo:
```
curl -X POST -H "Authorization: Bearer $MONETAG_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"day":"2026-11-03","revenue_usd":12.34}' https://<BASE_URL>/admin/monetag/confirmed
```
- Country-wise eCPM: Monetag postback me country nahi aata. Sirf tab milega jab aapka proxy country header bheje aur `MONETAG_GEO_HEADER` set ho (Koyeb ke default header ki guarantee nahi — pehle check karo).
- Fill rate sirf SDK ke `Empty feed` reports se maapa jata hai (client-reported).

## Testing
1. `python -m unittest discover -s tests -v`
2. Local/test deployment only: `APP_ENV=development`, `MONETAG_OPTIMIZE=false` (legacy flow): link kholo, ad dekho, file aani chahiye. Production me yeh mode band hai.
3. Postback set karo, ad dekho, 💰 Monetag me impression + estimated revenue check karo.
4. Production (`APP_ENV=production`, postback mode): ad dekho => file kuch second baad aani chahiye. Ad turant band karke dobara try karo, logs me `non_valued`/unverified dekho.
5. Extra zone daalo, `MONETAG_OPTIMIZE=true`: `MONETAG_ZONES` me dono, kuch ghante baad 💰 Monetag me traffic split dikhega.


## Batch 2 / 3 deploy notes
* Set `TRUSTED_PROXY_COUNT` (default 1) and confirm in the logs that different users show different `h:` ids; see README "Rate limiting".
* Health checks: see "Health checks: ONE rule" at the top of this file.
* Migrations run at startup under a MongoDB lock. With several instances only one migrates, the others wait up to `MIGRATION_WAIT_TIMEOUT`.
  A failed migration stops the app (log event `startup_failed`, phase `migrations`; `/readyz` stays 503; process exits 1; owner alert sent).
