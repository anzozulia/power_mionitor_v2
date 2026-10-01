"""Worker supervision: the watchdog, the health file and the DB-outage log (D-15, OPS-05).

Docker restarts a container only when its process exits; an "unhealthy" status restarts
nothing (INV-13). So the worker's main thread is a watchdog: when a loop has not stamped
its progress for longer than its limit (detection 60 s, Telegram I/O 180 s), or its thread
died, the stall action runs once. By default it logs CRITICAL, dumps every thread's stack,
flushes and exits 70. These tests inject the stall action and drive time with
``FakeClock``; nothing here sleeps or monkeypatches time. Threads are plain
``threading.Thread`` objects started and joined inside each test.

- INV-13 #3: a loop that stops stamping (stuck inside a call) triggers the stall action
  strictly after its limit, and a dead loop thread triggers it at once.
- E1: a loop thread that ended while the stop event is set (SIGTERM) is a shutdown, never
  a stall; the thread's state is read before the stop's.
- The health file is touched on every healthy cycle; a path that cannot be written logs
  one WARNING per run of failures and never raises.
- An unreachable database is progress, not a stall: ``DbOutageLog`` logs one WARNING when
  it goes away and one when it is back, with the error class only (D-16, OPS-08).
"""

import faulthandler
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from django.db import OperationalError

from powermon.worker import supervision

T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
LIMITS = {"detection": 60.0, "telegram-io": 180.0}
SUPERVISION_LOGGER = supervision.__name__
OUTAGE_LOGGER = "tests.worker.outage"


@contextmanager
def _running(*names: str) -> Iterator[dict[str, threading.Thread]]:
    """Live threads with these names, each waiting until the block ends."""
    release = threading.Event()
    threads = {name: threading.Thread(target=release.wait, args=(10,), name=name) for name in names}
    for thread in threads.values():
        thread.start()
    try:
        yield threads
    finally:
        release.set()
        for thread in threads.values():
            thread.join(5)


def _records(caplog: pytest.LogCaptureFixture, logger: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger]


def _finished(name: str) -> threading.Thread:
    """A loop thread that has already ended, as if an error escaped its loop."""
    thread = threading.Thread(target=lambda: None, name=name)
    thread.start()
    thread.join(5)
    return thread


# Progress stamps


def test_progress_keeps_the_last_stamp_per_loop() -> None:
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    assert progress.last("detection") is None

    progress.stamp("detection")
    clock.advance(seconds=5)
    progress.stamp("telegram-io")
    clock.advance(seconds=5)
    progress.stamp("telegram-io")

    assert (progress.last("detection"), progress.last("telegram-io")) == (0.0, 10.0)


# The watchdog (INV-13 #3, D-15)


def test_INV13_hung_loop_triggers_watchdog_stall_action() -> None:
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    with _running("detection", "telegram-io") as threads:
        progress.stamp("detection")
        progress.stamp("telegram-io")
        watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append)

        # The detection loop is stuck inside a call and stamps nothing more.
        clock.advance(seconds=60)
        assert watchdog.check() is None  # exactly the limit is not over it
        assert stalls == []

        clock.advance(milliseconds=1)
        assert watchdog.check() == "detection"
        assert stalls == ["detection"]


def test_watchdog_treats_a_dead_thread_as_a_stall() -> None:
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    with _running("detection") as running:
        progress.stamp("detection")
        progress.stamp("telegram-io")  # fresh, but the thread behind it is gone
        threads = {**running, "telegram-io": _finished("telegram-io")}
        watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append)

        assert watchdog.check() == "telegram-io"

    assert stalls == ["telegram-io"]


def test_watchdog_acts_once_when_both_loops_stall() -> None:
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    with _running("detection", "telegram-io") as threads:
        progress.stamp("detection")
        progress.stamp("telegram-io")
        watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append)
        clock.advance(seconds=181)

        found = [watchdog.check() for _ in range(3)]

    # The limits are walked in their given order; the action runs exactly once.
    assert found == ["detection", "detection", "detection"]
    assert stalls == ["detection"]


# E1 (final audit): a loop thread that ended because of the stop (SIGTERM) is a shutdown,
# not a stall. Only a loop that dies or hangs while the worker is not stopping is one.


class _EndsWithTheStop(threading.Thread):
    """A loop thread that sees the stop and returns while the watchdog asks about it.

    Never started: ``is_alive()`` sets the stop and answers False, as a real loop thread
    that ends between the watchdog's two reads would look.
    """

    def __init__(self, name: str, stop: threading.Event) -> None:
        super().__init__(name=name)
        self.stop_event = stop

    def is_alive(self) -> bool:
        self.stop_event.set()
        return False


def test_E1_a_loop_that_ended_because_of_the_stop_is_not_a_stall() -> None:
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    stop = threading.Event()
    progress.stamp("detection")
    progress.stamp("telegram-io")
    # SIGTERM: the stop is set, and both loops saw it and returned.
    stop.set()
    threads = {name: _finished(name) for name in LIMITS}
    watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append, stop=stop)

    assert watchdog.check() is None
    assert stalls == []


def test_E1_the_stop_is_read_after_the_thread_state() -> None:
    # Read the other way round, a loop that ends between the two reads would look dead.
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    stop = threading.Event()
    progress.stamp("detection")
    progress.stamp("telegram-io")
    threads = {name: _EndsWithTheStop(name, stop) for name in LIMITS}
    watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append, stop=stop)

    assert watchdog.check() is None
    assert stop.is_set()
    assert stalls == []


@pytest.mark.parametrize("failure", ["dead", "hung"])
def test_E1_a_loop_that_dies_or_hangs_while_not_stopping_is_still_a_stall(failure: str) -> None:
    clock = FakeClock(T0)
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    stop = threading.Event()
    with _running("detection", "telegram-io") as running:
        progress.stamp("detection")
        progress.stamp("telegram-io")
        threads = dict(running)
        if failure == "dead":
            threads["telegram-io"] = _finished("telegram-io")
        else:
            # Stuck inside a call: the I/O loop stamps nothing for over its 180 s.
            clock.advance(seconds=180, milliseconds=1)
            progress.stamp("detection")
        watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append, stop=stop)

        assert watchdog.check() == "telegram-io"

    assert stalls == ["telegram-io"]
    assert not stop.is_set()


def test_a_loop_that_never_stamped_counts_from_the_watchdog_start() -> None:
    clock = FakeClock(T0)
    clock.advance(seconds=1000)  # the watchdog starts long after the clock's zero
    progress = supervision.Progress(clock)
    stalls: list[str] = []
    with _running("detection", "telegram-io") as threads:
        progress.stamp("telegram-io")
        watchdog = supervision.Watchdog(clock, progress, LIMITS, threads, stalls.append)

        assert watchdog.check() is None
        clock.advance(seconds=60)
        assert watchdog.check() is None
        clock.advance(milliseconds=1)
        assert watchdog.check() == "detection"

    assert stalls == ["detection"]


def test_default_on_stall_logs_dumps_and_exits_70(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[Any] = []
    monkeypatch.setattr(faulthandler, "dump_traceback", lambda **kw: events.append(("dump", kw)))
    monkeypatch.setattr(os, "_exit", lambda code: events.append(("exit", code)))
    caplog.set_level(logging.DEBUG, logger=SUPERVISION_LOGGER)

    supervision.default_on_stall("detection")

    critical = [r for r in _records(caplog, SUPERVISION_LOGGER) if r.levelno == logging.CRITICAL]
    assert [r.getMessage() for r in critical] == [
        "worker loop detection made no progress for over 60 s or ended; exiting for a restart"
    ]
    [(dump, kwargs), exit_call] = events
    assert dump == "dump"
    assert kwargs["all_threads"] is True
    assert "file" in kwargs
    assert exit_call == ("exit", 70)
    assert supervision.EXIT_STALL == 70


def test_default_on_stall_exits_even_when_the_stack_dump_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []

    def broken_dump(**kwargs: Any) -> None:
        raise ValueError("sys.stderr has no file descriptor")

    monkeypatch.setattr(faulthandler, "dump_traceback", broken_dump)
    monkeypatch.setattr(os, "_exit", exits.append)

    supervision.default_on_stall("telegram-io")

    assert exits == [70]


# The health file (OPS-05)


def test_health_file_touch_creates_and_refreshes(tmp_path: Path) -> None:
    health = supervision.HealthFile(tmp_path / "health")

    health.touch()
    assert health.path.exists()

    os.utime(health.path, (1, 1))  # as if the last touch was long ago
    health.touch()
    assert health.path.stat().st_mtime > 1

    # The compose healthcheck reads this fixed path inside the worker container (02-10).
    assert supervision.HEALTH_FILE == Path("/tmp/powermon-worker.health")  # noqa: S108
    assert supervision.HealthFile().path == supervision.HEALTH_FILE


def test_health_file_that_cannot_be_written_warns_once_per_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=SUPERVISION_LOGGER)
    health = supervision.HealthFile(tmp_path / "missing" / "health")

    health.touch()
    health.touch()

    assert not health.path.exists()
    warnings = [r.getMessage() for r in _records(caplog, SUPERVISION_LOGGER)]
    assert warnings == [f"worker health file {health.path} cannot be written (FileNotFoundError)"]

    # Once it works again, a later failure is a new run and warns again.
    health.path.parent.mkdir()
    health.touch()
    health.path.unlink()
    health.path.parent.rmdir()
    health.touch()
    assert len(_records(caplog, SUPERVISION_LOGGER)) == 2


# The DB-outage log (D-16)


def test_db_outage_log_warns_once_and_reports_recovery(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock(T0)
    caplog.set_level(logging.DEBUG, logger=OUTAGE_LOGGER)
    outage = supervision.DbOutageLog(logging.getLogger(OUTAGE_LOGGER), "detection", clock)
    error = OperationalError('connection to server at "db" failed: password=hunter2')

    outage.ok()  # nothing failed: nothing to report
    outage.failed(error)
    outage.failed(error)
    clock.advance(seconds=37)
    outage.ok()
    outage.ok()

    lines = [(r.levelno, r.getMessage(), r.exc_info) for r in _records(caplog, OUTAGE_LOGGER)]
    assert lines == [
        (logging.WARNING, "detection: database unreachable (OperationalError); retrying", None),
        (logging.WARNING, "detection: database reachable again after 37 s", None),
    ]
    assert "hunter2" not in caplog.text

    # A second outage is a new run of failures.
    outage.failed(error)
    assert len(_records(caplog, OUTAGE_LOGGER)) == 3
