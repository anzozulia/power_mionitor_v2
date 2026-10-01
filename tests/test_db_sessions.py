"""Database sessions are bounded per role (D-16; RESEARCH Pattern 11, Pitfalls 2 and 7).

- Web sessions (the shared ``DATABASES["default"]`` OPTIONS): statement_timeout 5 s,
  idle_in_transaction_session_timeout 60 s, connect_timeout 5 s, keepalives 10/5/3 and
  libpq ``tcp_user_timeout`` 10000 ms. Without tcp_user_timeout a query on a partitioned
  database waits for ~15 min of kernel retransmits (Pitfall 2); with it the spike saw an
  error after 10.45 s.
- ``settings.WORKER_PG_OPTIONS`` holds the worker's session options (statement_timeout
  10 s, lock_timeout 5 s, idle_in_transaction_session_timeout 60 s); run_worker applies
  them (02-05). The test here proves the string is valid libpq ``options`` text.
- ``manage.py release`` runs migrations with statement_timeout 0 on the same connection,
  so the web cap never fails a long migration (Pitfall 7). That test uses
  ``transaction=True``: release runs in autocommit, like the one-shot migrate service.
"""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection

from powermon.web.management.commands import release


def _show(name: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW {name}")
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


def test_web_db_options_bound_every_session() -> None:
    default = settings.DATABASES["default"]

    assert default["OPTIONS"] == {
        "connect_timeout": 5,
        "keepalives": 1,
        "keepalives_idle": 10,
        "keepalives_interval": 5,
        "keepalives_count": 3,
        "tcp_user_timeout": 10000,
        "options": "-c statement_timeout=5000 -c idle_in_transaction_session_timeout=60000",
    }
    assert default["CONN_MAX_AGE"] == 60
    assert default["CONN_HEALTH_CHECKS"] is True


@pytest.mark.django_db
def test_web_session_shows_the_timeouts() -> None:
    assert _show("statement_timeout") == "5s"
    assert _show("idle_in_transaction_session_timeout") == "1min"
    # The client-side libpq options reach psycopg.connect (Django passes OPTIONS through).
    dsn = connection.connection.info.dsn
    assert "tcp_user_timeout=10000" in dsn
    assert "keepalives_idle=10" in dsn
    assert "connect_timeout=5" in dsn


def test_worker_pg_options_are_the_d16_values() -> None:
    assert settings.WORKER_PG_OPTIONS == (
        "-c statement_timeout=10000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=60000"
    )


@pytest.mark.django_db
def test_worker_pg_options_apply_to_a_session() -> None:
    # Edge: a typo in a GUC name would only surface when 02-05's worker connects.
    params = connection.settings_dict
    with psycopg.connect(
        dbname=params["NAME"],
        user=params["USER"],
        password=params["PASSWORD"],
        host=params["HOST"],
        port=params["PORT"],
        connect_timeout=5,
        options=settings.WORKER_PG_OPTIONS,
    ) as conn:
        values = [
            conn.execute(f"SHOW {name}").fetchone()
            for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")
        ]

    assert values == [("10s",), ("5s",), ("1min",)]


@pytest.fixture
def reset_statement_timeout() -> Iterator[None]:
    """release changes the shared test connection's session; put the OPTIONS value back."""
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET statement_timeout")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("reset_statement_timeout")
def test_release_runs_migrate_without_a_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The session starts with the web cap from OPTIONS, as in the real migrate container.
    assert _show("statement_timeout") == "5s"
    seen: list[tuple[str, str]] = []

    def migrate_spy(name: str, *args: Any, **kwargs: Any) -> None:
        seen.append((name, _show("statement_timeout")))

    monkeypatch.setattr(release, "call_command", migrate_spy)
    monkeypatch.setattr(release, "sync_admin", lambda username, password: None)

    call_command("release")

    # Failure direction: without the SET in release, the spy records '5s'.
    assert seen == [("migrate", "0")]
