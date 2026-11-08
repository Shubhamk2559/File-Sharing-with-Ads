# Batch 4 - "No ad provider" + "couldn't confirm the ad view"
* `ads/settings.py`: admin can no longer switch off the LAST usable ad network (in production that is Monetag; Tads.me is never served there) - clear error instead of every user getting "No ad provider is available".
* `ads/service.py`, `ads/postback.py`: if users finish Monetag ads but NO postback has reached the server since startup, a `postback_never_received` warning is logged and ONE owner alert is sent (after 3 cases). `/admin/health` metrics get `postback.last_ok_at` and `ads.unverified`.
* Tests: `tests/test_batch4.py`. Root cause of "couldn't confirm" is the Monetag zone Postback URL (KOYEB.md Step 1), not code.

---
# Batch 3 - Koyeb reliability + monitoring
**Deployment** - new `serve.py` entry point (Dockerfile CMD + Procfile): binds `0.0.0.0:$PORT`, validates the environment first (one message listing every missing/invalid variable, names only), graceful SIGTERM shutdown, non-zero exit on a fatal startup failure. Dockerfile: no credentials, exec-form CMD (python is PID 1), unprivileged user, liveness HEALTHCHECK; `.dockerignore` keeps `.env*`, tests and docs out of the image. `KOYEB.md` / `README.md` health-check instructions were contradictory and are rewritten into one rule: `/` = liveness, `/readyz` = readiness (Koyeb check path), plus recommended settings, first-deployment checklist, safe-restart notes.
**Lifecycle** (`health.py`, `manager.py`) - `start()` is non-blocking and idempotent; migrations -> Telegram init (retry with backoff, bad token fatal) -> `set_webhook` (retry; flaky Telegram does not block serving, a monitor keeps trying and re-registers on URL drift) -> background tasks, each started exactly once by a supervisor (crash => restart with backoff, owner alert on repeated crashes). A failed migration marks the instance FAILED: no Telegram app, no webhook, no loops, every route except `/` and `/readyz` answers 503, owner alert, exit 1. Shutdown is ordered and bounded (tasks -> queued updates -> bot -> MongoDB); the webhook is not deleted.
**MongoDB** - bounded timeouts (5 s server selection), pooled/retryable via Motor, background ping that reports an outage and the recovery once each; requests fail with 503 + Retry-After instead of 500s.
**Monitoring** (`observability.py`) - JSON logs (`ts`, `severity`, `logger`, `event`), all loggers through one redacting handler, configured secrets + pattern redaction (tokens in URLs, `secret=`, Bearer, URI passwords, tracebacks), user/chat ids and IPs as keyed hashes, WARNING+ de-duplication, counters, de-duplicated/capped/queued owner alerts, `/admin/health` diagnostics (no secrets, no user data). Tracked: startup/shutdown, webhook errors (ours and Telegram's), MongoDB failures, migration failures, rate-limit degraded/recovered, Monetag postback accepted/rejected (+ burst alert), ad-gate errors, delivery failures.
**Audit fixes** - the legacy client-attested `NOFILL_GRACE` is now forced off in the production profile (it trusted forged client events); `WEBHOOK_SECRET` must be >= 16 chars and `BASE_URL`/`LINK_BASE_URL` https in production; 20 environment variables the code read were undocumented and are now in `_.env.example`; stray 1-byte `migrations/HzbB` removed; `Dependency` downtime bug at clock 0 fixed.
**Test-fixture changes (not weakened assertions)** - fixtures use a 16+ char `WEBHOOK_SECRET`; the migration-ordering structural test now inspects `_startup` (same guarantee: migrations first, failure fatal); the readiness scenario marks the lifecycle ready; the dev-profile warning is read from stdout (structured logs).
* Tests: `tests/test_batch3.py` (80 cases) + `tests/manager_harness.py` (real `manager.py`, stubbed Telegram/Mongo); `main_harness.py` extended.

---
# Batch 2 - shared rate limiting + reliable migrations
**Rate limiting** (`ads/ratelimit.py`) - MongoDB-backed, shared by every instance, replaces the in-process limiter everywhere.
* One atomic conditional upsert per request (`{_id: category|keyhash|window, n: {$lt: allowed}}` + `$inc`), sliding-window counter (no 2x burst at window edges), denied attempts don't consume budget, TTL index `rate_limits.expire_at`, keys HMAC-hashed.
* 33 categories with env-tunable limits (`RL_<NAME>=max/seconds`, README). New coverage: Telegram bot users (`/start TOKEN`, menus, text), legacy `/api/ad/*`, `/f/<token>`, per-user hourly file-delivery cap, `/admin/monetag`, `/webhook` secret guessing; unchanged Batch 1 numbers are the defaults.
* Store failure never disables limits: brute-force counters fail closed; everything else falls back to a strict bounded local window; timeout + circuit breaker + one ERROR log per 30 s; recovers automatically.
* **Client IP**: only the `X-Forwarded-For` entry added by *your* proxy is trusted (`TRUSTED_PROXY_COUNT`, default 1). Before, the leftmost (client-supplied) entry let anyone dodge every IP limit. IPv6 grouped by /64.
* Postback: a CORRECT secret is still never limited; failed-secret budget is now shared across instances.
**Migrations** (`migrator.py`, `migrations/registry.py`) - versioned (`schema_migrations`), lock/lease (`schema_lock`, heartbeat, expiry takeover), idempotent, verified (drift such as a dropped index is detected and repaired), connection-error retries, hard failure aborts startup (the old code logged and carried on). `/readyz` = DB reachable + schema verified. `python -m migrator` for release steps; `MIGRATE_ON_STARTUP=false` = verify only. New indexes: `rate_limits.expire_at` (TTL), `files.created_at`, `ad_rewards.t`, `monetag_ads.sid`. SESSION_TTL changes are applied in place (collMod). No document is read, changed or deleted.
* Tests: `tests/test_ratelimit_shared.py`, `tests/test_migrations.py`, `tests/test_batch2_wiring.py` (+ `handlers_harness.py`, extended `main_harness.py`, richer `fakemongo.py`).

---
# Batch 1 - production ad-verification security
* **Fail closed by default**: `APP_ENV` defaults to `production`. Production requires `AD_VERIFY_MODE=postback` and a valid `MONETAG_POSTBACK_SECRET`, otherwise the process refuses to start (`ads/security.py`, `config.py`).
  **Before deploying, set the Monetag postback URL + `MONETAG_POSTBACK_SECRET` (KOYEB.md step 1), or the app won't boot / nobody gets files.**
* **Monetag postback hardened** (`ads/postback.py`, `monetag_store.record_postback`): constant-time secret, failed-secret rate limit, strict payload validation, `ymid` must be issued by us and bound to the same user (`telegram_id`), zone and a live session; attempt/session expiry; settled/late postbacks rejected; exact repeats idempotent. Attempts now store `user_id`.
* **Reward path**: `service.done` rejects expired attempts and postback records bound to another user/zone; sessions are expiry-checked explicitly (not only by Mongo's TTL reaper).
* **Tads.me**: no documented server-side verification => in strict mode it is never served and its browser callback can never unlock a file (`verify.decide`, `settings.trust_allowed`, config normalisation, admin patch rejection). Still available, labelled `client_attested`, in the development profile.
* Tests: `tests/test_ad_security.py`, `tests/test_main_wiring.py` (runs the real `main.py` with stubs), 2 new JS tests. Existing fixtures declare the development profile explicitly.

---
# Changes - Ad gate (Monetag + Tads.me)
* **Smart ads ON by default**, `mode=priority` (Monetag first, Tads fallback), Tads auto-enabled when `TADS_WIDGET_ID` is set, `TADS_ALLOW_CLIENT_REWARD` default true.
* **3 rounds** (`GATE_ROUNDS`) of Monetag->Tads; server counts rounds (`sessions.smart_round`, `ad_requests.round/chain_len/round_exhausted`).
* **No ad after all rounds => server-verified ad-blocker challenge => file (grace) or "Ad blocker detected"**: new `ads/gate.py`, `ads/guard.js`,
  routes `POST /api/gate/challenge`, `POST /api/smart/grace`, bait routes, neutral asset paths `/assets/sa.js` + `/assets/sg.js`. See README "Ad gate".
* Ad SDK that loads but defines no global => classified `blocked` (was `timeout`); `runChain` reports `sawBlocked`; Tads host is probed with the Monetag host.
* New collections `ad_gate` (TTL 1h) and `ad_grace`; migration updated (idempotent).
* Dedicated generous rate limiters for gate/bait endpoints (shared carrier-NAT IPs).
* Tests: `tests/test_gate.py` (29 cases, mutation-checked), `tests/guard.test.js`; `run_tests.sh` runs all `tests/*.test.js`.

# Smart rotation upgrade: kya badla hai

Har file ko apne project mein **same path** par replace/add karo (folder structure zip ke andar wahi hai).

## NEW files (add karo)
| File | Kya hai |
|---|---|
| `ads/bandit.py` | Naya engine: Thompson sampling, time-decay, value estimate, probation, traffic-share simulation |
| `tests/test_bandit.py` | 26 naye tests (bandit maths, validation, tick/back-off, report) |

## CHANGED files (replace karo)
| File | Kya badla |
|---|---|
| `ads/settings.py` | Naya `strategy` (`thompson` default / `weighted`), har network ka `ecpm_usd`, naye optimization knobs: `half_life_days`, `prior_strength`, `min_share`, `probation_share`, `use_monetag_estimate`, aur unki validation |
| `ads/engine.py` | `blockers()` (traffic kyun nahi mil raha), Thompson-based `plan_request`, naya `build_model`, naya `tick`: bench back-off x2/x4/x8, probation, strike forgiveness, plan doc mein model/shares/probation save, revenue join fast |
| `ads/report.py` | Report mein `earnings` (expected per 1000 requests, uplift vs equal split), har network ka `expected`, `blockers_text`, `hints`; Telegram summary mein hints + expected earnings |
| `ads/admin_ads.html` | Naya "Earnings engine" panel, strategy select, per-network "Your eCPM" input, naye optimization fields + Monetag estimate checkbox |
| `README.md` | Rotation section naya, "Rotation does nothing" checklist, tests ka note |

## Baaki sab kuch same hai
`main.py`, `ads/routes.py`, `ads/scoring.py`, `ads/service.py`, `ads/store.py`, `ads/verify.py`, adapters, DB collections: koi change nahi. Naya DB collection ya migration nahi chahiye; naye fields `plan` document aur config mein apne aap bhar jaate hain.

## Update ke baad
1. Redeploy / restart karo. Optimizer pehli baar ~5 min (`ADS_OPT_INTERVAL`) mein model banata hai; tab tak purana cold-start rotation chalega.
2. `/admin/ads` kholo: **Earnings engine** panel dekho, jo network "no" dikha raha hai uski wajah wahin likhi hai.
3. Kam se kam 2 networks enable karo aur har ek ka **eCPM** ya daily **revenue** daalo.
4. Purana behaviour chahiye to Routing mein strategy ko "Classic weighted score" kar do.

---
# Ads fix: har baar ad aaye + fast load + AdBlock/DNS detection

| File | Kya badla |
|---|---|
| `miniapp.html` | Ad nahi mila to **khud 4 baar tak dobara try** (naya ymid har baar). Monetag **preload** (`type:'preload'`) pehle, phir turant show. SDK fail hua to bina reload naya tag. **AdBlock / Private DNS / VPN detect** => "Ad blocker detected" screen + "Check again" button (settings badalne par khud aage badh jaata hai). Smart flow me bhi gate + auto-retry. |
| `main.py` | SDK `<script>` ab `async` + `preconnect`/`dns-prefetch` (page render nahi rukta, connection pehle se khula). CFG me `sdk` + `probe` hosts. Naya `GET /api/ping` (blocker check ka control). |

Koi DB/env change nahi. Redeploy karo.

---
# No-fill grace (ad hi na ho to file bina ad ke)

Monetag se 4 baar "Empty feed" aaye **aur** AdBlock/DNS off ho => `POST /api/ad/grace` file de deta hai (koi time limit nahi).
Server khud `monetag_ads` me us session ke `empty_feed` reports ginta hai (`NOFILL_GRACE_FAILS`, default 4). AdBlock/DNS wale user tak yeh pahunchta hi nahi (pehle "Ad blocker detected").
Env: `NOFILL_GRACE=true` (band karne ko false), `NOFILL_GRACE_FAILS=4`. Sirf original Monetag flow me; `SMART_ADS_ENABLED=true` me band.
Files: `main.py` (`/api/ad/grace`, `_deliver`), `db.py` (`grant_grace`), `monetag_store.py` (`count_empty_fails`), `config.py`, `miniapp.html`.
