"""INV-05 #1 and K-4 driven through the location page's switches (LOC-09, LOC-10; D-05, D-06).

Each switch is clicked as the admin clicks it: a signed-in POST to its URL, answered with
a redirect back to the location page. The effect is then followed through the parts of
the worker that act on it: detection (``detection.run_cycle``), the heartbeat gate
(``transitions.record_heartbeat``) and the I/O pass (``io_loop.run_iteration``). Each
switch has exactly one effect (INV-05):
- alerts off: transitions and the timeline carry on, but a transition recorded while
  alerts are off queues no alert, and nothing is held for later; alerts already queued
  still go out (D-06);
- router grace: only the timeout of decisions made after the change (K-4).

``run_iteration`` calls ``close_old_connections()``, so every test here is
``django_db(transaction=True)``: it must see the rows the other steps committed, and no
test-wide transaction may hold them. Time comes only from the injected ``FakeClock`` and
the instants passed to the engine; Telegram is faked at the HTTP boundary
(``fake_telegram``). The few chart helpers (``monitor``, ``insert_pieces`` shapes) are
copied from tests/chart/chart_fixtures.py instead of imported: tests have no
``__init__.py``, so tests/chart is on ``sys.path`` only once one of its modules was
collected, and this file must also run on its own.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.test import Client

from powermon.alerts.models import OutboxMessage
from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.worker import detection, io_loop

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"


@pytest.fixture
def admin(client: Client, transactional_db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _click(admin: Client, location: Any, switch: str, value: str) -> None:
    """Click a switch on the location page: its POST answers with a redirect back there."""
    response = admin.post(f"/locations/{location.pk}/{switch}/", {"value": value})
    assert (response.status_code, response.url) == (302, f"/locations/{location.pk}/")


def _anchors() -> None:
    """Detection resumed at 09:00 and the web start is unknown: no window after 09:00."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": None}
    )


def _beating(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """K-2: heartbeats every 60 s from 10:00:00 to 10:05:00, then silence (OFF after 10:06:30)."""
    _anchors()
    location = location_factory(**overrides)
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    return location


def _intervals(location: Any) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    return list(
        PowerInterval.objects.filter(location=location)
        .order_by("start_at")
        .values_list("state", "start_at", "end_at", "outage_start_at")
    )


# INV-05 #1, alert part: alerts off from the location page, then an outage


def test_INV05_1_alerts_off_outage_sends_nothing(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _beating(location_factory, name="Office")
    _click(admin, location, "alerts", "off")
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    relay = io_loop.RelayState()

    # The device went silent after 10:05:00: K-2, OFF after 10:06:30, not at it.
    assert detection.run_cycle(_at(10, 6, 30)) == 0
    assert detection.run_cycle(_at(10, 6, 31)) == 1

    # The OFF transition and the timeline are recorded as usual...
    state = LocationState.objects.get(location=location)
    assert (state.status, state.outage_started_at) == ("off", _at(10, 5))
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), None, _at(10, 5)),
    ]
    # ...but no alert is queued, so the relay has nothing to send.
    assert not OutboxMessage.objects.exists()
    assert io_loop.run_iteration(FakeClock(_at(10, 6, 35)), relay) is False

    # Power returns at 11:00: the ON transition is recorded, and again nothing is queued.
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    state = LocationState.objects.get(location=location)
    assert (state.status, state.on_since) == ("on", _at(11, 0))
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), _at(11, 0), _at(10, 5)),
        ("on", _at(11, 0), None, None),
    ]
    assert not OutboxMessage.objects.exists()
    assert io_loop.run_iteration(FakeClock(_at(11, 0, 5)), relay) is False
    # Subscribers got 0 messages for the whole outage.
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendMessage") == 0
    assert len(fake_telegram.calls) == 0


def test_alerts_already_queued_still_go_out(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    # D-06: whether an alert is sent is decided when its transition is recorded. The OFF
    # was recorded while alerts were on, so its alert is queued and goes out after the
    # switch; only the restore, recorded with alerts off, queues nothing.
    location = _beating(location_factory)
    assert detection.run_cycle(_at(10, 6, 31)) == 1
    [off] = OutboxMessage.objects.all()
    assert (off.kind, off.status) == ("power_off", "pending")

    _click(admin, location, "alerts", "off")
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(_at(10, 6, 35)), io_loop.RelayState()) is True

    assert fake_telegram.sent == [
        {"chat_id": DEFAULT_CHAT_ID, "text": OFF_EN, "parse_mode": "HTML"}
    ]
    assert OutboxMessage.objects.get(pk=off.pk).status == "sent"
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"
    assert list(OutboxMessage.objects.values_list("kind", "status")) == [("power_off", "sent")]
