"""Production entry point:  python serve.py   (Dockerfile CMD and Procfile).

* Binds 0.0.0.0:$PORT (Koyeb injects PORT for the port configured on the service; default 8000). An invalid PORT stops the process.
* Validates the environment BEFORE importing the application, so a missing variable produces one readable message (names only, never
  values) instead of a stack trace from deep inside an import.
* Installs structured logging + secret redaction before anything can log.
* SIGTERM/SIGINT => uvicorn graceful shutdown: new connections are refused, in-flight requests get HTTP_GRACEFUL_TIMEOUT seconds, then the
  lifespan runs BotManager.stop() (background tasks, queued Telegram updates, bot, MongoDB) within SHUTDOWN_TIMEOUT.
* A permanent startup failure (failed migration, invalid bot token, ...) makes the process exit NON-ZERO, so the platform restarts it or
  marks the deployment unhealthy. A clean SIGTERM exits 0.
`uvicorn main:app --host 0.0.0.0 --port $PORT` still works (the lifecycle then ends the process with SIGTERM on a fatal startup error).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # also works under `python -I`


def main() -> int:
    import envcheck

    try:
        envcheck.validate_required(os.environ)
        rt = envcheck.parse_runtime(os.environ)
        port = envcheck.parse_port(os.environ)
    except envcheck.EnvError as e:
        sys.stderr.write(f"FATAL: {e}\n")
        return 2

    import observability
    observability.bootstrap(os.environ, rt)

    import logging
    import uvicorn
    import health

    log = logging.getLogger("serve")
    try:
        import main as application  # noqa: F401  (config / security validation happens on import and raises with a clear message)
    except Exception as e:  # noqa: BLE001
        log.critical("application failed to import: %s: %s", type(e).__name__, observability.redact(str(e))[:500])
        return 3

    config = uvicorn.Config(application.app, host="0.0.0.0", port=port, log_config=None, proxy_headers=False, server_header=False,
                            timeout_graceful_shutdown=rt.http_graceful_timeout, timeout_keep_alive=65, access_log=True)
    server = uvicorn.Server(config)
    health.LIFECYCLE.set_exit_callback(lambda code: setattr(server, "should_exit", True))
    log.info("listening on 0.0.0.0:%s (graceful_timeout=%ss shutdown_timeout=%ss)", port, rt.http_graceful_timeout, rt.shutdown_timeout)
    server.run()
    return health.LIFECYCLE.exit_code


if __name__ == "__main__":
    sys.exit(main())
