"""gunicorn settings for the web container (D-16, MON-05, OPS-08).

Loaded by both compose files with ``gunicorn powermon.wsgi --config
python:powermon.web.gunicorn_conf``. gunicorn applies every module attribute whose name
is one of its settings, so only two such names live here: ``logconfig_dict`` and
``when_ready``. Every other name is private or not a gunicorn setting (``config`` is
one, so the config module is imported as ``app_config``).

- ``logconfig_dict``: the app's logging (``logging_setup.build_logging`` at LOG_LEVEL),
  plus gunicorn's own loggers. ``gunicorn.error`` writes through RedactingFormatter to
  stdout (UTC ISO timestamps, tokens and keys scrubbed). ``gunicorn.access`` has no
  handler and does not propagate: the access log stays off, because it would print the
  ``?key=`` device key.
- ``when_ready`` runs once in the master, after the socket is bound and before any worker
  forks, and records ``system_state.web_started_at``. Each web container start thereby
  opens a fresh detection window (INV-10 #2). It is not done in ``post_fork``, which
  would move the anchor whenever gunicorn replaces a worker (RESEARCH Pitfall 11).
- On a database error the write is retried every second for about 30 s, with one WARNING
  naming the error class only. Then the master exits 1 with a fixed message so Docker
  restarts web. An exception escaping the hook would make gunicorn print its traceback,
  psycopg message, host and user included, raw to stderr (RESEARCH Pitfall 6).

Import time stays free of Django: the master reads this module before ``django.setup()``.
"""

import contextlib
import logging
import os
import threading
from typing import Any

from powermon import config as app_config
from powermon import logging_setup
from powermon.clock import Clock, SystemClock

log = logging.getLogger(__name__)

# About 30 s of retries before the master gives up and Docker restarts web.
WEB_START_ATTEMPTS = 30
WEB_START_RETRY_S = 1.0


def _logconfig(level: str) -> dict[str, Any]:
    conf = logging_setup.build_logging(level)
    conf["loggers"]["gunicorn.error"] = {"level": level, "handlers": ["stdout"], "propagate": False}
    # WARNING: gunicorn writes access lines at INFO, so none is even built.
    conf["loggers"]["gunicorn.access"] = {"level": "WARNING", "handlers": [], "propagate": False}
    return conf


# A bad environment stops the master here with the config error (it names the variable,
# never a value), as it would stop django.setup().
logconfig_dict = _logconfig(app_config.load(os.environ).log_level)


def record_web_start(
    clock: Clock, *, attempts: int = WEB_START_ATTEMPTS, wait_s: float = WEB_START_RETRY_S
) -> bool:
    """Write ``system_state.web_started_at = clock.now()``; False after ``attempts`` failures."""
    from django.db import DatabaseError, connection

    from powermon.engine.models import SystemState

    for attempt in range(attempts):
        at = clock.now()
        try:
            SystemState.objects.update_or_create(pk=1, defaults={"web_started_at": at})
        except DatabaseError as exc:
            if attempt == 0:
                # No exception text: driver errors carry the host, the user and more.
                log.warning("web start: database unavailable (%s); retrying", type(exc).__name__)
            # A fresh connection next time; closing a broken one may fail too.
            with contextlib.suppress(DatabaseError):
                connection.close()
            if attempt + 1 < attempts:
                threading.Event().wait(wait_s)
        else:
            log.info("web start recorded; detection windows count from %s", at.isoformat())
            return True
    return False


def when_ready(server: Any) -> None:
    """gunicorn master hook: record this container's start once, before workers fork."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powermon.settings")
    try:
        import django
        from django.apps import apps
        from django.db import connections

        # Tests run with Django set up; a second setup would re-run dictConfig.
        if not apps.ready:
            django.setup()
        try:
            ok = record_web_start(SystemClock())
        finally:
            # Forked workers must never share the master's database socket.
            connections.close_all()
    except Exception:
        # Logged through the redacting formatter instead of gunicorn's raw traceback.
        log.exception("web start failed; exiting so Docker restarts web")
        raise SystemExit(1) from None
    if not ok:
        log.error("web start: could not record the start time; exiting so Docker restarts web")
        raise SystemExit(1)
