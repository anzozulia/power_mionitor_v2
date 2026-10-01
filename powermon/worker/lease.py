"""The single-worker lease: one PostgreSQL session advisory lock, kept in process (D-15).

Only the worker that holds ``LOCK_KEY`` detects and delivers (MON-04). The lock belongs to
a database session, so it lives on one dedicated psycopg connection, opened here outside
Django's connections: ``close_old_connections()`` and connection recycling never touch
it, and it is never used for work. The connection is in autocommit mode (a session lock
ignores transactions, and no transaction stays open).

The detection loop calls ``ensure_held()`` at the top of every cycle. It returns a frozen
``LeaseStatus`` in one of three states:

- HELD: this session holds the lock. Every successful acquisition raises ``generation``
  by one (0 means never held in this process). A new generation is a new active term, so
  the loops open a fresh detection window and recover interrupted sends for it.
- STANDBY: the database answered and another session holds the lock. A failed try takes
  nothing, so the standby keeps its session and tries again next cycle. It never blocks
  and never exits.
- DB_DOWN: the database did not answer. ``down_since`` and ``down_since_mono`` are set
  only when this process held the lock as the database went away, because the D-11 #2
  "database unreachable" notice is that worker's to send. A process that never held the
  lock has no timer. The timer clears as soon as the database answers again.

While the lock is held, a cycle only runs ``SELECT 1`` on the session. It never calls
``pg_try_advisory_lock`` there: session locks stack, so a held session that locked again
would need two unlocks to free it. If the session is gone (a DB restart, a terminated
backend, a network drop), the same call reconnects and tries again. This in-process
reacquisition replaces Phase 1's exit with code 3 (D-15 replaces Phase 1 D-18).

The session is bounded on both sides (D-16). On the client, connect_timeout, keepalives
and tcp_user_timeout turn a dead peer into an error within about 10 s. On the server, the
GUCs in ``LEASE_PG_OPTIONS`` make PostgreSQL drop a partitioned session, and with it the
lock, after about 11 s instead of the OS default of 2 h. Without them a healed partition
leaves a zombie holder and the only worker waits in standby (RESEARCH Pitfall 1).

Like ``SendResult`` in the Telegram client, nothing here raises across its boundary: a
database error becomes DB_DOWN. Warnings carry the error class only (a psycopg message
can carry the host, the port and the user, OPS-08): one when the session is lost, one per
run of connect errors, and one when the database answers again (D-16). ``current()``
returns the last published status without I/O and without the lease's mutex, so the I/O
thread can read it while a reconnect waits for its connect_timeout.
"""

import contextlib
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from powermon.clock import Clock, SystemClock

log = logging.getLogger(__name__)

# "POWERMON" in ASCII = 5786940001439534926, below 2**63 (a PostgreSQL bigint).
LOCK_KEY = 0x504F5745524D4F4E

# libpq session options. The client-side statement and lock timeouts bound every call;
# the server-side keepalive GUCs and tcp_user_timeout let PostgreSQL reap a partitioned
# lease session in about 11 s (RESEARCH spike 7).
LEASE_PG_OPTIONS = (
    "-c statement_timeout=10000 -c lock_timeout=5000 "
    "-c tcp_keepalives_idle=5 -c tcp_keepalives_interval=2 -c tcp_keepalives_count=3 "
    "-c tcp_user_timeout=10000"
)


class LeaseState(StrEnum):
    """What the last ``ensure_held()`` found."""

    HELD = "held"
    STANDBY = "standby"
    DB_DOWN = "db_down"


@dataclass(frozen=True)
class LeaseStatus:
    """One answer of ``ensure_held()``, also published for ``current()``."""

    state: LeaseState
    # 0 = never held in this process; +1 on every successful acquisition.
    generation: int
    # Set only on a HELD -> DB_DOWN loss in this process; cleared once the DB answers.
    down_since: datetime | None
    down_since_mono: float | None


class Lease:
    """The worker's hold on ``LOCK_KEY``, on its own connection, opened lazily."""

    def __init__(self, settings_dict: Mapping[str, Any], clock: Clock | None = None) -> None:
        # Django's DATABASES["default"]; tests pass the test database's dict. Read at each
        # connect, so a test can point a lease elsewhere and back.
        self._settings = settings_dict
        self._clock: Clock = SystemClock() if clock is None else clock
        self._mutex = threading.Lock()
        self._conn: psycopg.Connection[TupleRow] | None = None
        self._held = False
        self._generation = 0
        self._down_since: datetime | None = None
        self._down_since_mono: float | None = None
        # True from a failed connect or try until the database answers again.
        self._failing = False
        # True from the first standby result until a result that is not standby.
        self._announced_standby = False
        self._published = LeaseStatus(LeaseState.STANDBY, 0, None, None)

    def __repr__(self) -> str:
        return f"Lease(pid={self.pid})"

    def ensure_held(self) -> LeaseStatus:
        """Keep or take the lock without waiting for it; never raises (thread-safe)."""
        with self._mutex:
            status = self._ensure_held()
            self._published = status
            return status

    def current(self) -> LeaseStatus:
        """The last status ``ensure_held()`` returned (STANDBY, generation 0, before any)."""
        # One reference read: no I/O, no mutex, so it never waits for a reconnect.
        return self._published

    @property
    def pid(self) -> int | None:
        """The lock session's backend process id, or None without a connection."""
        conn = self._conn
        if conn is None or conn.closed:
            return None
        return conn.info.backend_pid

    def close(self) -> None:
        """Close the session quietly; the lock, if held, is released with it."""
        with self._mutex:
            self._drop()
            # A closed lease never tells a loop that it may act.
            self._published = LeaseStatus(LeaseState.STANDBY, self._generation, None, None)

    def _ensure_held(self) -> LeaseStatus:
        if self._held and self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
            except psycopg.Error as exc:
                self._lose(exc)
            else:
                return self._status(LeaseState.HELD)
        try:
            if self._conn is None:
                self._conn = self._connect()
            # Only on a session that does not hold the lock: a failed try takes nothing.
            row = self._conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
        except psycopg.Error as exc:
            self._drop()
            self._announced_standby = False
            if not self._failing:
                log.warning("worker lock: database unreachable (%s); retrying", type(exc).__name__)
                self._failing = True
            return self._status(LeaseState.DB_DOWN)
        # The database answered, so any outage is over.
        if self._failing:
            log.warning("worker lock: database reachable again")
            self._failing = False
        self._down_since = None
        self._down_since_mono = None
        if row and row[0]:
            self._held = True
            self._generation += 1
            self._announced_standby = False
            log.info("worker lock held (generation %d)", self._generation)
            return self._status(LeaseState.HELD)
        if not self._announced_standby:
            log.info("standby: waiting for the worker lock")
            self._announced_standby = True
        return self._status(LeaseState.STANDBY)

    def _lose(self, exc: psycopg.Error) -> None:
        """The held session is gone: drop it and start the down timer (D-11 #2)."""
        self._drop()
        self._down_since = self._clock.now()
        self._down_since_mono = self._clock.monotonic()
        log.warning("worker lock: lease session lost (%s)", type(exc).__name__)

    def _status(self, state: LeaseState) -> LeaseStatus:
        return LeaseStatus(state, self._generation, self._down_since, self._down_since_mono)

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        self._held = False
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    def _connect(self) -> psycopg.Connection[TupleRow]:
        s = self._settings
        return psycopg.connect(
            host=s["HOST"],
            port=s["PORT"],
            dbname=s["NAME"],
            user=s["USER"],
            password=s["PASSWORD"],
            autocommit=True,
            connect_timeout=5,
            keepalives=1,
            keepalives_idle=10,
            keepalives_interval=5,
            keepalives_count=3,
            tcp_user_timeout=10000,
            options=LEASE_PG_OPTIONS,
            application_name="powermon-worker-lease",
        )
