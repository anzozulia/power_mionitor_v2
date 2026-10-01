"""The single active worker: lock lease, standby, activation, loops, exit code 3 (D-14, D-18).

Only one worker runs detection and delivery. It holds a PostgreSQL session advisory lock
on its own psycopg connection, outside Django's connections (Pitfall 2). A second worker
polls in standby and never runs a loop. On activation the worker opens a fresh detection
window and turns interrupted sends into "uncertain". If the lock session dies or a loop
thread dies, ``serve`` returns 3 and the command exits with it, so Docker restarts the
process (D-18).

Every test that touches the database from a worker thread is
``django_db(transaction=True)``. Every Lease a test opens is closed, and every serve
thread is stopped, in a ``finally`` block, or pytest-django cannot drop the test database.
"""

import logging
import os
import re
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.core.management import CommandError, call_command
from django.db import connection, transaction

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.engine.models import SystemState
from powermon.worker import lease as lease_module
from powermon.worker.lease import LOCK_KEY, Lease
from powermon.worker.management.commands import run_worker

T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
OTHER_BOT_TOKEN = "987654321:" + "B" * 35
OTHER_CHAT_ID = -1009876543210
LOOP_NAMES = {"detection", "telegram-io"}
FAST = {"standby_poll": 0.05, "check_interval": 0.05, "detection_interval": 0.05}
WORKER_LOGGER = run_worker.__name__
LEASE_LOGGER = lease_module.__name__
UNREACHABLE = re.compile(r"worker lock: database unreachable or session lost \(\w+\); retrying")


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _loop_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.name in LOOP_NAMES and t.is_alive()}


def _resume(at: datetime | None) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": at, "web_started_at": None}
    )


def _resumed_at() -> datetime | None:
    return SystemState.objects.get(pk=1).detection_resumed_at


def _queue_off(location: Any, at: datetime = T0) -> OutboxMessage:
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - timedelta(seconds=91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


def _terminate_lease_backend() -> None:
    """Kill the worker's lock session from outside, as a DB restart or network drop would."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pid FROM pg_stat_activity "
            "WHERE application_name = 'powermon-worker-lease' AND datname = current_database()"
        )
        rows = cur.fetchall()
        assert len(rows) == 1, rows
        cur.execute("SELECT pg_terminate_backend(%s, 5000)", [rows[0][0]])
        assert cur.fetchone() == (True,)


class _Serve:
    """``run_worker.serve`` in a thread, with fast intervals, its exit code kept."""

    def __init__(self, lease: Lease, clock: FakeClock, **overrides: float) -> None:
        self.stop = threading.Event()
        self.code: int | None = None
        intervals = {**FAST, "io_idle_wait": 0.05, **overrides}

        def target() -> None:
            self.code = run_worker.serve(self.stop, clock, lease, **intervals)

        self.thread = threading.Thread(target=target, name="serve-under-test")
        self.thread.start()

    def finish(self, timeout: float = 10.0) -> int | None:
        self.stop.set()
        self.thread.join(timeout)
        return self.code


@pytest.fixture
def leases() -> Iterator[Callable[[], Lease]]:
    """``new() -> Lease`` on the test database; every lease is closed afterwards."""
    made: list[Lease] = []

    def new() -> Lease:
        lease = Lease(connection.settings_dict)
        made.append(lease)
        return lease

    yield new
    for lease in made:
        lease.close()


# The lease (Pitfall 2, D-18)


@pytest.mark.django_db(transaction=True)
def test_lease_is_exclusive_and_never_blocks(leases: Callable[[], Lease]) -> None:
    a = leases()
    b = leases()

    assert a.try_acquire() is True

    started = time.monotonic()
    assert b.try_acquire() is False
    assert time.monotonic() - started < 1.0
    assert a.alive() is True
    assert isinstance(a.pid, int)
    assert a.pid != b.pid
    # The repr names the session only: no connection details, no password.
    assert repr(a) == f"Lease(pid={a.pid})"
    assert connection.settings_dict["PASSWORD"] not in repr(a)
    a.close()
    assert a.pid is None
    assert a.alive() is False
    assert b.try_acquire() is True
    assert LOCK_KEY == 0x504F5745524D4F4E < 2**63


@pytest.mark.django_db(transaction=True)
def test_lease_not_alive_after_its_backend_is_terminated(leases: Callable[[], Lease]) -> None:
    a = leases()
    assert a.try_acquire() is True

    _terminate_lease_backend()

    assert a.alive() is False
    # The dead session released the lock: another worker can take it now.
    b = leases()
    assert b.try_acquire() is True
    # A try on the dead connection fails quietly and drops it; the next try reconnects.
    assert a.try_acquire() is False
    assert a.pid is None
    b.close()
    assert a.try_acquire() is True


def test_lease_try_acquire_returns_false_when_the_database_is_unreachable() -> None:
    # Nothing listens on port 1: the connect fails, nothing raises, the standby keeps polling.
    lease = Lease({**connection.settings_dict, "HOST": "127.0.0.1", "PORT": 1})
    try:
        assert lease.try_acquire() is False
        assert lease.pid is None
        assert lease.alive() is False
    finally:
        lease.close()


def _lease_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LEASE_LOGGER]


def test_lease_warns_once_while_the_database_stays_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Without this line the standby line would be the only output, and README section 10
    # reads it as "a second worker". The class name only: a psycopg error message can
    # carry the host, the port and the user.
    lease = Lease({**connection.settings_dict, "HOST": "127.0.0.1", "PORT": 1})
    caplog.set_level(logging.DEBUG, logger=LEASE_LOGGER)
    try:
        for _ in range(3):
            assert lease.try_acquire() is False
    finally:
        lease.close()

    lines = _lease_lines(caplog)
    assert [r.getMessage() for r in lines] == [
        "worker lock: database unreachable or session lost (OperationalError); retrying"
    ]
    assert (lines[0].levelno, lines[0].exc_info) == (logging.WARNING, None)
    assert connection.settings_dict["PASSWORD"] not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_lease_warns_again_after_the_database_answered(
    leases: Callable[[], Lease], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LEASE_LOGGER)
    a = leases()
    b = leases()
    # Taking the lock, or finding it held by another worker, is not a database problem.
    assert a.try_acquire() is True
    assert b.try_acquire() is False
    b.close()
    assert _lease_lines(caplog) == []

    _terminate_lease_backend()  # a database restart drops the lock session
    assert a.try_acquire() is False
    assert a.try_acquire() is True  # the database answers again: that run of errors ends
    _terminate_lease_backend()
    assert a.try_acquire() is False

    messages = [r.getMessage() for r in _lease_lines(caplog)]
    assert len(messages) == 2
    assert all(UNREACHABLE.fullmatch(m) for m in messages), messages


# Activation (D-14)


@pytest.mark.django_db(transaction=True)
def test_activate_opens_a_fresh_window_and_recovers_interrupted_sends(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    _resume(T0 - timedelta(days=1))
    interrupted = _queue_off(location_factory())
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)

    run_worker.activate(T0)

    assert _resumed_at() == T0
    row = OutboxMessage.objects.get(pk=interrupted.pk)
    assert (row.status, row.last_error) == ("uncertain", "interrupted")
    assert any("worker active" in r.getMessage() for r in caplog.records)


@pytest.mark.django_db(transaction=True)
def test_activate_recreates_a_missing_system_state_row() -> None:
    SystemState.objects.all().delete()

    run_worker.activate(T0)

    assert _resumed_at() == T0


# serve: standby, delivery, stop, exit code 3


@pytest.mark.django_db(transaction=True)
def test_standby_never_activates_and_stops_cleanly(
    leases: Callable[[], Lease],
    location_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    holder = leases()
    assert holder.try_acquire() is True
    before = T0 - timedelta(days=1)
    _resume(before)
    interrupted = _queue_off(location_factory())
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending")
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)
    standby = leases()

    serving = _Serve(standby, FakeClock(T0))
    try:
        assert _wait_for(lambda: "standby: waiting for the worker lock" in caplog.text)
        time.sleep(0.3)  # several more polls
        assert _resumed_at() == before
        assert OutboxMessage.objects.get(pk=interrupted.pk).status == "sending"
        assert _loop_threads() == set()
    finally:
        code = serving.finish()

    assert code == 0
    assert not serving.thread.is_alive()
    assert caplog.text.count("standby: waiting for the worker lock") == 1
    assert standby.pid is None


@pytest.mark.django_db(transaction=True)
def test_serve_delivers_queued_alerts_then_stops(
    leases: Callable[[], Lease], location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _resume(None)
    location = location_factory()
    row = _queue_off(location)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    lease = leases()
    clock = FakeClock(T0 + timedelta(minutes=1))

    serving = _Serve(lease, clock)
    try:
        assert _wait_for(lambda: len(fake_telegram.sent) == 1)
        assert _wait_for(lambda: _loop_threads() == LOOP_NAMES)
        assert _resumed_at() == clock.now()
    finally:
        code = serving.finish()

    assert code == 0
    assert _loop_threads() == set()
    assert fake_telegram.sent == [
        {
            "chat_id": DEFAULT_CHAT_ID,
            "text": "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>",
            "parse_mode": "HTML",
        }
    ]
    assert OutboxMessage.objects.get(pk=row.pk).status == "sent"
    assert lease.pid is None
    # The lock is free again for the next worker.
    successor = leases()
    assert successor.try_acquire() is True


@pytest.mark.django_db(transaction=True)
def test_lease_loss_exits_with_code_3(
    leases: Callable[[], Lease], caplog: pytest.LogCaptureFixture
) -> None:
    _resume(None)
    clock = FakeClock(T0)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)

    serving = _Serve(leases(), clock)
    try:
        assert _wait_for(lambda: _resumed_at() == clock.now())
        assert _wait_for(lambda: _loop_threads() == LOOP_NAMES)

        _terminate_lease_backend()

        serving.thread.join(2.0)
        assert not serving.thread.is_alive()
    finally:
        serving.finish()

    assert serving.code == run_worker.EXIT_LEASE_LOST == 3
    assert serving.stop.is_set()
    assert _loop_threads() == set()
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1


@pytest.mark.django_db(transaction=True)
def test_a_dead_loop_exits_with_code_3(
    leases: Callable[[], Lease], monkeypatch: pytest.MonkeyPatch
) -> None:
    _resume(None)

    def broken_io_thread(stop: threading.Event, clock: Any, idle_wait: float) -> None:
        return  # the thread ends at once, as if an error escaped its loop

    monkeypatch.setattr(run_worker, "io_thread", broken_io_thread)

    serving = _Serve(leases(), FakeClock(T0))
    try:
        serving.thread.join(2.0)
        assert not serving.thread.is_alive()
    finally:
        serving.finish()

    assert serving.code == 3
    assert _loop_threads() == set()


# The loops


@pytest.mark.django_db(transaction=True)
def test_loops_survive_a_failing_iteration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"cycle": 0, "relay": 0}

    def failing_cycle(now: datetime) -> int:
        calls["cycle"] += 1
        raise RuntimeError("cycle failed")

    def failing_relay(clock: Any, state: Any, stop: threading.Event) -> bool:
        calls["relay"] += 1
        raise RuntimeError("relay failed")

    monkeypatch.setattr(run_worker, "run_cycle", failing_cycle)
    monkeypatch.setattr(run_worker, "run_iteration", failing_relay)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)
    stop = threading.Event()
    clock = FakeClock(T0)
    threads = [
        threading.Thread(target=run_worker.detection_loop, args=(stop, clock, 0.02)),
        threading.Thread(target=run_worker.io_thread, args=(stop, clock, 0.02)),
    ]
    for t in threads:
        t.start()
    try:
        assert _wait_for(lambda: calls["cycle"] >= 3 and calls["relay"] >= 3)
        assert all(t.is_alive() for t in threads)
    finally:
        stop.set()
        for t in threads:
            t.join(5.0)

    assert not any(t.is_alive() for t in threads)
    assert "detection cycle failed" in caplog.text
    assert "telegram I/O iteration failed" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_INV15_io_thread_starts_no_new_send_after_sigterm(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # SIGTERM lands while the first location's alert is in flight: the thread finishes that
    # send, records it, claims nothing more and returns. The other alert stays pending for
    # the next worker instead of being cut off mid-send at exit.
    first = _queue_off(location_factory())
    second = _queue_off(location_factory(bot_token=OTHER_BOT_TOKEN, chat_id=OTHER_CHAT_ID))
    stop = threading.Event()
    fake_telegram.answer(DEFAULT_BOT_TOKEN, stop.set)
    fake_telegram.accept(OTHER_BOT_TOKEN)

    run_worker.io_thread(stop, FakeClock(T0 + timedelta(minutes=1)), 0.01)

    assert OutboxMessage.objects.get(pk=first.pk).status == "sent"
    row = OutboxMessage.objects.get(pk=second.pk)
    assert (row.status, row.attempts) == ("pending", 0)
    assert len(fake_telegram.calls) == 1


# The command


def test_run_worker_refuses_build_mode(settings: Any) -> None:
    settings.CFG = replace(settings.CFG, build=True)

    with pytest.raises(CommandError, match="build mode"):
        call_command("run_worker")


def test_run_worker_stops_on_sigterm_and_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Any] = {}
    seen: dict[str, Any] = {}
    exits: list[int] = []

    def fake_serve(stop: threading.Event, clock: Any, lease: Any, **kwargs: Any) -> int:
        # Docker sends SIGTERM: the handler only sets the stop event.
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        seen.update(stopped=stop.is_set(), lease=lease)
        return 0

    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(run_worker, "serve", fake_serve)
    monkeypatch.setattr(os, "_exit", exits.append)

    call_command("run_worker")

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert seen["stopped"] is True
    assert isinstance(seen["lease"], Lease)
    assert exits == []


def test_run_worker_exits_with_the_code_serve_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(run_worker, "serve", lambda *args, **kwargs: 3)
    monkeypatch.setattr(os, "_exit", exits.append)

    call_command("run_worker")

    assert exits == [3]
