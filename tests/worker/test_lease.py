"""The single-worker lease: HELD, STANDBY or DB_DOWN, reacquired in process (D-15, D-16).

The worker holds a PostgreSQL session advisory lock on its own psycopg connection
(``Lease``), outside Django's connections. These tests prove the D-15 rules on real
PostgreSQL:

- the lock is exclusive and a try never blocks; a standby keeps its session (MON-04);
- a held session is never locked again: session locks stack, so one unlock must free it;
- a terminated lease session is replaced in the same call, with a new generation and no
  exception, as a database restart needs (MON-06; D-15 replaces Phase 1's exit 3);
- the DB-down timer starts only when this process loses a lease it held (D-11 #2);
- one WARNING per run of database errors, one when the database answers again (D-16);
- the session carries the server-side keepalive GUCs that make PostgreSQL drop a
  partitioned zombie holder in about 11 s (RESEARCH Pitfall 1).

Every test that reaches the database is ``django_db(transaction=True)``: the lease's own
connection must see locks outside any test transaction, and the test database's name is
only in ``connection.settings_dict`` once the database fixture has run. Every lease is
closed afterwards (the ``leases`` fixture), or pytest-django cannot drop the test database.
States are compared as strings: ``LeaseState`` is a ``StrEnum``.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from conftest import FakeClock, terminate_backends
from django.db import connection

from powermon.worker import lease as lease_module
from powermon.worker.lease import LOCK_KEY, Lease

T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
LEASE_LOGGER = lease_module.__name__
LEASE_APPLICATION_NAME = "powermon-worker-lease"
UNREACHABLE_LINE = "worker lock: database unreachable (OperationalError); retrying"

MakeLease = Callable[..., Lease]


@pytest.fixture
def leases() -> Iterator[MakeLease]:
    """``new(settings_dict=None, clock=None) -> Lease``; every lease is closed afterwards."""
    made: list[Lease] = []

    def new(settings_dict: Mapping[str, Any] | None = None, clock: Any = None) -> Lease:
        lease = Lease(connection.settings_dict if settings_dict is None else settings_dict, clock)
        made.append(lease)
        return lease

    yield new
    for lease in made:
        lease.close()


def _unreachable() -> dict[str, Any]:
    """The test database's settings, pointed at a port where nothing listens."""
    return {**connection.settings_dict, "HOST": "127.0.0.1", "PORT": 1}


def _reachable_again(settings_dict: dict[str, Any]) -> None:
    real = connection.settings_dict
    settings_dict.update(HOST=real["HOST"], PORT=real["PORT"])


def _session(lease: Lease) -> psycopg.Connection[Any]:
    """The lease's own session: a session lock can only be released (or shown) on it."""
    conn = lease._conn
    assert conn is not None
    return conn


def _lease_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LEASE_LOGGER]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in _lease_records(caplog) if r.levelno >= logging.WARNING]


# Exclusive, never blocking, never stacking (MON-04, D-15)


@pytest.mark.django_db(transaction=True)
def test_lease_is_exclusive_and_never_blocks(leases: MakeLease) -> None:
    a = leases()
    b = leases()

    held = a.ensure_held()
    assert (held.state, held.generation) == ("held", 1)

    started = time.monotonic()
    standby = b.ensure_held()
    assert time.monotonic() - started < 1.0
    assert (standby.state, standby.generation) == ("standby", 0)
    assert isinstance(a.pid, int)
    assert isinstance(b.pid, int)
    assert a.pid != b.pid

    a.close()
    assert a.pid is None
    # A closed lease never claims to hold the lock.
    assert a.current().state == "standby"
    taken = b.ensure_held()
    assert (taken.state, taken.generation) == ("held", 1)
    assert LOCK_KEY == 0x504F5745524D4F4E < 2**63


@pytest.mark.django_db(transaction=True)
def test_lease_never_relocks_a_held_session(leases: MakeLease) -> None:
    a = leases()
    b = leases()
    for _ in range(5):
        status = a.ensure_held()
        assert (status.state, status.generation) == ("held", 1)

    # Session locks stack per call: had A tried again while holding, one unlock would
    # leave it holding four and B would stay in standby.
    _session(a).execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))

    assert b.ensure_held().state == "held"


@pytest.mark.django_db(transaction=True)
def test_standby_keeps_its_session_and_logs_once(
    leases: MakeLease, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LEASE_LOGGER)
    holder = leases()
    standby = leases()
    assert holder.ensure_held().state == "held"

    first = standby.ensure_held()
    pid = standby.pid
    second = standby.ensure_held()

    assert (first.state, second.state) == ("standby", "standby")
    # A failed try takes nothing, so the standby keeps its session instead of reconnecting.
    assert pid is not None
    assert standby.pid == pid
    messages = [r.getMessage() for r in _lease_records(caplog)]
    assert messages.count("standby: waiting for the worker lock") == 1
    assert _warnings(caplog) == []


# A lost session is reacquired in process (MON-06, D-15)


@pytest.mark.django_db(transaction=True)
def test_lease_reacquires_in_process_after_its_backend_is_terminated(
    leases: MakeLease, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LEASE_LOGGER)
    a = leases()
    assert a.ensure_held().generation == 1
    first_pid = a.pid

    # A database restart or a network drop, as seen from the lease session.
    assert terminate_backends(LEASE_APPLICATION_NAME) == 1

    status = a.ensure_held()

    assert (status.state, status.generation) == ("held", 2)
    assert (status.down_since, status.down_since_mono) == (None, None)
    assert a.pid is not None
    assert a.pid != first_pid
    assert _warnings(caplog) == ["worker lock: lease session lost (AdminShutdown)"]
    assert all(r.exc_info is None for r in _lease_records(caplog))
    assert "worker lock held (generation 2)" in [r.getMessage() for r in _lease_records(caplog)]


@pytest.mark.django_db(transaction=True)
def test_lease_down_timer_starts_only_after_holding(leases: MakeLease) -> None:
    clock = FakeClock(T0)
    settings_dict = dict(connection.settings_dict)
    a = leases(settings_dict, clock)
    assert a.ensure_held().state == "held"
    clock.advance(seconds=5)

    # The database goes away while this process holds the lock.
    settings_dict.update(HOST="127.0.0.1", PORT=1)
    assert terminate_backends(LEASE_APPLICATION_NAME) == 1
    down = a.ensure_held()

    lost_at, lost_mono = clock.now(), clock.monotonic()
    assert (down.state, down.generation) == ("db_down", 1)
    assert (down.down_since, down.down_since_mono) == (lost_at, lost_mono)

    clock.advance(seconds=30)
    still = a.ensure_held()
    assert (still.state, still.down_since, still.down_since_mono) == ("db_down", lost_at, lost_mono)

    _reachable_again(settings_dict)
    back = a.ensure_held()
    assert (back.state, back.generation) == ("held", 2)
    assert (back.down_since, back.down_since_mono) == (None, None)


def test_never_held_lease_has_no_down_timer(leases: MakeLease) -> None:
    # Only a worker that held the lease reports the outage (D-11 #2); a fresh process that
    # cannot reach the database is not that worker.
    lease = leases(_unreachable(), FakeClock(T0))

    status = lease.ensure_held()

    assert (status.state, status.generation) == ("db_down", 0)
    assert (status.down_since, status.down_since_mono) == (None, None)
    assert lease.pid is None


@pytest.mark.django_db(transaction=True)
def test_lease_warns_once_while_unreachable_and_says_when_it_is_back(
    leases: MakeLease, caplog: pytest.LogCaptureFixture
) -> None:
    # The class name only: a psycopg error message can carry the host, the port and the user.
    caplog.set_level(logging.DEBUG, logger=LEASE_LOGGER)
    settings_dict = _unreachable()
    lease = leases(settings_dict)

    for _ in range(3):
        assert lease.ensure_held().state == "db_down"

    records = _lease_records(caplog)
    assert [(r.levelno, r.getMessage(), r.exc_info) for r in records] == [
        (logging.WARNING, UNREACHABLE_LINE, None)
    ]
    assert connection.settings_dict["PASSWORD"] not in caplog.text

    _reachable_again(settings_dict)
    assert lease.ensure_held().state == "held"
    assert lease.ensure_held().state == "held"

    later = [(r.levelno, r.getMessage()) for r in _lease_records(caplog)[1:]]
    assert later == [
        (logging.WARNING, "worker lock: database reachable again"),
        (logging.INFO, "worker lock held (generation 1)"),
    ]


# The session itself (D-16, RESEARCH Pitfall 1)


@pytest.mark.django_db(transaction=True)
def test_lease_session_settings(leases: MakeLease) -> None:
    lease = leases()
    assert lease.ensure_held().state == "held"
    session = _session(lease)

    def show(statement: str) -> str:
        row = session.execute(statement).fetchone()
        assert row is not None
        return str(row[0])

    # Server side: PostgreSQL itself reaps a partitioned session (and the lock) in ~11 s.
    assert (
        show("SHOW tcp_keepalives_idle"),
        show("SHOW tcp_keepalives_interval"),
        show("SHOW tcp_keepalives_count"),
        show("SHOW tcp_user_timeout"),
    ) == ("5", "2", "3", "10000")
    assert (show("SHOW statement_timeout"), show("SHOW lock_timeout")) == ("10s", "5s")
    # Client side: a dead peer becomes an error instead of a hang.
    dsn = session.info.dsn
    for part in ("connect_timeout=5", "keepalives_idle=10", "tcp_user_timeout=10000"):
        assert part in dsn
    with connection.cursor() as cur:
        cur.execute("SELECT application_name FROM pg_stat_activity WHERE pid = %s", [lease.pid])
        assert cur.fetchone() == (LEASE_APPLICATION_NAME,)


# current(): the published status, no I/O


@pytest.mark.django_db(transaction=True)
def test_current_publishes_without_io(leases: MakeLease, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = leases()
    before = lease.current()
    assert (before.state, before.generation, before.down_since) == ("standby", 0, None)

    status = lease.ensure_held()
    assert lease.current() == status

    # A reconnect to a partitioned database waits for its connect_timeout while holding the
    # lease's mutex. current() must still answer at once with the last published status.
    other = leases()
    entered, release = threading.Event(), threading.Event()

    def blocked_connect() -> psycopg.Connection[Any]:
        entered.set()
        release.wait(10)
        raise psycopg.OperationalError("simulated connect timeout")

    monkeypatch.setattr(other, "_connect", blocked_connect)
    caller = threading.Thread(target=other.ensure_held, name="lease-caller")
    caller.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        meanwhile = other.current()
        assert time.monotonic() - started < 0.5
        assert (meanwhile.state, meanwhile.generation) == ("standby", 0)
    finally:
        release.set()
        caller.join(5)

    assert not caller.is_alive()
    assert other.current().state == "db_down"


@pytest.mark.django_db(transaction=True)
def test_lease_repr_shows_only_the_pid(leases: MakeLease) -> None:
    lease = leases()
    lease.ensure_held()
    s = connection.settings_dict

    assert repr(lease) == f"Lease(pid={lease.pid})"
    for detail in (s["HOST"], s["USER"], s["PASSWORD"], s["NAME"]):
        assert str(detail) not in repr(lease)
