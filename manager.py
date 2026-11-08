import asyncio
import hashlib
import logging
import os
import time

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application

import db
import handlers
import health
import migrator
import monetag_engine
import observability as obs
from ads import engine as ads_engine, slog
from config import AUTO_DELETE_SECONDS, BASE_URL, MAIN_BOT_TOKEN, OWNER_ID, RT, SESSION_TTL, WEBHOOK_SECRET
from migrations import registry as migration_registry

log = logging.getLogger("manager")


async def run_startup_migrations() -> "migrator.Result":
    """Versioned, lock-protected, verified migrations (see migrator.py). Raises migrator.MigrationError on any failure."""
    result = await migrator.ensure_schema(
        lambda name: db._db[name], migration_registry.MIGRATIONS,
        reconcile=migration_registry.reconcilers(SESSION_TTL), **migrator.settings_from_env(os.environ))
    log.info("database schema ready (version %s, applied by this instance: %s)", result.version, result.ran or "none")
    return result


class BotManager:
    """Runs the single bot (MAIN_BOT_TOKEN) plus the supervised background loops.

    start() returns immediately; the real startup (migrations -> Telegram init -> webhook -> background tasks) runs as ONE supervised task,
    so the HTTP server answers the liveness probe while it works and /readyz says when traffic may flow. A permanent failure (failed
    migration, invalid bot token, Telegram unreachable for STARTUP_TIMEOUT) marks the lifecycle FAILED, alerts the owner once and ends the
    process non-zero. Nothing is served on a half-started instance (health.HealthGate)."""

    def __init__(self, *, lifecycle: health.Lifecycle | None = None, alerts: obs.AlertManager | None = None,
                 tasks: health.TaskSupervisor | None = None):
        self.main: Application | None = None
        self.lifecycle = lifecycle or health.LIFECYCLE
        self.alerts = alerts or obs.ALERTS
        self.tasks = tasks or health.TaskSupervisor(on_crash=self._on_task_crash)
        self.db_dep = obs.Dependency("mongodb", RT.db_fail_threshold)
        self.webhook_dep = obs.Dependency("telegram_webhook", 3)
        self.webhook_info: dict = {}
        self._last_webhook_error_ts = 0.0
        self._stopping = False

    @property
    def secret(self) -> str:
        return hashlib.sha256(f"{WEBHOOK_SECRET}:main".encode()).hexdigest()

    @property
    def webhook_url(self) -> str:
        return f"{BASE_URL}/webhook"

    # ------------------------------------------------------------------ startup
    async def start(self) -> None:
        """Non-blocking and idempotent: a second call (duplicate lifespan, reload) starts NOTHING again."""
        if not self.lifecycle.begin_start():
            slog.event("startup_duplicate_ignored", level="warning", state=self.lifecycle.state.value)
            return
        slog.event("app_starting", pid=os.getpid())
        self.tasks.start("startup", self._startup, restart=False)

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        """Test/diagnostic helper: True once startup finished, False if it failed / stopped / timed out."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.lifecycle.accepting:
                return True
            if self.lifecycle.state in (health.State.FAILED, health.State.STOPPING, health.State.STOPPED):
                return False
            await asyncio.sleep(0.01)
        return False

    def _build_app(self) -> Application:
        app = Application.builder().token(MAIN_BOT_TOKEN).updater(None).concurrent_updates(True).build()
        app.bot_data["manager"] = self
        app.bot_data["bot_id"] = "main"
        handlers.register_main_handlers(app)
        app.add_error_handler(self.on_bot_error)
        return app

    async def _startup(self) -> None:
        t0 = time.monotonic()
        lc = self.lifecycle
        phase = "migrations"
        try:
            lc.set_phase(phase)
            # Schema first and REQUIRED: a failed / unverifiable migration ends here (_fatal); no traffic is ever served on a
            # half-migrated database. Transient DB errors are retried inside migrator.ensure_schema (MIGRATION_WAIT_TIMEOUT).
            await run_startup_migrations()

            phase = "telegram_init"
            lc.set_phase(phase)
            app = await health.retry_until(self._init_app, timeout=RT.startup_timeout, what="telegram_init")
            self.main = app
            await app.start()
            try:  # koi slash-command menu nahi: sab kuch buttons me
                await app.bot.delete_my_commands()
            except Exception as e:  # noqa: BLE001
                slog.event("delete_my_commands_failed", level="warning", error=type(e).__name__)
            self.alerts.set_sender(self._send_owner)
            await self.alerts.flush()

            phase = "webhook"
            lc.set_phase(phase)
            try:
                await health.retry_until(self._register_webhook_once, timeout=RT.webhook_register_timeout, what="set_webhook")
                self.webhook_dep.success()
            except Exception as e:  # noqa: BLE001
                if health.is_permanent_telegram(e) or isinstance(e, BadRequest):
                    raise                                  # bad token / bad webhook URL: configuration, will not fix itself
                await self._webhook_failed("set_webhook", e)   # Telegram flaky: serve anyway, the monitor keeps retrying

            phase = "tasks"
            lc.set_phase(phase)
            self._start_background_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await self._fatal(phase, e)
            return
        lc.mark_ready()
        dur = int((time.monotonic() - t0) * 1000)
        obs.METRICS.gauge("startup.duration_ms", dur)
        slog.event("startup_complete", duration_ms=dur, webhook=self.webhook_dep.status, tasks=sorted(self.tasks.snapshot()))

    async def _init_app(self) -> Application:
        app = self._build_app()                            # fresh object per attempt: a half-initialised Application is never reused
        await app.initialize()                             # getMe
        return app

    def _start_background_tasks(self) -> None:
        for name, factory in (("cleanup", self.cleanup_loop), ("monetag_optimizer", monetag_engine.optimizer_loop),
                              ("ads_optimizer", ads_engine.loop), ("db_monitor", self._db_monitor),
                              ("webhook_monitor", self._webhook_monitor)):
            self.tasks.start(name, factory)               # idempotent: a duplicate start is ignored and logged

    async def _fatal(self, phase: str, exc: BaseException) -> None:
        err = type(exc).__name__
        detail = obs.redact(str(exc))[:300]
        slog.event("startup_failed", level="critical", phase=phase, error=err, detail=detail)
        obs.METRICS.inc("startup.failed")
        self.lifecycle.fail(phase, err)
        if not health.is_permanent_telegram(exc):          # with an invalid token the owner can't be reached anyway
            try:
                if self.main is None or not self.alerts._sender:
                    self.alerts.set_sender(self._standalone_send)
                await self.alerts.alert(f"startup_failed:{phase}",
                                        f"The bot failed to start (phase: {phase}, {err}: {detail}). The instance serves no traffic and "
                                        "exits so the platform can restart it. Check the logs.")
            except Exception:  # noqa: BLE001
                pass
        self.lifecycle.request_exit(1)

    # ------------------------------------------------------------------ webhook
    async def _register_webhook_once(self) -> None:
        await self.main.bot.set_webhook(url=self.webhook_url, secret_token=self.secret, allowed_updates=["message", "callback_query"])
        slog.event("webhook_registered")

    async def _webhook_failed(self, what: str, e: BaseException) -> None:
        ev = self.webhook_dep.failure(e)
        obs.METRICS.inc("webhook.register_failed")
        slog.event("webhook_registration_failed", level="warning", what=what, error=type(e).__name__, detail=obs.redact(str(e))[:200])
        if ev == "down":
            await self.alerts.alert(
                "webhook_down", f"Telegram webhook could not be registered/verified ({what}: {type(e).__name__}). The bot will not receive "
                                "messages until this recovers; retrying automatically.")

    async def _check_webhook(self) -> None:
        """Verify with Telegram that OUR url is registered and surface Telegram's own delivery errors."""
        bot = self.main.bot
        try:
            info = await asyncio.wait_for(bot.get_webhook_info(), 15)
        except Exception as e:  # noqa: BLE001
            await self._webhook_failed("get_webhook_info", e)
            return
        err_date = getattr(info, "last_error_date", None)
        err_ts = err_date.timestamp() if hasattr(err_date, "timestamp") else (float(err_date) if err_date else 0.0)
        msg = obs.redact(getattr(info, "last_error_message", "") or "")[:200]
        self.webhook_info = {"url_registered": getattr(info, "url", "") == self.webhook_url,
                             "pending_updates": getattr(info, "pending_update_count", None),
                             "last_error": msg or None, "last_error_at": obs._iso(err_ts) if err_ts else None}
        if getattr(info, "url", "") != self.webhook_url:
            slog.event("webhook_url_mismatch", level="warning", registered=bool(getattr(info, "url", "")))
            try:
                await health.retry_until(self._register_webhook_once, timeout=30, what="set_webhook")
            except Exception as e:  # noqa: BLE001
                await self._webhook_failed("set_webhook", e)
                return
        if err_ts and err_ts > self._last_webhook_error_ts and time.time() - err_ts < 900:
            self._last_webhook_error_ts = err_ts
            obs.METRICS.inc("webhook.telegram_reported_error")
            slog.event("telegram_webhook_error", level="warning", message=msg, pending=self.webhook_info["pending_updates"])
            await self.alerts.alert("webhook_delivery_error",
                                    f"Telegram reports webhook delivery errors: \"{msg}\" (pending updates: "
                                    f"{self.webhook_info['pending_updates']}). Check /readyz, the service logs and BASE_URL.",
                                    severity="warning", cooldown=1800)
        elif self.webhook_dep.success() == "recovered":
            slog.event("webhook_recovered")
            await self.alerts.recover("webhook_down", "Telegram webhook is registered and healthy again.")

    async def _webhook_monitor(self) -> None:
        while True:
            await asyncio.sleep(10 if self.webhook_dep.consecutive_failures else RT.webhook_check_interval)
            await self._check_webhook()

    async def _db_monitor(self) -> None:
        await health.db_monitor_loop(db.ping, self.db_dep, self.alerts, interval=RT.db_health_interval,
                                     timeout=RT.mongo_selection_timeout_ms / 1000 + 1)

    # ------------------------------------------------------------------ errors / alerts
    async def on_bot_error(self, update, context) -> None:
        """PTB error handler: one structured, de-duplicated line each; owner alert only for bursts. The update (user data) is never logged."""
        err = getattr(context, "error", None)
        name = type(err).__name__ if err is not None else "Unknown"
        obs.METRICS.inc("bot.handler_errors")
        slog.event("bot_handler_error", level="error", error=name, detail=obs.redact(str(err))[:200] if err is not None else None)
        obs.BOT_ERROR_WATCH.hit(name)
        if obs.BOT_ERROR_WATCH.crossed():
            top = ", ".join(f"{k}={v}" for k, v in list(obs.BOT_ERROR_WATCH.breakdown().items())[:5])
            await self.alerts.alert("bot_error_burst", f"{len(obs.BOT_ERROR_WATCH._ev)} bot handler errors in 5 minutes ({top}).",
                                    severity="warning")

    async def _on_task_crash(self, name: str, consecutive: int, exc: BaseException) -> None:
        await self.alerts.alert(f"task_crash:{name}", f"Background task '{name}' keeps crashing ({consecutive} times in a row, "
                                                      f"last: {type(exc).__name__}). It is restarting with backoff.", severity="warning")

    async def _send_owner(self, text: str) -> None:
        await self.main.bot.send_message(OWNER_ID, text)

    async def _standalone_send(self, text: str) -> None:
        """Alert path for failures BEFORE the main Application exists (e.g. a failed migration): a short-lived Bot for this one message."""
        from telegram import Bot
        async with Bot(MAIN_BOT_TOKEN) as bot:
            await bot.send_message(OWNER_ID, text)

    async def notify_owner(self, text: str):
        try:
            await self._send_owner(obs.redact(text))
        except Exception as e:  # noqa: BLE001
            log.warning("owner notify failed: %s", type(e).__name__)

    async def alert_once(self, key: str, text: str):
        """Backwards-compatible entry point (delivery.py): de-duplicated per key (6 h), shared hourly cap, redacted."""
        await self.alerts.alert(key, text, severity="warning", cooldown=6 * 3600)

    async def active_usernames(self) -> list[str]:
        return [self.main.bot.username]

    # ------------------------------------------------------------------ shutdown
    async def stop(self) -> None:
        """Graceful, bounded, idempotent: stop background tasks -> let queued Telegram updates finish -> stop the bot -> close MongoDB.
        The webhook is NOT deleted: Telegram keeps retrying and the next instance picks it up."""
        if self._stopping or not self.lifecycle.begin_stopping():
            return
        self._stopping = True
        t0 = time.monotonic()
        budget = RT.shutdown_timeout
        slog.event("shutdown_started", budget_s=budget)

        def left() -> float:
            return max(0.5, budget - (time.monotonic() - t0))

        stuck = await self.tasks.stop(timeout=min(5.0, left()))
        if stuck:
            slog.event("shutdown_tasks_stuck", level="warning", tasks=stuck)
        app = self.main
        if app is not None:
            q = getattr(app, "update_queue", None)
            end = time.monotonic() + min(5.0, left())
            while q is not None and not q.empty() and time.monotonic() < end:
                await asyncio.sleep(0.05)
            for step in ("stop", "shutdown"):
                try:
                    await asyncio.wait_for(getattr(app, step)(), left())
                except Exception as e:  # noqa: BLE001
                    slog.event("shutdown_step_failed", level="warning", step=step, error=type(e).__name__)
        try:
            db.close()
        except Exception as e:  # noqa: BLE001
            slog.event("shutdown_step_failed", level="warning", step="db_close", error=type(e).__name__)
        self.lifecycle.mark_stopped()
        slog.event("shutdown_complete", duration_ms=int((time.monotonic() - t0) * 1000))

    # ---------- auto-delete delivered files ----------
    async def cleanup_loop(self):
        if AUTO_DELETE_SECONDS <= 0:
            return
        while True:
            try:
                await self.cleanup_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("cleanup error")
            await asyncio.sleep(10)

    async def cleanup_once(self):
        now = time.time()
        for d in await db.due_deletes(now, 100):
            overdue = now - d["delete_at"] > 3600
            if d["bot_id"] != self.main.bot.id:
                if overdue:      # purane (hata diye gaye) bots ke entries
                    await db.remove_delete(d["_id"])
                continue
            try:
                await self.main.bot.delete_message(d["chat_id"], d["message_id"])
            except RetryAfter:
                return
            except (BadRequest, Forbidden):
                pass  # pehle hi delete ho chuka / ab delete nahi ho sakta
            except TelegramError as e:
                log.warning("auto-delete failed: %s", type(e).__name__)
                if not overdue:
                    continue
            await db.remove_delete(d["_id"])
