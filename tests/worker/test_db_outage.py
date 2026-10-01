"""The running worker records every lapse once and keeps detecting (MON-06, D-04; INV-13 #1, #2).

This is the in-process half of INV-13. A dropped lease session, a dropped Django session,
or both (what a database restart does to the worker) are simulated with
``pg_terminate_backend`` on real PostgreSQL. The worker must reacquire the lease or
reconnect by itself, record the missed window as one monitoring gap (one incident, one
notice), and go on detecting: a location that times out afterwards is alerted, and both
the OFF and the gap notice are delivered by the same process, so the I/O thread replaced
its dead connection too. No exit, no stall, no manual action. Restarting the real
containers (the database, the worker) is a manual check in 02-09, never a test harness
here (docs/v1-lessons.md section 1).

INV-13 #2 (OPS-02, D-11 #2): a database unreachable for more than 5 min is reported once,
straight to the admin chat with the ops bot, because the outbox lives in the database;
only a worker that held the lease when the database went away sends it, and the gap
notice follows through the outbox once the database is back. "Unreachable" is the lease
pointed at 127.0.0.1:1 on a mutable settings copy after its session was terminated.

C1 (wave 3 audit): a worker whose lease session is gone claims and sends no outbox row, of
either channel, even while its last published status still says HELD and another worker
holds the lock; it sends again only after it holds the lock itself.

WR-01 (code review): an outcome the relay kept after a database error is written back only
onto this worker's own claim. A "no request made" reset needs the claim's lease session to
still hold the lock and is dropped by a new lease generation; a kept retry concerns only
the attempt its claim counted. So another worker's send of the row is never reset to
"pending" and never repeated.

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

import dataclasses
import json
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
import responses
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
from django.db import IntegrityError, OperationalError, connection, transaction
from requests import PreparedRequest
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import ops, outbox, texts
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import lapse, timeline, transitions
from powermon.engine.models import PowerInterval, SystemState
from powermon.worker import detection, io_loop, supervision
from powermon.worker.lease import Lease, LeaseState, LeaseStatus
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
    """``run_worker.serve`` in a thread, with the timing rule's intervals and a stall recorder.

    ``intervals`` overrides SERVE_INTERVALS (``check_interval``, ``detection_interval``,
    ``io_idle_wait``).
    """

    def __init__(self, lease: Lease, clock: FakeClock, tmp_path: Path, **intervals: float) -> None:
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
                **{**SERVE_INTERVALS, **intervals},
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
    fake_telegram: Any,
    worker_sessions: None,
    seen: _Seen,
    tmp_path: Path,
) -> None:
    _system(cursor=None)
    location = _heartbeating(location_factory)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)  # the OFF at the end is delivered, offline
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


# INV-13 #2: the database unreachable for more than 5 min (OPS-02, D-11 #2)

IO_LOGGER = io_loop.__name__
NOT_CONFIGURED = "ops notice (ops chat not configured): "


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    return settings


@pytest.fixture
def no_ops_chat(settings: Any) -> Any:
    """``settings.CFG`` with no ops chat, whatever the env file says (D-09)."""
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token="", ops_chat_id=None)
    return settings


def _db_down_text(since: str) -> str:
    return (
        f"🛑 Database unreachable since {since} (over 5 min). Detection is paused; "
        "the gap will be recorded as not monitored when it is back."
    )


def _point_away(settings_dict: dict[str, Any]) -> None:
    """The lease's database is unreachable from now on: nothing listens on port 1."""
    settings_dict.update(HOST="127.0.0.1", PORT=1)


def _point_back(settings_dict: dict[str, Any]) -> None:
    real = connection.settings_dict
    settings_dict.update(HOST=real["HOST"], PORT=real["PORT"])


def _refused(token: str) -> requests.ConnectionError:
    # A real connect-phase exception carries the URL, and so the token, in its text (P-12).
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


def _ops_bot_answers(fake: Any, *answers: Any) -> list[dict[str, Any]]:
    """The ops bot answers each sendMessage with the next answer; the last one repeats.

    An answer is an exception (raised by the transport), ``(status, json_body)`` or "ok".
    Returns the JSON bodies of the accepted messages.
    """
    accepted: list[dict[str, Any]] = []
    calls: list[PreparedRequest] = []

    def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        answer = answers[min(len(calls), len(answers) - 1)]
        calls.append(request)
        if isinstance(answer, BaseException):
            raise answer
        if answer == "ok":
            accepted.append(json.loads(request.body or b"{}"))
            return 200, {}, json.dumps({"ok": True, "result": {"message_id": len(accepted)}})
        status, body = answer
        return status, {}, json.dumps(body)

    fake.rsps.add_callback(
        responses.POST,
        f"{fake.API}/bot{OPS_BOT_TOKEN}/sendMessage",
        callback=callback,
        content_type="application/json",
    )
    return accepted


def _ops_bot_calls(fake: Any) -> list[Any]:
    return [c for c in fake.calls if f"/bot{OPS_BOT_TOKEN}/" in c.request.url]


def _down(clock: FakeClock, generation: int = 1) -> LeaseStatus:
    """The status of a lease this process held, lost at the clock's current time."""
    return LeaseStatus(LeaseState.DB_DOWN, generation, clock.now(), clock.monotonic())


@pytest.mark.django_db(transaction=True)
def test_INV13_db_down_over_5_min_one_direct_notice_then_gap_notice(
    ops_settings: Any, kyiv: Any, fake_telegram: Any
) -> None:
    _system(cursor=None)
    clock = FakeClock(T0)
    settings_dict = dict(connection.settings_dict)
    lease = Lease(settings_dict, clock)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    tracker = lapse.CycleTracker()
    try:
        held = lease.ensure_held()
        assert (held.state, held.generation) == ("held", 1)
        detection.run_detection(clock, held.generation, tracker)
        assert _cursor() == T0  # the first cycle started fresh

        # The database goes away 5 s later, while this process holds the lease.
        clock.advance(seconds=5)
        _point_away(settings_dict)
        assert terminate_backends(LEASE) == 1
        down = lease.ensure_held()
        assert (down.state, down.down_since) == ("db_down", T0 + _seconds(5))

        # Nothing at 0 s, nor at exactly 300 s (strict).
        assert io_loop.notify_db_down(down, clock, state) is False
        clock.advance(seconds=300)
        assert io_loop.notify_db_down(lease.ensure_held(), clock, state) is False
        assert len(fake_telegram.calls) == 0

        # One direct notice once it is over 5 min, with the ops bot to the ops chat only.
        clock.advance(seconds=1)
        assert io_loop.notify_db_down(lease.ensure_held(), clock, state) is True
        assert fake_telegram.sent == [
            {"chat_id": OPS_CHAT_ID, "text": _db_down_text("13:00:05"), "parse_mode": "HTML"}
        ]
        assert len(_ops_bot_calls(fake_telegram)) == len(fake_telegram.calls) == 1

        # Never again while the outage lasts.
        for _ in range(60):
            clock.advance(seconds=10)
            assert io_loop.notify_db_down(lease.ensure_held(), clock, state) is False
        assert len(fake_telegram.calls) == 1

        # The database is back: the lease is held again, the gap is recorded once, and its
        # notice goes through the outbox.
        _point_back(settings_dict)
        back = lease.ensure_held()
        assert (back.state, back.generation) == ("held", 2)
        assert io_loop.notify_db_down(back, clock, state) is False
        assert state.db_down_notified is False
        detection.run_detection(clock, back.generation, tracker)
        assert _gaps() == [(T0, clock.now())]
        assert io_loop.run_iteration(clock, state) is True
    finally:
        lease.close()

    assert len(fake_telegram.sent) == 2
    assert fake_telegram.sent[1]["chat_id"] == OPS_CHAT_ID
    assert fake_telegram.sent[1]["text"].startswith(GAP_PREFIX)
    assert [n.status for n in _gap_notices()] == ["sent"]


def test_db_down_notice_only_from_a_worker_that_held_the_lease(
    ops_settings: Any, fake_telegram: Any
) -> None:
    # A standby, or a worker restarted during the outage: it never held the lease, so it
    # has no down timer and never reports the outage, however long it lasts (D-11 #2).
    settings_dict = dict(connection.settings_dict)
    _point_away(settings_dict)
    clock = FakeClock(T0)
    lease = Lease(settings_dict, clock)
    state = io_loop.RelayState()
    try:
        for _ in range(90):
            status = lease.ensure_held()
            assert (status.state, status.down_since, status.down_since_mono) == (
                "db_down",
                None,
                None,
            )
            assert io_loop.notify_db_down(status, clock, state) is False
            clock.advance(seconds=10)
    finally:
        lease.close()

    assert len(fake_telegram.calls) == 0
    assert state.db_down_notified is False


def test_db_down_notice_logged_when_the_ops_chat_is_not_configured(
    no_ops_chat: Any, kyiv: Any, fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=IO_LOGGER)
    clock = FakeClock(T0)
    state = io_loop.RelayState()
    down = _down(clock)
    clock.advance(seconds=301)

    # Logged once, and no request (D-09).
    assert io_loop.notify_db_down(down, clock, state) is False
    clock.advance(seconds=60)
    assert io_loop.notify_db_down(down, clock, state) is False

    lines = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == IO_LOGGER]
    assert lines == [(logging.WARNING, NOT_CONFIGURED + _db_down_text("13:00:00"))]
    assert len(fake_telegram.calls) == 0
    assert state.db_down_notified is True


def test_db_down_notice_retries_a_refused_send(
    ops_settings: Any, kyiv: Any, fake_telegram: Any
) -> None:
    accepted = _ops_bot_answers(fake_telegram, _refused(OPS_BOT_TOKEN), "ok")
    clock = FakeClock(T0)
    state = io_loop.RelayState()
    down = _down(clock)
    clock.advance(seconds=301)

    # Refused: nothing was sent, so the notice is not counted, and the ops chat backs off.
    assert io_loop.notify_db_down(down, clock, state) is True
    assert (accepted, state.db_down_notified) == ([], False)
    clock.advance(seconds=io_loop.BACKOFF_CAP_S - 1)
    assert io_loop.notify_db_down(down, clock, state) is False
    assert len(_ops_bot_calls(fake_telegram)) == 1

    # The ops bot reachable again after the backoff: one notice, then never again.
    clock.advance(seconds=1)
    assert io_loop.notify_db_down(down, clock, state) is True
    for _ in range(10):
        clock.advance(seconds=60)
        assert io_loop.notify_db_down(down, clock, state) is False
    assert [m["text"] for m in accepted] == [_db_down_text("13:00:00")]
    assert len(_ops_bot_calls(fake_telegram)) == 2
    assert state.db_down_notified is True


@pytest.mark.parametrize(
    ("first", "wait_s"),
    [
        ((502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}), 30),
        (
            (
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 120",
                    "parameters": {"retry_after": 120},
                },
            ),
            120,
        ),
        (
            (
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 999_999},
                },
            ),
            io_loop.MAX_RETRY_AFTER_S,
        ),
    ],
    ids=["transient", "rate-limited", "rate-limited-capped"],
)
def test_db_down_notice_waits_before_a_retry(
    ops_settings: Any, kyiv: Any, fake_telegram: Any, first: Any, wait_s: int
) -> None:
    accepted = _ops_bot_answers(fake_telegram, first, "ok")
    clock = FakeClock(T0)
    state = io_loop.RelayState()
    down = _down(clock)
    clock.advance(seconds=301)

    assert io_loop.notify_db_down(down, clock, state) is True
    clock.advance(seconds=wait_s - 1)
    assert io_loop.notify_db_down(down, clock, state) is False
    clock.advance(seconds=1)
    assert io_loop.notify_db_down(down, clock, state) is True

    assert [m["text"] for m in accepted] == [_db_down_text("13:00:00")]
    assert len(_ops_bot_calls(fake_telegram)) == 2


@pytest.mark.parametrize(
    ("answer", "warning"),
    [
        (
            (403, {"ok": False, "error_code": 403, "description": "Forbidden"}),
            "relay: permanent error http_403 for the ops chat; "
            "the database-down notice is not resent",
        ),
        (requests.ReadTimeout("read timed out"), None),
    ],
    ids=["permanent", "maybe-delivered"],
)
def test_db_down_notice_is_never_resent_after_a_final_answer(
    ops_settings: Any,
    kyiv: Any,
    fake_telegram: Any,
    caplog: pytest.LogCaptureFixture,
    answer: Any,
    warning: str | None,
) -> None:
    # A 400/401/403 will not get better by retrying, and a send that may have reached
    # Telegram is never repeated (at-most-once, INV-16).
    _ops_bot_answers(fake_telegram, answer)
    caplog.set_level(logging.WARNING, logger=IO_LOGGER)
    clock = FakeClock(T0)
    state = io_loop.RelayState()
    down = _down(clock)
    clock.advance(seconds=301)

    assert io_loop.notify_db_down(down, clock, state) is True
    for _ in range(30):
        clock.advance(seconds=60)
        assert io_loop.notify_db_down(down, clock, state) is False

    assert len(_ops_bot_calls(fake_telegram)) == 1
    assert state.db_down_notified is True
    lines = [r.getMessage() for r in caplog.records if r.name == IO_LOGGER]
    assert lines == ([] if warning is None else [warning])
    assert OPS_BOT_TOKEN not in caplog.text


@pytest.mark.parametrize("recovered", [LeaseState.HELD, LeaseState.STANDBY])
def test_db_down_notice_resets_after_recovery(
    ops_settings: Any, kyiv: Any, fake_telegram: Any, recovered: LeaseState
) -> None:
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(T0)
    state = io_loop.RelayState()
    first = _down(clock)
    clock.advance(seconds=301)
    assert io_loop.notify_db_down(first, clock, state) is True

    # The database answered again: the next outage is a new one.
    assert io_loop.notify_db_down(LeaseStatus(recovered, 2, None, None), clock, state) is False
    assert state.db_down_notified is False
    clock.advance(minutes=10)
    second = _down(clock, generation=2)
    clock.advance(seconds=300)
    assert io_loop.notify_db_down(second, clock, state) is False
    clock.advance(seconds=1)
    assert io_loop.notify_db_down(second, clock, state) is True
    clock.advance(minutes=10)
    assert io_loop.notify_db_down(second, clock, state) is False

    assert [m["text"] for m in fake_telegram.sent] == [
        _db_down_text("13:00:00"),
        _db_down_text("13:15:01"),
    ]


@pytest.mark.django_db(transaction=True)
def test_io_thread_sends_the_db_down_notice(
    ops_settings: Any, kyiv: Any, fake_telegram: Any, seen: _Seen, tmp_path: Path
) -> None:
    _system(cursor=None)
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(T0)
    settings_dict = dict(connection.settings_dict)
    lease = Lease(settings_dict, clock)
    serving = _Serve(lease, clock, tmp_path, detection_interval=0.05, io_idle_wait=0.05)
    try:
        assert wait_for(lambda: lease.current().state == "held", WAIT_S)
        _point_away(settings_dict)
        assert _terminate(LEASE) == 1
        # The detection loop finds the session gone and the database unreachable.
        assert wait_for(lambda: lease.current().state == "db_down", WAIT_S)
        assert lease.current().down_since == T0

        # 310 s in 10 s steps, each after both loops stamped progress (the timing rule):
        # no stamp is ever more than about 10 s old at a watchdog check, and no cycle runs
        # while the lease is down, so no lapse rule applies.
        _step_clock(clock, 310, seen, cycles=False)
        assert wait_for(lambda: len(fake_telegram.sent) == 1, WAIT_S)
        _step_clock(clock, 60, seen, cycles=False)
    finally:
        code = serving.finish()
        lease.close()

    assert code == 0
    assert serving.stalls == []
    assert len(fake_telegram.calls) == 1
    assert fake_telegram.sent == [
        {"chat_id": OPS_CHAT_ID, "text": _db_down_text("13:00:00"), "parse_mode": "HTML"}
    ]


# C1 (wave 3 audit): no claim after the lease session is lost (INV-02 #2, INV-15)

OFF_WAS_ON_US = 300_000_000


def _queue_off(location: Any) -> OutboxMessage:
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=T0,
            recorded_at=T0,
            payload={"was_on_us": OFF_WAS_ON_US},
        )


def _claim_state(row: OutboxMessage) -> tuple[str, int]:
    row.refresh_from_db()
    return row.status, row.attempts


def _io_stamps(seen: _Seen) -> int:
    return seen.mark().stamps.get("telegram-io", 0)


@pytest.mark.django_db(transaction=True)
def test_C1_a_pass_claims_only_while_its_lease_session_holds_the_lock(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    ops_settings: Any,
    fake_telegram: Any,
) -> None:
    location = location_factory()
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(T0)
    old, new = leases(clock), leases(clock)
    assert old.ensure_held().state == "held"
    lost_pid = old.current().pid
    off = _queue_off(location)
    with transaction.atomic():
        notice = outbox.enqueue_ops(
            outbox.KIND_OPS_GAP,
            payload={"start_us": ops.instant_us(T0 - _seconds(10)), "end_us": ops.instant_us(T0)},
            recorded_at=T0,
        )

    # The old session is gone and a second worker holds the lock: a pass that still names
    # the old session claims nothing, on either channel.
    assert _terminate(LEASE) == 1
    assert new.ensure_held().state == "held"
    state = io_loop.RelayState(lease_pid=lost_pid)
    for _ in range(3):
        assert io_loop.run_iteration(clock, state) is False
    assert len(fake_telegram.calls) == 0
    assert (_claim_state(off), _claim_state(notice)) == (("pending", 0), ("pending", 0))

    # The holder's session: the subscriber head, then the ops row, in one pass.
    state.lease_pid = new.current().pid
    assert io_loop.run_iteration(clock, state) is True
    assert [m["chat_id"] for m in fake_telegram.sent] == [DEFAULT_CHAT_ID, OPS_CHAT_ID]
    assert (_claim_state(off), _claim_state(notice)) == (("sent", 1), ("sent", 1))


@pytest.mark.django_db(transaction=True)
def test_C1_old_holder_never_sends_after_losing_the_lock(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    seen: _Seen,
    tmp_path: Path,
) -> None:
    # The audit's reproduction: right after a cycle the old holder's lease session is
    # terminated and a second worker takes the lock, then an OFF is queued. The old
    # holder's detection loop is held before it asks the lease again, so its published
    # status still says HELD (the window C1 is about).
    _system(cursor=None)
    location = location_factory()
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    clock = FakeClock(T0)
    old, new = leases(clock), leases(clock)
    serving = _Serve(old, clock, tmp_path)
    try:
        assert wait_for(lambda: old.current().generation == 1, WAIT_S)
        start = seen.mark()
        assert wait_for(lambda: seen.cycled_since(start) and seen.stamped_since(start), WAIT_S)
        seen.hold()
        try:
            assert _terminate(LEASE) == 1  # the second worker has no session yet
            assert new.ensure_held().state == "held"
            assert old.current().state == "held"  # stale: the old holder has not looked
            off = _queue_off(location)
            passes = _io_stamps(seen)
            # Ten passes or more of the old holder's I/O thread (three stamps a pass).
            assert wait_for(lambda: _io_stamps(seen) >= passes + 30, WAIT_S)
            assert fake_telegram.sent == []
            assert _claim_state(off) == ("pending", 0)
        finally:
            seen.release()

        # The old holder looks again: its session is gone and the lock is taken.
        assert wait_for(lambda: old.current().state == "standby", WAIT_S)
        passes = _io_stamps(seen)
        assert wait_for(lambda: _io_stamps(seen) >= passes + 10, WAIT_S)
        assert fake_telegram.sent == []

        # The second worker goes away: the old process holds the lock again (generation
        # 2) and sends the OFF, once.
        new.close()
        assert wait_for(lambda: old.current().generation == 2, WAIT_S)
        assert wait_for(lambda: len(fake_telegram.sent) == 1, WAIT_S)
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert fake_telegram.sent == [
        {
            "chat_id": DEFAULT_CHAT_ID,
            "text": texts.render_alert(outbox.KIND_POWER_OFF, "en", OFF_WAS_ON_US),
            "parse_mode": "HTML",
        }
    ]
    assert _claim_state(off) == ("sent", 1)


# WR-01 (code review): a kept outcome is written back only onto this worker's own claim, so
# another worker's send of the row is never reset and never repeated (ALRT-05, MON-04)


def _db_error() -> OperationalError:
    return OperationalError("server closed the connection unexpectedly")


def _claim_raises_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay's next claim raises a database error without committing (a DB blip)."""
    real = outbox.claim

    def claim(message_id: int, lease_pid: int | None = None) -> bool:
        monkeypatch.setattr(outbox, "claim", real)
        raise _db_error()

    monkeypatch.setattr(outbox, "claim", claim)


def _claim_kept_by_a_lost_worker(
    leases: Callable[[FakeClock], Lease],
    location: Any,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OutboxMessage, Lease, io_loop.RelayState, Lease]:
    """Worker A keeps a "no request made" reset of an OFF, then loses its lease session.

    A's claim raises and does not commit, so the OFF is still pending; then A's session is
    terminated and worker B takes the lock. Returns the OFF, A's lease and relay state (its
    lease_pid still names the lost session, as a stale HELD status does) and B's lease.
    """
    old, new = leases(clock), leases(clock)
    assert old.ensure_held().state == "held"
    off = _queue_off(location)
    a = io_loop.RelayState(lease_pid=old.current().pid)
    _claim_raises_once(monkeypatch)
    assert io_loop.run_iteration(clock, a) is False
    assert (list(a.unapplied), _claim_state(off)) == ([off.pk], ("pending", 0))
    assert _terminate(LEASE) == 1  # A's session only: B has none yet
    assert new.ensure_held().state == "held"
    return off, old, a, new


@pytest.mark.django_db(transaction=True)
def test_WR01_a_kept_reset_never_resets_another_workers_send(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The review's reproduction: B claims and sends the OFF, and while B's request is in
    # flight, A (its published status still HELD) runs a pass that flushes the kept reset.
    clock = FakeClock(T0)
    off, _old, a, new = _claim_kept_by_a_lost_worker(leases, location_factory(), clock, monkeypatch)
    b = io_loop.RelayState(lease_pid=new.current().pid)
    fake_telegram.answer(DEFAULT_BOT_TOKEN, lambda: io_loop.run_iteration(clock, a))
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    assert io_loop.run_iteration(clock, b) is True

    # B's "sent" is recorded, and neither worker sends the OFF again.
    assert _claim_state(off) == ("sent", 1)
    for _ in range(3):
        clock.advance(seconds=1)
        assert io_loop.run_iteration(clock, b) is False
        assert io_loop.run_iteration(clock, a) is False
    assert len(fake_telegram.sent) == 1
    assert a.unapplied == {}


@pytest.mark.django_db(transaction=True)
def test_WR01_a_kept_retry_never_resets_another_workers_send(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A's OFF is refused (not sent). A's write of the retry commits, but its answer is lost,
    # so A keeps the outcome. Then B holds the lock and sends the OFF; A's flush during B's
    # send concerns A's own attempt only and leaves B's claim alone.
    location = location_factory()
    clock = FakeClock(T0)
    old, new = leases(clock), leases(clock)
    assert old.ensure_held().state == "held"
    off = _queue_off(location)
    a = io_loop.RelayState(lease_pid=old.current().pid)
    fake_telegram.fail(DEFAULT_BOT_TOKEN, exc=_refused(DEFAULT_BOT_TOKEN))
    real_retry = outbox.mark_retry

    def committed_then_lost(*args: Any, **kwargs: Any) -> bool:
        monkeypatch.setattr(outbox, "mark_retry", real_retry)
        real_retry(*args, **kwargs)
        raise _db_error()

    monkeypatch.setattr(outbox, "mark_retry", committed_then_lost)
    assert io_loop.run_iteration(clock, a) is True
    assert (list(a.unapplied), _claim_state(off)) == ([off.pk], ("pending", 1))

    clock.advance(seconds=2)
    assert _terminate(LEASE) == 1
    assert new.ensure_held().state == "held"
    b = io_loop.RelayState(lease_pid=new.current().pid)
    fake_telegram.answer(DEFAULT_BOT_TOKEN, lambda: io_loop.run_iteration(clock, a))
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    assert io_loop.run_iteration(clock, b) is True

    assert _claim_state(off) == ("sent", 2)
    for _ in range(3):
        clock.advance(seconds=30)
        assert io_loop.run_iteration(clock, b) is False
    assert len(fake_telegram.sent) == 1
    assert a.unapplied == {}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("named", ["lost", "current"])
def test_WR01_a_new_lease_generation_drops_the_kept_resets(
    leases: Callable[[FakeClock], Lease],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    named: str,
) -> None:
    # B claims the OFF and its request may have reached Telegram when B's session is lost
    # too. A then holds the lock again (generation 2): its activation writes no kept reset,
    # whichever lease session its state names, and declares B's send uncertain.
    clock = FakeClock(T0)
    off, old, a, new = _claim_kept_by_a_lost_worker(leases, location_factory(), clock, monkeypatch)
    assert outbox.claim(off.pk, new.current().pid) is True
    assert _terminate(LEASE) == 1  # B's session
    held = old.ensure_held()
    assert (held.state, held.generation) == ("held", 2)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    if named == "current":
        a.lease_pid = held.pid

    assert io_loop.activate(a, clock) == 1

    assert a.unapplied == {}
    assert _claim_state(off) == ("uncertain", 1)
    a.lease_pid = held.pid
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, a) is False
    assert len(fake_telegram.calls) == 0
