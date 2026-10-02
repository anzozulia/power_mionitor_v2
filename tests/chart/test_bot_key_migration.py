"""The ``chart_message.bot_key`` backfill migration (D-08, Phase 3 IN-02).

Migration 0008 adds ``chart_message.bot_key`` as a nullable column and fills every
existing record from its location's current token, with the ``io_loop.bot_key`` hash
(the first 12 hex digits of SHA-256, inlined in the migration, never the token). The
backfill is right because no token could change before Phase 4. Migration 0009 then makes
the column NOT NULL. They are two migrations so no transaction both updates the rows and
alters the table (RESEARCH A7).

The test migrates the test database back to 0007, writes records with the historical
models (which have no ``bot_key`` yet), migrates forward through the backfill and checks
each record's key. Migrations cannot run inside a test's wrapping transaction, hence
``transaction=True``. The test database is shared by the whole session, so the finally
block always migrates it back to the graph's leaf nodes, whatever the test did.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from powermon.worker import io_loop

BEFORE = ("powermon", "0007_login_failure")
AFTER = ("powermon", "0009_chart_message_bot_key_not_null")
TOKEN_A = "123456789:" + "T" * 35
TOKEN_B = "987654321:" + "U" * 35
AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
CHAT_ID = -1001234567890


def _location(apps: Any, name: str, token: str, key: str, **extra: Any) -> Any:
    return apps.get_model("powermon", "Location").objects.create(
        name=name, bot_token=token, chat_id=CHAT_ID, device_key=key * 32, created_at=AT, **extra
    )


def _record(apps: Any, location: Any, day: date, message_id: int) -> Any:
    return apps.get_model("powermon", "ChartMessage").objects.create(
        location_id=location.pk,
        local_date=day,
        chat_id=CHAT_ID,
        message_id=message_id,
        last_rendered_at=AT,
        created_at=AT,
    )


@pytest.mark.django_db(transaction=True)
def test_bot_key_backfill_migration() -> None:
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([BEFORE])
        old = executor.loader.project_state([BEFORE]).apps
        home = _location(old, "Home", TOKEN_A, "a")
        office = _location(old, "Office", TOKEN_B, "b")
        # A deleted location's records are backfilled too: every row needs a key.
        gone = _location(old, "Gone", TOKEN_A, "c", deleted_at=AT)
        records = {
            _record(old, home, date(2026, 9, 30), 1001).pk: TOKEN_A,
            _record(old, home, date(2026, 10, 1), 1002).pk: TOKEN_A,
            _record(old, office, date(2026, 10, 1), 1003).pk: TOKEN_B,
            _record(old, gone, date(2026, 10, 1), 1004).pk: TOKEN_A,
        }

        executor.loader.build_graph()
        executor.migrate([AFTER])

        new = executor.loader.project_state([AFTER]).apps
        chart_message = new.get_model("powermon", "ChartMessage")
        keys = dict(chart_message.objects.values_list("pk", "bot_key"))
        assert keys == {pk: io_loop.bot_key(token) for pk, token in records.items()}
        assert io_loop.bot_key(TOKEN_A) != io_loop.bot_key(TOKEN_B)
        # Only the hash is stored, never the token or its secret part.
        assert all(TOKEN_A.split(":", 1)[1] not in key for key in keys.values())
    finally:
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
