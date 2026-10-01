"""The running worker records every lapse once and keeps detecting (MON-06, D-04; INV-13 #1).

This is the in-process half of INV-13. A dropped lease session, a dropped Django session,
or both (what a database restart does to the worker) are simulated with
``pg_terminate_backend`` on real PostgreSQL. The worker must reacquire the lease or
reconnect by itself, record the missed window as one monitoring gap (one incident, one
notice), and go on detecting: a location that times out afterwards is alerted, and both
the OFF and the gap notice are delivered by the same process, so the I/O thread replaced
its dead connection too. No exit, no stall, no manual action. Restarting the real
containers (the database, the worker) is a manual check in 02-09, never a test harness
here (docs/v1-lessons.md section 1).

Timing rule. The ``FakeClock`` is shared by everything in ``serve``: ``advance`` also
moves the monotonic time that the watchdog's 60 s detection limit and the 15 s lapse
rule read. One large advance would therefore make the next cycle carve a gap by itself
(an extra incident, and ``detection_resumed_at = now``, so no OFF follows), or leave the
detection stamp more than 60 s old at a watchdog check. So the serve-level tests move
time only through ``_step_clock``:

- steps of at most STEP_S (10 s), always with ``advance``, never ``set``;
- before each step, both loops have stamped progress since the previous step and (with
  ``cycles``) a detection cycle that entered after the previous step has returned, so
  completed cycles are at most 10 s of fake time apart (never over the 15 s threshold);
- the step is taken while the detection loop is held between two cycles: the ``seen``
  fixture can hold the loop at the top of its next iteration, before it asks the lease or
  reads the clock. ``before_step`` (first step only) runs inside that hold, right before
  the clock moves, so the first cycle that sees the new time is also the first after
  ``before_step`` (a termination), and only the forced carve triggers can carve then. A
  slow termination cannot let a cycle in at the old time.

serve runs with detection_interval 1.0 s and io_idle_wait 0.05 s; a location that must
time out has ``period_s=10, grace_s=10`` (timeout 20 s, the model minimum), so three steps
after a carve pass its timeout. Every serve gets an injected stall recorder, so no test
can reach the real ``os._exit``, and asserts it was never called. 02-08 reuses
``_step_clock`` (with ``cycles=False``) for its I/O-thread case.

Hygiene: every test that touches the database from another thread is
``django_db(transaction=True)``; every Lease is closed and every serve stopped in a
fixture or ``finally``; a thread whose own session was killed closes its connection.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    OPS_CHAT_ID,
    Actor,
    FakeClock,
    terminate_backends,
    wait_for,
)
from django.db import IntegrityError, OperationalError, connection

from powermon.alerts import outbox, texts
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import lapse, timeline, transitions
from powermon.engine.models import PowerInterval, SystemState
from powermon.worker import detection, supervision
from powermon.worker.lease import Lease
from powermon.worker.management.commands import run_worker

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
WORKER = run_worker.WORKER_APPLICATION_NAME
LEASE = "powermon-worker-lease"
STEP_S = 10.0
# Real seconds for one awaited event (a cycle is at most 1 s of real time apart).
WAIT_S = 15.0
SERVE_INTERVALS = {"check_interval": 0.05, "detection_interval": 1.0, "io_idle_wait": 0.05}
GAP_PREFIX = "⏸ Monitoring gap"

Interval = tuple[str, datetime, datetime | None, datetime | None]


# The timing rule: what serve's loops did, and holding the detection loop between cycles


@dataclass(frozen=True)
class _Mark:
    entered: int
    stamps: dict[str, int]


@dataclass
class _Seen:
    """Counts of ``detection.run_detection`` calls and ``Progress.stamp`` calls by loop.

    ``entered`` numbers each run_detection call when it enters, ``returned`` lists the
    numbers of the calls that returned (a call that raised is not in it), and ``entries``
    has ``(generation, tracker.db_failed)`` as each call saw them on entry.
    """

    entered: int = 0
    returned: list[int] = field(default_factory=list)
    entries: list[tuple[int, bool]] = field(default_factory=list)
    stamps: dict[str, int] = field(default_factory=dict)
    _cond: threading.Condition = field(default_factory=threading.Condition)
    _in_flight: int = 0
    _open: bool = True
    _idle: bool = True

    def mark(self) -> _Mark:
        with self._cond:
            return _Mark(self.entered, dict(self.stamps))

    def stamped_since(self, mark: _Mark) -> bool:
        with self._cond:
            return all(
                self.stamps.get(name, 0) > mark.stamps.get(name, 0)
                for name in ("detection", "telegram-io")
            )

    def cycled_since(self, mark: _Mark) -> bool:
        with self._cond:
            return any(number > mark.entered for number in self.returned)

    def hold(self) -> None:
        """Keep the detection loop at the top of its next iteration; wait until it is there."""
        with self._cond:
            self._open = False
            if not self._cond.wait_for(lambda: self._idle, timeout=WAIT_S):
                self._open = True
                self._cond.notify_all()
                raise AssertionError("the detection loop never reached the end of a cycle")

    def release(self) -> None:
        with self._cond:
            self._open = True
            self._cond.notify_all()

    # Called from the loop threads by the wrappers the ``seen`` fixture installs.

    def enter(self, generation: int, db_failed: bool) -> int:
        with self._cond:
            self.entered += 1
            self.entries.append((generation, db_failed))
            self._in_flight += 1
            self._idle = False
            return self.entered

    def leave(self, number: int, returned: bool) -> None:
        with self._cond:
            self._in_flight -= 1
            if returned:
                self.returned.append(number)
            self._idle = True
            self._cond.notify_all()

    def at_stamp(self, name: str) -> None:
        """Before a stamp: a detection stamp outside a cycle starts a new iteration."""
        if name != "detection":
            return
        with self._cond:
            if self._in_flight:
                return
            # The top of an iteration: the previous one is over, whatever it did.
            self._idle = True
            self._cond.notify_all()
            self._cond.wait_for(lambda: self._open, timeout=WAIT_S)
            self._idle = False

    def stamped(self, name: str) -> None:
        with self._cond:
            self.stamps[name] = self.stamps.get(name, 0) + 1


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> _Seen:
    """Install the two counting wrappers before serve starts; return their record."""
    record = _Seen()
    real_run_detection = detection.run_detection
    real_stamp = supervision.Progress.stamp

    def counted_run_detection(*args: Any, **kwargs: Any) -> int:
        tracker = kwargs.get("tracker", args[2] if len(args) > 2 else None)
        generation = kwargs.get("generation", args[1] if len(args) > 1 else 0)
        number = record.enter(generation, bool(getattr(tracker, "db_failed", False)))
        returned = False
        try:
            result: int = real_run_detection(*args, **kwargs)
            returned = True
            return result
        finally:
            record.leave(number, returned)

    def counted_stamp(self: supervision.Progress, name: str) -> None:
        record.at_stamp(name)
        real_stamp(self, name)
        record.stamped(name)

    # run_worker reaches run_detection through the module, so this attribute is the call.
    monkeypatch.setattr(detection, "run_detection", counted_run_detection)
    monkeypatch.setattr(supervision.Progress, "stamp", counted_stamp)
    return record


def _step_clock(
    clock: FakeClock,
    seconds: float,
    seen: _Seen,
    *,
    cycles: bool = True,
    before_step: Callable[[], None] | None = None,
) -> None:
    """Advance ``clock`` by ``seconds`` in steps of at most STEP_S (the timing rule).

    Before each step: both loops stamped since the previous step and, with ``cycles``, a
    detection cycle that entered after it has returned; then the detection loop is held at
    the top of its next iteration while ``before_step`` (first step only) runs and the
    clock moves. ``cycles=False`` is for loops that run no cycle (a lease in DB_DOWN):
    nothing is held then.
    """
    remaining = seconds
    mark = seen.mark()
    first = True
    while remaining > 0:
        assert wait_for(lambda mark=mark: seen.stamped_since(mark), WAIT_S), "no progress"
        if cycles:
            assert wait_for(lambda mark=mark: seen.cycled_since(mark), WAIT_S), "no cycle"
            seen.hold()
        try:
            if first and before_step is not None:
                before_step()
            step = min(STEP_S, remaining)
            clock.advance(seconds=step)
            remaining -= step
            # Taken inside the hold: the first cycle at the new time enters after it.
            mark = seen.mark()
        finally:
            if cycles:
                seen.release()
        first = False


# serve, leases and the worker's session names


class _Serve:
    """``run_worker.serve`` in a thread, with the timing rule's intervals and a stall recorder."""

    def __init__(self, lease: Lease, clock: FakeClock, tmp_path: Path) -> None:
        self.stop = threading.Event()
        self.code: int | None = None
        self.stalls: list[str] = []
        health = supervision.HealthFile(tmp_path / "health")

        def target() -> None:
            self.code = run_worker.serve(
                self.stop,
                clock,
                lease,
                health=health,
                on_stall=self.stalls.append,
                **SERVE_INTERVALS,
            )

        self.thread = threading.Thread(target=target, name="serve-under-test")
        self.thread.start()

    def finish(self, timeout: float = 30.0) -> int | None:
        self.stop.set()
        self.thread.join(timeout)
        return self.code


@pytest.fixture
def leases() -> Iterator[Callable[[FakeClock], Lease]]:
    """``new(clock) -> Lease`` on the test database; every lease is closed afterwards."""
    made: list[Lease] = []

    def new(clock: FakeClock) -> Lease:
        lease = Lease(connection.settings_dict, clock)
        made.append(lease)
        return lease

    yield new
    for lease in made:
        lease.close()


def _my_session_name() -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting('application_name')")
        row = cur.fetchone()
    return str(row[0]) if row else ""


@pytest.fixture
def worker_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker threads' Django sessions named as in production: powermon-worker.

    ``connection.settings_dict`` is one dict shared by every thread's connection, this test
    thread's included. So this thread's own session is opened first, under its own name,
    and only then is OPTIONS replaced, by a copy (as ``apply_worker_db_settings`` does). The
    test thread keeps that session for the whole test, so ``terminate_backends(WORKER)``
    run from it ends only the worker threads' sessions (plan-check advisory 2).
    CONN_MAX_AGE None, as in the worker, so no connection is recycled into a new backend
    pid mid-test (Pitfall 3). monkeypatch puts both keys back afterwards.
    """
    connection.ensure_connection()
    assert _my_session_name() != WORKER
    settings_dict = connection.settings_dict
    monkeypatch.setitem(
        settings_dict, "OPTIONS", {**settings_dict["OPTIONS"], "application_name": WORKER}
    )
    monkeypatch.setitem(settings_dict, "CONN_MAX_AGE", None)


def _sessions(application_name: str) -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = %s AND datname = current_database()",
            [application_name],
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _terminate(application_name: str) -> int:
    """End every session named ``application_name``, never this test thread's own."""
    assert _my_session_name() != application_name
    return terminate_backends(application_name)


def _kill_my_session() -> None:
    """End this thread's own DB session from another one, as a DB restart would."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        row = cur.fetchone()
    assert row is not None
    killer = Actor(lambda: _terminate_pid(row[0]))
    killer.start()
    killer.join(10)
    assert killer.exc is None
    assert killer.result is True


def _terminate_pid(pid: int) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
        row = cur.fetchone()
    return bool(row and row[0])


# Database helpers


def _system(cursor: datetime | None, resumed: datetime | None = None) -> None:
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "last_cycle_completed_at": cursor,
            "detection_resumed_at": resumed,
            "web_started_at": None,
        },
    )


def _cursor() -> datetime | None:
    row = SystemState.objects.filter(pk=1).first()
    return None if row is None else row.last_cycle_completed_at


def _gaps() -> list[tuple[datetime, datetime | None]]:
    rows = OpsIncident.objects.filter(kind=lapse.KIND_MONITORING_GAP).order_by("id")
    return [(r.started_at, r.ended_at) for r in rows]


def _gap_notices() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(kind=outbox.KIND_OPS_GAP).order_by("id"))


def _offs(location: Any) -> list[OutboxMessage]:
    return list(
        OutboxMessage.objects.filter(location=location, kind=outbox.KIND_POWER_OFF).order_by("id")
    )


def _intervals(location: Any) -> list[Interval]:
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


def _heartbeating(location_factory: Callable[..., Any]) -> Any:
    """A location that is on since T0 and times out 20 s after its window starts."""
    location = location_factory(period_s=10, grace_s=10)
    assert transitions.record_heartbeat(location.pk, T0) == "started"
    return location


# INV-13 #1 in process: dropped sessions, reacquired lease, detection resumes


@pytest.mark.django_db(transaction=True)
def test_INV13_lease_session_terminated_reacquires_and_carves(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    worker_sessions: None,
    seen: _Seen,
    tmp_path: Path,
) -> None:
    _system(cursor=None)
    location = _heartbeating(location_factory)
    clock = FakeClock(T0)
    lease = leases(clock)

    def drop_the_lease_session() -> None:
        assert _terminate(LEASE) == 1

    serving = _Serve(lease, clock, tmp_path)
    try:
        assert wait_for(lambda: lease.current().generation == 1, WAIT_S)
        assert wait_for(lambda: _cursor() == T0, WAIT_S)  # generation 1 started fresh
        assert _gaps() == []

        # Only the lease session dies: the detection connection (and its pid) survive,
        # so only the new generation can force the carve of a 10 s window.
        _step_clock(clock, 10, seen, before_step=drop_the_lease_session)

        assert wait_for(lambda: lease.current().generation == 2, WAIT_S)
        assert wait_for(lambda: _gaps() == [(T0, T0 + _seconds(10))], WAIT_S)
        assert serving.thread.is_alive()

        # Silent ever since: 20 s after the window restarted at T0 + 10 s it is OFF.
        _step_clock(clock, 30, seen)
        assert wait_for(lambda: len(_offs(location)) == 1, WAIT_S)
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    [off] = _offs(location)
    assert off.event_at == T0 + _seconds(10)
    assert _gaps() == [(T0, T0 + _seconds(10))]
    assert (lease.current().state, lease.current().generation) == ("standby", 2)


@pytest.mark.django_db(transaction=True)
def test_INV13_db_sessions_terminated_detection_resumes_and_alerts(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    worker_sessions: None,
    seen: _Seen,
    tmp_path: Path,
) -> None:
    _system(cursor=None)
    location = _heartbeating(location_factory)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(T0)
    lease = leases(clock)

    def database_restart() -> None:
        # Every session the worker has: both threads' Django sessions and the lease.
        assert _terminate(WORKER) == 2
        assert _terminate(LEASE) == 1

    serving = _Serve(lease, clock, tmp_path)
    try:
        assert wait_for(lambda: lease.current().generation == 1, WAIT_S)
        # Both loops hold an open session (the I/O thread after its activation and pass).
        assert wait_for(lambda: _sessions(WORKER) == 2, WAIT_S)
        assert fake_telegram.sent == []

        _step_clock(clock, 10, seen, before_step=database_restart)

        assert wait_for(lambda: lease.current().generation == 2, WAIT_S)
        assert wait_for(lambda: _gaps() == [(T0, T0 + _seconds(10))], WAIT_S)
        _step_clock(clock, 30, seen)
        assert wait_for(lambda: len(_offs(location)) == 1, WAIT_S)
        # The same process delivers both, so the I/O thread replaced its dead connection.
        assert wait_for(lambda: len(fake_telegram.sent) == 2, WAIT_S)
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert _gaps() == [(T0, T0 + _seconds(10))]
    [off] = _offs(location)
    assert off.status == "sent"
    alerts = [m for m in fake_telegram.sent if m["chat_id"] == DEFAULT_CHAT_ID]
    notices = [m for m in fake_telegram.sent if m["chat_id"] == OPS_CHAT_ID]
    assert [m["text"] for m in alerts] == [
        texts.render_alert(off.kind, "en", off.payload["was_on_us"])
    ]
    assert len(notices) == 1
    assert notices[0]["text"].startswith(GAP_PREFIX)
    assert [n.status for n in _gap_notices()] == ["sent"]


@pytest.mark.django_db(transaction=True)
def test_MON05_detection_connection_reestablished_forces_carve(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    worker_sessions: None,
    seen: _Seen,
    tmp_path: Path,
) -> None:
    _system(cursor=None)
    _heartbeating(location_factory)
    clock = FakeClock(T0)
    lease = leases(clock)

    def drop_the_worker_sessions() -> None:
        assert _terminate(WORKER) == 2

    serving = _Serve(lease, clock, tmp_path)
    try:
        assert wait_for(lambda: lease.current().generation == 1, WAIT_S)
        assert wait_for(lambda: _sessions(WORKER) == 2, WAIT_S)

        # The lease stays: only the new backend pid of the detection connection forces it.
        _step_clock(clock, 10, seen, before_step=drop_the_worker_sessions)

        assert wait_for(lambda: _gaps() == [(T0, T0 + _seconds(10))], WAIT_S)
        _step_clock(clock, 20, seen)
        assert wait_for(lambda: _cursor() == T0 + _seconds(30), WAIT_S)
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert lease.current().generation == 1
    assert _gaps() == [(T0, T0 + _seconds(10))]


# INV-13 #4, carve variant: one location's error never stops the others


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "error",
    [RuntimeError("a broken timeline"), IntegrityError("power_interval_no_overlap")],
    ids=["bug", "constraint"],
)
def test_INV13_error_in_one_location_during_carve_does_not_stop_others(
    location_factory: Callable[..., Any],
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    _system(cursor=T0, resumed=T0 - timedelta(hours=1))
    a, b = location_factory(name="A"), location_factory(name="B")
    for location in (a, b):
        transitions.record_heartbeat(location.pk, T0 - timedelta(minutes=5))
    real = timeline.overwrite

    def failing_for_a(
        cur: Any, location_id: int, start: datetime, end: datetime, state: str = "not_monitored"
    ) -> int:
        if location_id == a.pk:
            raise error
        return real(cur, location_id, start, end, state)

    monkeypatch.setattr(timeline, "overwrite", failing_for_a)
    caplog.set_level(logging.ERROR, logger=lapse.__name__)
    end = T0 + timedelta(minutes=10)

    assert lapse.carve_if_needed(end, force=True) == lapse.Gap(T0, end)

    assert _intervals(a) == [("on", T0 - timedelta(minutes=5), None, None)]
    assert _intervals(b) == [
        ("on", T0 - timedelta(minutes=5), T0, None),
        ("not_monitored", T0, end, None),
        ("on", end, None, None),
    ]
    assert _cursor() == end
    assert len(_gap_notices()) == 1
    lines = [r.getMessage() for r in caplog.records if r.name == lapse.__name__]
    assert lines == [f"lapse carve failed for location {a.pk}"]


def _lose_the_session() -> None:
    """This thread's session is terminated mid-transaction, as in a database restart."""
    _kill_my_session()
    with connection.cursor() as cur:
        cur.execute("SELECT 1")


def _hit_a_lock_timeout() -> None:
    raise OperationalError("canceling statement due to lock timeout")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "fault", [_lose_the_session, _hit_a_lock_timeout], ids=["lost-session", "lock-timeout"]
)
def test_INV13_carve_aborts_on_a_connectivity_error_and_keeps_the_cursor(
    location_factory: Callable[..., Any],
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    fault: Callable[[], None],
) -> None:
    # A lost session is the tricky case: Django opens a new connection as the failed
    # transaction exits, so the connection looks usable, yet this location was not carved.
    _system(cursor=T0, resumed=T0 - timedelta(hours=1))
    location = location_factory()
    transitions.record_heartbeat(location.pk, T0 - timedelta(minutes=5))
    real = timeline.overwrite

    def faulty(*args: Any, **kwargs: Any) -> int:
        fault()
        return 0

    monkeypatch.setattr(timeline, "overwrite", faulty)
    end = T0 + timedelta(minutes=10)
    try:
        with pytest.raises(OperationalError):
            lapse.carve_if_needed(end, force=True)
    finally:
        connection.close()  # a dead session must not reach the next query

    # The cursor did not move past a carve that did not happen.
    assert _cursor() == T0
    assert (_gaps(), _gap_notices()) == ([], [])

    # The next cycle re-runs the whole carve: one gap, one notice, the location carved.
    monkeypatch.setattr(timeline, "overwrite", real)
    later = end + _seconds(5)
    assert lapse.carve_if_needed(later, force=True) == lapse.Gap(T0, later)
    assert _gaps() == [(T0, later)]
    assert len(_gap_notices()) == 1
    assert _intervals(location)[1] == ("not_monitored", T0, later, None)


# The detection loop's failure flag (D-04: a lost connection forces the next carve)


def _run_detection_loop(
    lease: Lease, clock: FakeClock, tmp_path: Path, until: Callable[[], bool]
) -> None:
    """``run_worker.detection_loop`` in a thread until ``until()`` holds, then stopped."""
    stop = threading.Event()
    progress = supervision.Progress(clock)
    health = supervision.HealthFile(tmp_path / "health")
    thread = threading.Thread(
        target=run_worker.detection_loop, args=(stop, clock, 0.01, lease, progress, health)
    )
    thread.start()
    try:
        assert wait_for(until, WAIT_S)
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


@pytest.mark.django_db(transaction=True)
def test_worker_cycles_after_a_db_error_carve_the_failed_window(
    leases: Callable[[FakeClock], Lease],
    ops_settings: Any,
    seen: _Seen,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _system(cursor=None)
    clock = FakeClock(T0)
    real_cycle = detection.run_cycle
    calls: list[datetime] = []

    def cycle(now: datetime, tick: Callable[[], None] | None = None) -> int:
        # Each decisions step ends 5 s later (inside the loop thread, so no cycle reads the
        # clock mid-step); the second one loses its connection, as in a DB restart.
        calls.append(now)
        try:
            if len(calls) == 2:
                _kill_my_session()
                with connection.cursor() as cur:
                    cur.execute("SELECT 1")
            return real_cycle(now, tick=tick)
        finally:
            clock.advance(seconds=5)

    monkeypatch.setattr(detection, "run_cycle", cycle)

    _run_detection_loop(leases(clock), clock, tmp_path, until=lambda: len(calls) >= 5)

    # The cycle after the failed one entered with db_failed set, carved the 5 s it lost,
    # and cleared the flag; nothing else was carved.
    assert seen.entries[:4] == [(1, False), (1, False), (1, True), (1, False)]
    assert _gaps() == [(T0 + _seconds(5), T0 + _seconds(10))]
    assert len(_gap_notices()) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("a bug in the decisions step"),
        OperationalError("canceling statement due to statement timeout"),
    ],
    ids=["bug", "statement-timeout"],
)
def test_decisions_failing_every_cycle_give_at_most_one_gap_notice(
    leases: Callable[[FakeClock], Lease],
    ops_settings: Any,
    seen: _Seen,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
) -> None:
    # Plan-check advisory 1. The database stays reachable and the decisions step raises
    # on every cycle for 50 s of fake time. Only the generation's first cycle carves (the
    # 5 s before it); the error neither arms db_failed (the connection still works) nor
    # keeps the generation trigger armed, and the cursor keeps up, so no later cycle
    # carves on the 15 s threshold either.
    _system(cursor=T0 - _seconds(5), resumed=T0 - timedelta(hours=1))
    clock = FakeClock(T0)
    calls: list[datetime] = []

    def failing(now: datetime, tick: Callable[[], None] | None = None) -> int:
        calls.append(now)
        clock.advance(seconds=5)
        raise error

    monkeypatch.setattr(detection, "run_cycle", failing)

    _run_detection_loop(leases(clock), clock, tmp_path, until=lambda: len(calls) >= 10)

    assert _gaps() == [(T0 - _seconds(5), T0)]
    assert len(_gap_notices()) == 1
    assert [failed for _generation, failed in seen.entries] == [False] * len(seen.entries)
    assert _cursor() is not None and _cursor() >= T0 + _seconds(45)
