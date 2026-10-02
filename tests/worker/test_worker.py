"""The worker process heals itself: loops always on, lease states, watchdog (D-15, D-16).

``run_worker.serve`` starts both loops at once, and they always run. Only the detection
loop calls ``lease.ensure_held()``; the Telegram I/O loop reads ``lease.current()``. While
the lease is HELD, each new generation opens a fresh detection window and turns sends left
in "sending" into "uncertain", and then the loops detect and deliver. A standby never
writes or sends, and an unreachable database is an idle iteration that still stamps
progress (MON-04, INV-13). The main thread is the watchdog: a loop stuck inside a call, or
a loop thread that died, runs the stall action, which tests inject instead of the default
exit 70 (OPS-05, INV-13 #3). After a terminated session, the lease is reacquired in process,
and every worker DB entry point starts with ``close_old_connections()``, so a dead Django
connection is replaced before its next statement (MON-06).

Time comes from ``FakeClock``: tests advance it before every expected new generation, and
assert the detection window (``detection_resumed_at`` = clock.now()) for generation 1 only,
because 02-06's lapse carve opens a later window only when the clock moved past its cursor.
run_worker reaches detection and the relay through their modules, so tests patch module
attributes only (``detection.run_cycle``, ``io_loop.run_iteration``, ``run_worker.io_thread``,
``run_worker.serve``), never a name imported into run_worker. 02-06's ``run_detection`` calls
the module-level ``run_cycle``, so these patches keep working there.

Thread and lease hygiene: every test that touches the database from a worker thread is
``django_db(transaction=True)``. Every Lease a test opens is closed, and every serve thread
is stopped, in a ``finally`` block or a fixture, or pytest-django cannot drop the test
database. Every serve under test gets an injected stall action, so no test can reach the
real ``os._exit``.
"""

import logging
import os
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    Actor,
    FakeClock,
    terminate_backends,
    wait_for,
)
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, connections, transaction

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.engine import transitions
from powermon.engine.models import LocationState, SystemState
from powermon.worker import detection, io_loop, supervision
from powermon.worker import lease as lease_module
from powermon.worker.lease import Lease
from powermon.worker.management.commands import run_worker

T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
OTHER_BOT_TOKEN = "987654321:" + "B" * 35
OTHER_CHAT_ID = -1009876543210
LOOP_NAMES = {"detection", "telegram-io"}
FAST = {"check_interval": 0.05, "detection_interval": 0.05, "io_idle_wait": 0.05}
WORKER_LOGGER = run_worker.__name__
OPS_LOGGER = "powermon.alerts.ops"
LEASE_APPLICATION_NAME = "powermon-worker-lease"
OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
OPS_WARNING = (
    "ops chat not configured (OPS_BOT_TOKEN, OPS_CHAT_ID unset): ops notices go to this log only"
)


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


def _status(message: OutboxMessage) -> tuple[str, int]:
    row = OutboxMessage.objects.get(pk=message.pk)
    return row.status, row.attempts


def _on_since(location_factory: Callable[..., Any], at: datetime, **overrides: Any) -> Any:
    """A location whose first heartbeat arrived at ``at``: it is on, with period 60 s."""
    location = location_factory(**overrides)
    transitions.record_heartbeat(location.pk, at)
    return location


def _unreachable() -> dict[str, Any]:
    """The test database's settings, pointed at a port where nothing listens."""
    return {**connection.settings_dict, "HOST": "127.0.0.1", "PORT": 1}


def _terminate(pid: int) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
        row = cur.fetchone()
    return bool(row and row[0])


def _kill_my_session() -> None:
    """End this thread's own DB session from another one, as a DB restart would.

    The session dies while idle: Django only finds out at its next statement, unless
    ``close_old_connections()`` health-checks and replaces it first.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        row = cur.fetchone()
    assert row is not None
    killer = Actor(lambda: _terminate(row[0]))
    killer.start()
    killer.join(10)
    assert killer.exc is None
    assert killer.result is True


class _CountingHealth(supervision.HealthFile):
    """A health file that also counts its touches."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.touches = 0

    def touch(self) -> None:
        super().touch()
        self.touches += 1


class _WatchdogGate:
    """Records serve's watchdog checks, and ends them before the test stops serve.

    A check that starts just before ``stop.set()`` can find a loop thread that already
    returned because of that stop, take it for a dead loop and run the stall action, so
    serve returns 70 instead of 0. ``quiesce()`` closes that window: it waits for a check
    in flight, and every later check returns None without looking at the loops. Each check
    made before it is recorded as (the clock's monotonic time, what it found).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
        self.checks: list[tuple[float, str | None]] = []
        self._quiet = threading.Event()
        self._lock = threading.Lock()
        real_check = supervision.Watchdog.check
        gate = self

        def check(watchdog: supervision.Watchdog) -> str | None:
            with gate._lock:
                if gate._quiet.is_set():
                    return None
                at = clock.monotonic()
                found = real_check(watchdog)
                gate.checks.append((at, found))
                return found

        monkeypatch.setattr(supervision.Watchdog, "check", check)

    def checked_at(self, at: float) -> bool:
        """True once a check ran with the clock at ``at``."""
        return any(when == at for when, _ in list(self.checks))

    def quiesce(self) -> None:
        self._quiet.set()
        with self._lock:
            pass


class _StopAsTheCheckWaitEnds(threading.Event):
    """serve's stop event, for a SIGTERM that lands just as the watchdog's wait times out.

    Once ``armed`` is set, the next ``wait`` of serve's own thread sets the stop, waits
    until both loop threads have returned because of it, and then answers False, as a
    wait that ended a moment before the stop would. serve then runs a watchdog check on
    loops that ended normally (E1). Every other wait, and every loop thread's, is the
    plain event's.
    """

    def __init__(self) -> None:
        super().__init__()
        self.armed = threading.Event()
        self.raced = threading.Event()
        self.loops_ended = False

    def wait(self, timeout: float | None = None) -> bool:
        serving = threading.current_thread().name == "serve-under-test"
        if serving and self.armed.is_set() and not self.raced.is_set():
            self.raced.set()
            self.set()
            self.loops_ended = wait_for(lambda: _loop_threads() == set())
            return False
        return super().wait(timeout)


class _Serve:
    """``run_worker.serve`` in a thread, with fast intervals and an injected stall action.

    With a ``gate``, ``finish()`` ends the watchdog's checks before it stops serve. A
    ``stop`` replaces the plain stop event.
    """

    def __init__(
        self,
        lease: Lease,
        clock: FakeClock,
        health: supervision.HealthFile,
        on_stall: Callable[[str], None] | None = None,
        gate: _WatchdogGate | None = None,
        stop: threading.Event | None = None,
        **overrides: float,
    ) -> None:
        self.stop = threading.Event() if stop is None else stop
        self.code: int | None = None
        self.stalls: list[str] = []
        self.gate = gate
        action = self.stalls.append if on_stall is None else on_stall
        intervals = {**FAST, **overrides}

        def target() -> None:
            self.code = run_worker.serve(
                self.stop, clock, lease, health=health, on_stall=action, **intervals
            )

        self.thread = threading.Thread(target=target, name="serve-under-test")
        self.thread.start()

    def finish(self, timeout: float = 10.0) -> int | None:
        if self.gate is not None:
            self.gate.quiesce()
        self.stop.set()
        self.thread.join(timeout)
        return self.code


@pytest.fixture
def leases() -> Iterator[Callable[..., Lease]]:
    """``new(clock=None) -> Lease`` on the test database; every lease is closed afterwards."""
    made: list[Lease] = []

    def new(clock: FakeClock | None = None) -> Lease:
        lease = Lease(connection.settings_dict, clock)
        made.append(lease)
        return lease

    yield new
    for lease in made:
        lease.close()


@pytest.fixture
def no_ops_chat(settings: Any) -> Any:
    """``settings.CFG`` with no ops chat, whatever the env file says (D-09)."""
    settings.CFG = replace(settings.CFG, ops_bot_token="", ops_chat_id=None)
    return settings


@pytest.fixture
def worker_command(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """For ``call_command("run_worker")`` past its build-mode check.

    The command rewrites, in place, the settings dict that every thread's connection
    shares (``apply_worker_db_settings``). Both keys it replaces are put back after the
    test, and every connection is closed, so no connection opened with the worker options
    outlives the test and no later test connects as powermon-worker.
    """
    monkeypatch.setitem(connection.settings_dict, "OPTIONS", connection.settings_dict["OPTIONS"])
    monkeypatch.setitem(
        connection.settings_dict, "CONN_MAX_AGE", connection.settings_dict["CONN_MAX_AGE"]
    )
    yield
    connections.close_all()


# serve: both loops always run, act only while HELD (D-15, MON-04)


@pytest.mark.django_db(transaction=True)
def test_standby_runs_both_loops_but_never_writes_or_sends(
    leases: Callable[..., Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    tmp_path: Path,
) -> None:
    holder = leases()
    assert holder.ensure_held().state == "held"
    before = T0 - timedelta(days=1)
    _resume(before)
    interrupted = _queue_off(location_factory())
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)
    waiting = _queue_off(location_factory(bot_token=OTHER_BOT_TOKEN, chat_id=OTHER_CHAT_ID))
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OTHER_BOT_TOKEN)
    health = _CountingHealth(tmp_path / "health")
    clock = FakeClock(T0 + timedelta(minutes=1))
    standby = leases(clock)

    serving = _Serve(standby, clock, health)
    try:
        assert wait_for(lambda: _loop_threads() == LOOP_NAMES)
        assert wait_for(lambda: health.touches >= 5)  # several standby cycles
        assert standby.current().state == "standby"
        assert _resumed_at() == before
        assert _status(interrupted) == ("sending", 1)
        assert _status(waiting) == ("pending", 0)
        assert len(fake_telegram.calls) == 0
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert _loop_threads() == set()
    assert health.path.exists()


@pytest.mark.django_db(transaction=True)
def test_serve_delivers_queued_alerts_then_stops(
    leases: Callable[..., Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    tmp_path: Path,
) -> None:
    _resume(None)
    row = _queue_off(location_factory())
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    clock = FakeClock(T0 + timedelta(minutes=1))
    lease = leases(clock)

    serving = _Serve(lease, clock, supervision.HealthFile(tmp_path / "health"))
    try:
        assert wait_for(lambda: len(fake_telegram.sent) == 1)
        assert wait_for(lambda: _loop_threads() == LOOP_NAMES)
        assert _resumed_at() == clock.now()
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert _loop_threads() == set()
    assert fake_telegram.sent == [
        {"chat_id": DEFAULT_CHAT_ID, "text": OFF_EN, "parse_mode": "HTML"}
    ]
    assert _status(row) == ("sent", 1)
    assert lease.pid is None
    # The lock is free again for the next worker.
    successor = leases()
    assert successor.ensure_held().state == "held"


@pytest.mark.django_db(transaction=True)
def test_new_generation_opens_a_window_and_recovers_interrupted_sends(
    leases: Callable[..., Lease],
    location_factory: Callable[..., Any],
    no_ops_chat: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _resume(T0 - timedelta(days=1))
    interrupted = _queue_off(location_factory())
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)
    caplog.set_level(logging.INFO)
    clock = FakeClock(T0 + timedelta(minutes=1))
    lease = leases(clock)

    serving = _Serve(lease, clock, supervision.HealthFile(tmp_path / "health"))
    try:
        assert wait_for(lambda: _resumed_at() == clock.now())
        assert wait_for(lambda: _status(interrupted)[0] == "uncertain")
        assert wait_for(lambda: "relay activated" in caplog.text)
    finally:
        code = serving.finish()

    assert code == 0
    assert lease.current().generation == 1
    row = OutboxMessage.objects.get(pk=interrupted.pk)
    assert (row.status, row.last_error) == ("uncertain", "interrupted")
    ops_lines = [r.getMessage() for r in caplog.records if r.name == OPS_LOGGER]
    assert len(ops_lines) == 1
    assert "(the worker stopped while sending it)" in ops_lines[0]
    worker = [r.getMessage() for r in caplog.records if r.name == WORKER_LOGGER]
    assert worker.count(f"worker active since {clock.now().isoformat()} (generation 1)") == 1
    relay = [r.getMessage() for r in caplog.records if r.name == io_loop.__name__]
    assert relay == ["relay activated: 1 interrupted send(s) marked uncertain"]


@pytest.mark.django_db(transaction=True)
def test_lease_loss_is_reacquired_in_process_without_exit(
    leases: Callable[..., Lease], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _resume(None)
    clock = FakeClock(T0)
    caplog.set_level(logging.INFO)
    lease = leases(clock)

    serving = _Serve(lease, clock, supervision.HealthFile(tmp_path / "health"))
    try:
        assert wait_for(lambda: lease.current().generation == 1)
        assert wait_for(lambda: _resumed_at() == clock.now())

        clock.advance(seconds=10)
        # A database restart or a network drop, as seen from the lease session.
        assert terminate_backends(LEASE_APPLICATION_NAME) == 1

        assert wait_for(lambda: lease.current().generation == 2)
        assert lease.current().state == "held"
        assert serving.thread.is_alive()
        assert _loop_threads() == LOOP_NAMES
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert [r for r in caplog.records if r.levelno >= logging.CRITICAL] == []
    assert "worker lock: lease session lost (AdminShutdown)" in caplog.text


# A dead Django connection is replaced before the next statement (MON-06, D-16)


@pytest.mark.django_db(transaction=True)
def test_io_activation_and_pass_replace_a_dead_connection(
    location_factory: Callable[..., Any], fake_telegram: Any, no_ops_chat: Any
) -> None:
    clock = FakeClock(T0 + timedelta(minutes=1))
    interrupted = _queue_off(location_factory())
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)

    try:
        _kill_my_session()
        assert io_loop.activate(io_loop.RelayState(), clock) == 1
        assert _status(interrupted) == ("uncertain", 1)

        queued = _queue_off(location_factory(bot_token=OTHER_BOT_TOKEN, chat_id=OTHER_CHAT_ID))
        fake_telegram.accept(OTHER_BOT_TOKEN)
        _kill_my_session()
        assert io_loop.run_iteration(clock, io_loop.RelayState()) is True
    finally:
        # On a failure, never leave this thread's dead session to the teardown flush.
        connection.close()
    assert fake_telegram.sent == [{"chat_id": OTHER_CHAT_ID, "text": OFF_EN, "parse_mode": "HTML"}]
    assert _status(queued) == ("sent", 1)


# The watchdog sees a stuck or dead loop (INV-13 #3, OPS-05)


@pytest.mark.django_db(transaction=True)
def test_INV13_loop_blocked_in_a_call_exits_through_the_stall_action(
    leases: Callable[..., Lease], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resume(None)
    entered, release = threading.Event(), threading.Event()

    def stuck_cycle(now: datetime, tick: Callable[[], None] | None = None) -> int:
        entered.set()
        release.wait(30)  # a call that does not return until the stall action ends it
        return 0

    monkeypatch.setattr(detection, "run_cycle", stuck_cycle)
    checks: list[str | None] = []
    real_check = supervision.Watchdog.check

    def counted_check(self: supervision.Watchdog) -> str | None:
        found = real_check(self)
        checks.append(found)
        return found

    monkeypatch.setattr(supervision.Watchdog, "check", counted_check)
    stalls: list[str] = []

    def on_stall(name: str) -> None:
        stalls.append(name)
        release.set()

    clock = FakeClock(T0)
    serving = _Serve(
        leases(clock),
        clock,
        supervision.HealthFile(tmp_path / "health"),
        on_stall=on_stall,
        check_interval=0.05,
    )
    try:
        assert entered.wait(5)
        clock.advance(seconds=59)
        seen = len(checks)
        assert wait_for(lambda: len(checks) >= seen + 3)
        assert stalls == []

        clock.advance(seconds=2)
        serving.thread.join(10)
        assert not serving.thread.is_alive()
    finally:
        release.set()
        serving.finish()

    assert stalls == ["detection"]
    assert serving.code == run_worker.EXIT_STALL == 70
    assert serving.stop.is_set()
    assert _loop_threads() == set()


@pytest.mark.django_db(transaction=True)
def test_a_dead_loop_goes_through_the_watchdog(
    leases: Callable[..., Lease], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resume(None)

    def broken_io_thread(*args: Any) -> None:
        return  # the thread ends at once, as if an error escaped its loop

    monkeypatch.setattr(run_worker, "io_thread", broken_io_thread)

    serving = _Serve(leases(), FakeClock(T0), supervision.HealthFile(tmp_path / "health"))
    try:
        serving.thread.join(10)
        assert not serving.thread.is_alive()
    finally:
        serving.finish()

    assert serving.stalls == ["telegram-io"]
    assert serving.code == 70
    assert _loop_threads() == set()


@pytest.mark.django_db(transaction=True)
def test_E1_a_check_after_the_loops_stopped_for_sigterm_finds_no_stall_and_serve_exits_0(
    leases: Callable[..., Lease], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # E1 (final audit): a watchdog check that runs after a SIGTERM's stop, with both loop
    # threads already returned because of it, is a clean stop: exit 0, no stall action.
    _resume(None)
    stop = _StopAsTheCheckWaitEnds()
    after_the_stop: list[str | None] = []
    real_check = supervision.Watchdog.check

    def check(watchdog: supervision.Watchdog) -> str | None:
        found = real_check(watchdog)
        if stop.raced.is_set():
            after_the_stop.append(found)
        return found

    monkeypatch.setattr(supervision.Watchdog, "check", check)
    clock = FakeClock(T0)
    serving = _Serve(leases(clock), clock, supervision.HealthFile(tmp_path / "health"), stop=stop)
    try:
        assert wait_for(lambda: _loop_threads() == LOOP_NAMES)
        stop.armed.set()
        serving.thread.join(10)
        assert not serving.thread.is_alive()
    finally:
        code = serving.finish()

    assert stop.raced.is_set() and stop.loops_ended
    # The check ran on the ended loops, and found nothing.
    assert after_the_stop == [None]
    assert serving.stalls == []
    assert code == 0
    assert _loop_threads() == set()


def test_db_down_iterations_stamp_progress_but_not_the_health_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No django_db mark: with the database down nothing may touch Django's connection, and
    # an attempt would be logged as an error by the loop (asserted below).
    clock = FakeClock(T0)
    calls: list[float] = []

    class ObservedLease(Lease):
        def ensure_held(self) -> Any:
            status = super().ensure_held()
            calls.append(clock.monotonic())
            return status

    lease = ObservedLease(_unreachable(), clock)
    health = _CountingHealth(tmp_path / "health")
    caplog.set_level(logging.INFO)
    gate = _WatchdogGate(monkeypatch, clock)

    serving = _Serve(lease, clock, health, gate=gate)
    try:
        # The watchdog exists and counts from 0 before the clock moves, and the detection
        # loop has asked the lease once.
        assert wait_for(lambda: gate.checked_at(0.0) and bool(calls))
        # 70 s in 10 s steps, past the 60 s detection limit: every step sees a new cycle,
        # which stamped progress just before it asked the lease.
        for _ in range(7):
            clock.advance(seconds=10)
            target = clock.monotonic()
            assert wait_for(lambda target=target: bool(calls) and calls[-1] == target), (
                serving.stalls
            )
        # A check 70 s after the watchdog started finds no stalled loop. Without the
        # DB-down stamps it would find detection stale (70 s > 60 s) and stall.
        assert wait_for(lambda: gate.checked_at(clock.monotonic())), serving.stalls
        assert serving.stalls == []
        assert lease.current().state == "db_down"
        assert serving.thread.is_alive()
    finally:
        code = serving.finish()
        lease.close()

    assert code == 0
    assert serving.stalls == []
    assert {found for _, found in gate.checks} == {None}
    assert health.touches == 0
    assert not health.path.exists()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.django_db(transaction=True)
def test_detection_db_error_logs_one_warning_not_a_traceback_per_cycle(
    leases: Callable[..., Lease],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _resume(None)
    failing = threading.Event()
    failing.set()
    calls: list[datetime] = []

    def cycle(now: datetime, tick: Callable[[], None] | None = None) -> int:
        calls.append(now)
        if failing.is_set():
            raise OperationalError("server closed the connection unexpectedly")
        return 0

    monkeypatch.setattr(detection, "run_cycle", cycle)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)
    clock = FakeClock(T0)
    lease = leases(clock)
    health = _CountingHealth(tmp_path / "health")
    stop = threading.Event()
    thread = threading.Thread(
        target=run_worker.detection_loop,
        args=(stop, clock, 0.02, lease, supervision.Progress(clock), health),
    )
    thread.start()
    try:
        assert wait_for(lambda: len(calls) >= 5)
        touches_while_failing = health.touches
        failing.clear()
        seen = len(calls)
        assert wait_for(lambda: len(calls) >= seen + 2)
    finally:
        stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert touches_while_failing == 0
    assert health.touches > 0
    lines = [r for r in caplog.records if r.name == WORKER_LOGGER and r.levelno >= logging.WARNING]
    assert [(r.levelno, r.getMessage(), r.exc_info) for r in lines] == [
        (logging.WARNING, "detection: database unreachable (OperationalError); retrying", None),
        (logging.WARNING, "detection: database reachable again after 0 s", None),
    ]


# The loop bodies: progress ticks and the dead-connection abort (D-15, INV-13 #4)


@pytest.mark.django_db(transaction=True)
def test_run_cycle_ticks_after_every_location_and_aborts_on_a_dead_connection(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _resume(None)
    first, second, third = (_on_since(location_factory, T0) for _ in range(3))
    ticks: list[int] = []

    def tick() -> None:
        ticks.append(1)

    # Every monitored location ticks, whether or not it turns OFF.
    assert detection.run_cycle(T0 + timedelta(seconds=10), tick=tick) == 0
    assert len(ticks) == 3

    # A database error on a connection that still works (a statement timeout) stays one
    # location's problem: it is logged and the cycle goes on.
    real_mark_off = transitions.mark_off

    def timing_out(snap: Any, d: Any, now: datetime, alerts_enabled: bool) -> bool:
        if snap.location_id == first.pk:
            raise OperationalError("canceling statement due to statement timeout")
        return real_mark_off(snap, d, now, alerts_enabled)

    monkeypatch.setattr(transitions, "mark_off", timing_out)
    caplog.set_level(logging.ERROR, logger=detection.__name__)
    ticks.clear()
    assert detection.run_cycle(T0 + timedelta(minutes=2), tick=tick) == 2
    assert len(ticks) == 3
    assert [r.getMessage() for r in caplog.records] == [f"detection failed for location {first.pk}"]
    assert [s.status for s in LocationState.objects.order_by("pk")] == ["on", "off", "off"]

    # A dead connection aborts the whole cycle: every later location would fail the same
    # way, so the loop logs one WARNING instead of one traceback per location.
    fourth = _on_since(location_factory, T0)

    def dying(snap: Any, d: Any, now: datetime, alerts_enabled: bool) -> bool:
        _kill_my_session()
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        return True

    monkeypatch.setattr(transitions, "mark_off", dying)
    ticks.clear()
    try:
        with pytest.raises(OperationalError):
            detection.run_cycle(T0 + timedelta(minutes=3), tick=tick)
    finally:
        connection.close()  # this thread's session is dead; let the next query reconnect

    assert len(ticks) == 1
    assert len(caplog.records) == 1
    assert LocationState.objects.get(pk=fourth.pk).status == "on"
    assert {second.pk, third.pk} == set(
        LocationState.objects.filter(status="off").values_list("pk", flat=True)
    )


@pytest.mark.django_db(transaction=True)
def test_run_iteration_ticks_after_every_row(
    location_factory: Callable[..., Any], fake_telegram: Any, no_ops_chat: Any
) -> None:
    sent = _queue_off(location_factory())
    not_due = _queue_off(
        location_factory(bot_token=OTHER_BOT_TOKEN, chat_id=OTHER_CHAT_ID),
        at=T0 + timedelta(hours=1),
    )
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    ticks: list[int] = []

    busy = io_loop.run_iteration(
        FakeClock(T0 + timedelta(minutes=1)), io_loop.RelayState(), tick=lambda: ticks.append(1)
    )

    assert busy is True
    # One tick per head (sent or skipped), one for the ops step.
    assert len(ticks) == 3
    assert (_status(sent), _status(not_due)) == (("sent", 1), ("pending", 0))


# The loops keep going (INV-13), and stop on request (INV-15)


@pytest.mark.django_db(transaction=True)
def test_loops_survive_a_failing_iteration(
    leases: Callable[..., Lease],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _resume(None)
    calls = {"cycle": 0, "relay": 0}

    def failing_cycle(now: datetime, tick: Callable[[], None] | None = None) -> int:
        calls["cycle"] += 1
        raise RuntimeError("cycle failed")

    def failing_relay(clock: Any, state: Any, stop: Any = None, tick: Any = None) -> bool:
        calls["relay"] += 1
        raise RuntimeError("relay failed")

    monkeypatch.setattr(detection, "run_cycle", failing_cycle)
    monkeypatch.setattr(io_loop, "run_iteration", failing_relay)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)
    stop = threading.Event()
    clock = FakeClock(T0)
    lease = leases(clock)
    progress = supervision.Progress(clock)
    health = supervision.HealthFile(tmp_path / "health")
    threads = [
        threading.Thread(
            target=run_worker.detection_loop, args=(stop, clock, 0.02, lease, progress, health)
        ),
        threading.Thread(target=run_worker.io_thread, args=(stop, clock, 0.02, lease, progress)),
    ]
    for t in threads:
        t.start()
    try:
        assert wait_for(lambda: calls["cycle"] >= 3 and calls["relay"] >= 3)
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
    leases: Callable[..., Lease], location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # SIGTERM lands while the first location's alert is in flight: the thread finishes that
    # send, records it, claims nothing more and returns. The other alert stays pending for
    # the next worker instead of being cut off mid-send at exit.
    first = _queue_off(location_factory())
    second = _queue_off(location_factory(bot_token=OTHER_BOT_TOKEN, chat_id=OTHER_CHAT_ID))
    stop = threading.Event()
    fake_telegram.answer(DEFAULT_BOT_TOKEN, stop.set)
    fake_telegram.accept(OTHER_BOT_TOKEN)
    clock = FakeClock(T0 + timedelta(minutes=1))
    lease = leases(clock)
    assert lease.ensure_held().state == "held"

    run_worker.io_thread(stop, clock, 0.01, lease, supervision.Progress(clock))

    assert _status(first) == ("sent", 1)
    assert _status(second) == ("pending", 0)
    assert len(fake_telegram.calls) == 1


# The worker's DB sessions (D-16, RESEARCH Pitfall 3)


def test_apply_worker_db_settings() -> None:
    original = connection.settings_dict
    copy = {**original, "OPTIONS": dict(original["OPTIONS"])}
    options_before = copy["OPTIONS"]
    replaced = {"options", "application_name"}

    run_worker.apply_worker_db_settings(copy)

    # Persistent connections: a recycled connection would look like a reconnect (02-06).
    assert copy["CONN_MAX_AGE"] is None
    assert copy["CONN_HEALTH_CHECKS"] is True
    assert copy["OPTIONS"]["options"] == django_settings.WORKER_PG_OPTIONS
    assert copy["OPTIONS"]["application_name"] == "powermon-worker"
    assert run_worker.WORKER_APPLICATION_NAME == "powermon-worker"
    kept = {k: v for k, v in copy["OPTIONS"].items() if k not in replaced}
    assert kept == {k: v for k, v in original["OPTIONS"].items() if k not in replaced}
    assert kept["tcp_user_timeout"] == 10000
    # OPTIONS is replaced by a copy, never changed in place.
    assert copy["OPTIONS"] is not options_before
    assert options_before == original["OPTIONS"]


# The command


def test_run_worker_refuses_build_mode(settings: Any) -> None:
    settings.CFG = replace(settings.CFG, build=True)

    with pytest.raises(CommandError, match="build mode"):
        call_command("run_worker")


def test_run_worker_stops_on_sigterm_and_exits_0(
    worker_command: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers: dict[int, Any] = {}
    seen: dict[str, Any] = {}
    exits: list[int] = []

    def fake_serve(stop: threading.Event, clock: Any, lease: Any, **kwargs: Any) -> int:
        # Docker sends SIGTERM: the handler only sets the stop event.
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        seen.update(
            stopped=stop.is_set(),
            lease=lease,
            max_age=connection.settings_dict["CONN_MAX_AGE"],
            options=dict(connection.settings_dict["OPTIONS"]),
        )
        return 0

    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(run_worker, "serve", fake_serve)
    monkeypatch.setattr(os, "_exit", exits.append)

    call_command("run_worker")

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert seen["stopped"] is True
    assert isinstance(seen["lease"], Lease)
    # The container-local record of the last held cycle (WR-02): /tmp survives a restart
    # of the worker container and is never shared with another container.
    assert seen["lease"].held_marker == lease_module.HELD_MARKER
    assert lease_module.HELD_MARKER == Path("/tmp/powermon-worker.held")  # noqa: S108
    # The worker's session settings are in place before serve opens any connection.
    assert seen["max_age"] is None
    assert seen["options"]["options"] == django_settings.WORKER_PG_OPTIONS
    assert seen["options"]["application_name"] == "powermon-worker"
    assert exits == []


def test_run_worker_enables_charts(worker_command: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The production worker runs the chart lifecycle; serve's default (every test above
    # and every Phase 2 test) keeps it off (D-05, Pitfall 4).
    captured: dict[str, Any] = {}
    exits: list[int] = []

    def fake_serve(stop: threading.Event, clock: Any, lease: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(run_worker, "serve", fake_serve)
    monkeypatch.setattr(os, "_exit", exits.append)

    call_command("run_worker")

    assert captured["charts"] is True
    assert exits == []


def test_run_worker_exits_with_the_code_serve_returns(
    worker_command: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(run_worker, "serve", lambda *args, **kwargs: 70)
    monkeypatch.setattr(os, "_exit", exits.append)

    call_command("run_worker")

    assert exits == [70]


def test_run_worker_warns_once_when_the_ops_chat_is_not_configured(
    worker_command: None,
    no_ops_chat: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(run_worker, "serve", lambda *args, **kwargs: 0)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)

    call_command("run_worker")

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == WORKER_LOGGER and r.levelno == logging.WARNING
    ]
    assert warnings == [OPS_WARNING]


def test_run_worker_says_nothing_about_a_configured_ops_chat(
    worker_command: None,
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(run_worker, "serve", lambda *args, **kwargs: 0)
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)

    call_command("run_worker")

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
