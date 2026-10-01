"""The single-worker lock: a PostgreSQL session advisory lock (D-14, D-18, Pitfall 2).

Only the worker that holds ``LOCK_KEY`` runs detection and delivery; a second instance
polls ``try_acquire`` in standby. The lock belongs to a database session, so it lives on
one dedicated psycopg connection, opened here outside Django's connections: Django's
``close_old_connections()`` and connection recycling never touch it, and it is never
used for work. The connection is in autocommit mode (a session lock ignores
transactions, and no transaction stays open) and has TCP keepalives, so a dead peer is
noticed.

The lock is gone when this session ends (a DB restart, a network drop, a terminated
backend). ``alive`` notices that, and the Phase 1 worker then exits with code 3 so
Docker restarts it (D-18); Phase 2 replaces that with in-process reacquisition.

Like ``SendResult`` in the Telegram client, nothing here raises across its boundary:
``try_acquire`` and ``alive`` return False on any database error.
"""

import contextlib
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import TupleRow

# "POWERMON" in ASCII = 5786940001439534926, below 2**63 (a PostgreSQL bigint).
LOCK_KEY = 0x504F5745524D4F4E


class Lease:
    """The worker's hold on ``LOCK_KEY``, on its own connection, opened lazily."""

    def __init__(self, settings_dict: Mapping[str, Any]) -> None:
        # Django's DATABASES["default"]; tests pass the test database's dict.
        self._settings = settings_dict
        self._conn: psycopg.Connection[TupleRow] | None = None

    def __repr__(self) -> str:
        return f"Lease(pid={self.pid})"

    def try_acquire(self) -> bool:
        """Take the lock without waiting; False if another session holds it or on any error.

        A failed try takes nothing, so polling does not stack. After True the caller never
        calls it again on this session: a session lock would stack per call.
        """
        try:
            if self._conn is None:
                self._conn = self._connect()
            row = self._conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
        except psycopg.Error:
            # The database is unreachable or this session died: drop it and reconnect on
            # the next try.
            self.close()
            return False
        return bool(row and row[0])

    def alive(self) -> bool:
        """True while the lock session answers a trivial query."""
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT 1")
        except psycopg.Error:
            return False
        return True

    @property
    def pid(self) -> int | None:
        """The lock session's backend process id, or None without a connection."""
        if self._conn is None or self._conn.closed:
            return None
        return self._conn.info.backend_pid

    def close(self) -> None:
        """Close the session quietly; the lock, if held, is released with it."""
        conn, self._conn = self._conn, None
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
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            application_name="powermon-worker-lease",
        )
