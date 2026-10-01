"""gunicorn's master logs redacted UTC lines and records each web start once (MON-05, OPS-08).

``powermon.web.gunicorn_conf`` is loaded with ``--config python:powermon.web.gunicorn_conf``:

- ``logconfig_dict`` routes gunicorn's own error logger through RedactingFormatter to
  stdout and keeps the access log off (it would print ``?key=``) (D-16, T-02-09).
- ``when_ready`` runs once in the master, before any worker forks, and writes
  ``system_state.web_started_at`` (RESEARCH Pattern 12; never per worker, Pitfall 11).
  A database error is retried with one class-name-only WARNING, then the master exits 1
  with a fixed message instead of printing a psycopg traceback raw to stderr (Pitfall 6).
- INV-10 #2: a web-only redeploy that loses one heartbeat gives no false OFF, because the
  timeout of a location that is on counts from max(last heartbeat, web start).

The hooks are called directly; no test starts gunicorn (the real master is a scripted
check in the plan's verify step). Tests that write system_state use
``django_db(transaction=True)``, like the detection tests: ``run_cycle`` calls
``close_old_connections()``, and teardown truncates every table, the singleton included.
"""

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, FakeClock
from django.conf import settings
from django.db import OperationalError, connections
from django.utils.module_loading import import_string
from gunicorn.config import make_settings

from powermon.alerts.models import OutboxMessage
from powermon.engine import transitions
from powermon.engine.models import SystemState
from powermon.worker import detection

UTC_LINE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{3}[+]00:00"
CONF_LOGGER = "powermon.web.gunicorn_conf"


@pytest.fixture
def gconf() -> ModuleType:
    return import_module("powermon.web.gunicorn_conf")


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _conf_records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == CONF_LOGGER and r.levelno == level]


# logconfig_dict (D-16, T-02-09)


def test_logconfig_dict_routes_gunicorn_error_through_redaction(gconf: ModuleType) -> None:
    conf = gconf.logconfig_dict
    level = settings.CFG.log_level

    assert conf["version"] == 1
    assert conf["disable_existing_loggers"] is False
    redacting = conf["formatters"]["redacting"]
    assert redacting["()"] == "powermon.logging_setup.RedactingFormatter"
    assert redacting["format"] == import_module("powermon.logging_setup").FORMAT
    assert conf["handlers"]["stdout"] == {
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "formatter": "redacting",
    }
    assert conf["loggers"]["gunicorn.error"] == {
        "level": level,
        "handlers": ["stdout"],
        "propagate": False,
    }
    access = conf["loggers"]["gunicorn.access"]
    assert access["handlers"] == []
    assert access["propagate"] is False
    assert conf["loggers"]["urllib3"] == {"level": "WARNING"}
    assert conf["loggers"]["django.db.backends"] == {"level": "WARNING"}

    formatter = import_string(redacting["()"])(redacting["format"])
    record = logging.LogRecord(
        "gunicorn.error",
        logging.ERROR,
        __file__,
        1,
        "Error handling /bot%s/x",
        (DEFAULT_BOT_TOKEN,),
        None,
    )
    text = formatter.format(record)

    assert re.fullmatch(
        rf"{UTC_LINE} ERROR gunicorn[.]error Error handling /\[REDACTED-TOKEN\]/x", text
    )


def test_logconfig_dict_applies_in_a_process_without_django() -> None:
    # The master loads the config module before django.setup(): no Django import, and the
    # dict must be accepted by dictConfig exactly as gunicorn passes it.
    probe = (
        "import logging, logging.config, sys\n"
        "import powermon.web.gunicorn_conf as g\n"
        "assert 'django' not in sys.modules, 'gunicorn_conf imported Django'\n"
        "logging.config.dictConfig(g.logconfig_dict)\n"
        "error = logging.getLogger('gunicorn.error')\n"
        "error.info('Booting worker; url /bot%s/getMe', sys.argv[1])\n"
        "logging.getLogger('gunicorn.access').info('GET /hb?key=SECRETKEY 200')\n"
    )
    env = {**os.environ, "LOG_LEVEL": "INFO"}

    result = subprocess.run(
        [sys.executable, "-c", probe, DEFAULT_BOT_TOKEN],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    assert re.fullmatch(
        rf"{UTC_LINE} INFO gunicorn[.]error Booting worker; url /\[REDACTED-TOKEN\]/getMe", lines[0]
    )
    # The access log stays off: no stdout line and nothing on stderr either.
    assert "SECRETKEY" not in result.stdout + result.stderr
    assert DEFAULT_BOT_TOKEN not in result.stdout + result.stderr


def test_gunicorn_conf_exposes_only_the_intended_settings(gconf: ModuleType) -> None:
    # gunicorn applies every module attribute whose name is a setting: a stray name such
    # as `config` (gunicorn's own --config) would make the master refuse to start. No
    # per-worker hook (post_fork, post_worker_init) may write the web start either.
    names = set(vars(gconf)) & set(make_settings())

    assert names == {"logconfig_dict", "when_ready"}


# record_web_start (MON-05)


@pytest.mark.django_db(transaction=True)
def test_record_web_start_writes_web_started_at(gconf: ModuleType) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": _at(8, 0)}
    )

    assert gconf.record_web_start(FakeClock(_at(10, 0, 25))) is True

    row = SystemState.objects.get(pk=1)
    assert row.web_started_at == _at(10, 0, 25)
    # Only the web start moves; the worker's anchor is the worker's.
    assert row.detection_resumed_at == _at(9, 0)

    # Edge: a missing singleton row is recreated.
    SystemState.objects.all().delete()
    assert gconf.record_web_start(FakeClock(_at(10, 5))) is True
    assert SystemState.objects.get(pk=1).web_started_at == _at(10, 5)


def _failing_writes(monkeypatch: pytest.MonkeyPatch, failures: int | None) -> list[dict[str, Any]]:
    """Make SystemState writes raise OperationalError ``failures`` times (None: always)."""
    calls: list[dict[str, Any]] = []
    real = SystemState.objects.update_or_create

    def write(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if failures is None or len(calls) <= failures:
            raise OperationalError("connection to server at db failed: password=hunter2")
        return real(*args, **kwargs)

    monkeypatch.setattr(SystemState.objects, "update_or_create", write)
    return calls


def test_record_web_start_gives_up_with_one_warning(
    gconf: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _failing_writes(monkeypatch, failures=None)
    caplog.set_level(logging.INFO, logger=CONF_LOGGER)

    assert gconf.record_web_start(FakeClock(_at(10, 0, 25)), attempts=3, wait_s=0) is False

    assert len(calls) == 3
    assert _conf_records(caplog, logging.WARNING) == [
        "web start: database unavailable (OperationalError); retrying"
    ]
    # The driver's error text (host, user, password) is never logged.
    assert "hunter2" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_record_web_start_recovers_after_a_short_outage(
    gconf: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _failing_writes(monkeypatch, failures=2)
    caplog.set_level(logging.INFO, logger=CONF_LOGGER)
    clock = FakeClock(_at(10, 0, 25))

    assert gconf.record_web_start(clock, attempts=5, wait_s=0) is True

    assert len(calls) == 3
    assert len(_conf_records(caplog, logging.WARNING)) == 1
    assert SystemState.objects.get(pk=1).web_started_at == _at(10, 0, 25)
    assert "hunter2" not in caplog.text


def test_record_web_start_defaults_retry_for_about_30_s(gconf: ModuleType) -> None:
    assert gconf.WEB_START_ATTEMPTS == 30
    assert gconf.WEB_START_RETRY_S == 1.0


# when_ready (the master hook)


def test_when_ready_exits_when_the_start_cannot_be_recorded(
    gconf: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(gconf, "record_web_start", lambda clock: False)
    closed: list[bool] = []
    monkeypatch.setattr(connections, "close_all", lambda: closed.append(True))

    with pytest.raises(SystemExit) as exc_info:
        gconf.when_ready(object())

    assert exc_info.value.code == 1
    assert _conf_records(caplog, logging.ERROR) == [
        "web start: could not record the start time; exiting so Docker restarts web"
    ]
    assert closed == [True]


def test_when_ready_exits_on_an_unexpected_error(
    gconf: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A bug must not escape the hook: gunicorn would print its traceback raw to stderr.
    def boom(clock: Any) -> bool:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(gconf, "record_web_start", boom)
    monkeypatch.setattr(connections, "close_all", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        gconf.when_ready(object())

    assert exc_info.value.code == 1
    [record] = [r for r in caplog.records if r.name == CONF_LOGGER and r.levelno == logging.ERROR]
    assert record.getMessage() == "web start failed; exiting so Docker restarts web"
    assert record.exc_info is not None


@pytest.mark.django_db(transaction=True)
def test_when_ready_records_once_and_closes_connections(
    gconf: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gconf, "SystemClock", lambda: FakeClock(_at(10, 0, 25)))
    real_close_all = connections.close_all
    seen_at_close: list[datetime | None] = []

    def close_all() -> None:
        # The write is committed before the master closes its sockets.
        seen_at_close.append(SystemState.objects.get(pk=1).web_started_at)
        real_close_all()

    monkeypatch.setattr(connections, "close_all", close_all)

    gconf.when_ready(object())

    # Closed once, after the write, so forked workers never share the master's DB socket.
    assert seen_at_close == [_at(10, 0, 25)]
    assert SystemState.objects.get(pk=1).web_started_at == _at(10, 0, 25)


# INV-10 #2: a web-only redeploy that swallows a heartbeat (MON-05)


def _heartbeating_at_10s(location_factory: Callable[..., Any]) -> Any:
    """A location (period 60 s, grace 30 s) heartbeating at second :10 of each minute."""
    location = location_factory()
    for minute in (57, 58, 59):
        transitions.record_heartbeat(location.pk, _at(9, minute, 10))
    return location


def _web_only_redeploy_history(location_factory: Callable[..., Any]) -> Any:
    # The worker started at 09:00; the old web container at 08:00.
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": _at(8, 0)}
    )
    return _heartbeating_at_10s(location_factory)


@pytest.mark.django_db(transaction=True)
def test_INV10_web_only_redeploy_lost_heartbeat_no_off(
    gconf: ModuleType, location_factory: Callable[..., Any]
) -> None:
    location = _web_only_redeploy_history(location_factory)
    # The web is down 10:00:05-10:00:25, so the 10:00:10 heartbeat never arrives; the new
    # container's master records its start at 10:00:25.
    assert gconf.record_web_start(FakeClock(_at(10, 0, 25))) is True

    # 09:59:10 + 90 s = 10:00:40 has passed, but the window counts from 10:00:25.
    assert detection.run_cycle(_at(10, 0, 45)) == 0
    assert detection.run_cycle(_at(10, 1, 5)) == 0
    # 10:01:10 < 10:00:25 + 90 s: the next heartbeat is an ordinary one.
    assert transitions.record_heartbeat(location.pk, _at(10, 1, 10)) == "plain"

    assert OutboxMessage.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_INV10_without_the_web_start_the_lost_heartbeat_gives_a_false_off(
    location_factory: Callable[..., Any],
) -> None:
    # Failure direction: the same history with no web start recorded.
    location = _web_only_redeploy_history(location_factory)

    offs = detection.run_cycle(_at(10, 0, 45)) + detection.run_cycle(_at(10, 1, 5))

    assert offs == 1
    # ...and the next heartbeat turns it into a false OFF + ON pair.
    assert transitions.record_heartbeat(location.pk, _at(10, 1, 10)) == "restored"
    kinds = list(OutboxMessage.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == ["power_off", "power_on"]
