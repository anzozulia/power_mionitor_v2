"""After a restore, every location restarts silently (OPS-06; D-13, D-14, D-16, SC4).

What these scenarios prove:
- D-13: ``manage.py post_restore`` sets every location that is not deleted and not waiting
  back to "waiting for first heartbeat" (status waiting, its times NULL, ``state_version``
  bumped), each under its row lock. Its open interval becomes not monitored from the
  dump's last known moment (the detection cursor), never before the open piece's start;
  history and the maintenance flag are kept. Its first heartbeat restarts monitoring with
  no alert (MON-01, K-1), and the lost hours stay not monitored.
- D-14: every pending or sending outbox row of both channels is dropped with
  ``last_error`` "restored", with no expiry or uncertain notice (RESEARCH Pitfall 5:
  activation would turn a sending row into an uncertain one with an ops notice), and
  every open ops incident is closed quietly. ``system_state`` is left alone, so the
  worker's first forced carve records one gap and sends the single gap notice (SC4).
- The step refuses while a worker holds the worker lock, writes nothing then, and a
  second run changes nothing.
- RESEARCH Pitfall 3: the FIRST gate never closes the restored open piece before its
  start, even with a server clock behind the dump.
- D-16: the read-only history fingerprint the restore drill compares.

The tests are ``django_db(transaction=True)`` (module ``pytestmark``): the I/O pass and
the detection cycle call ``close_old_connections()``, the worker lock is held on the
lease's own connection, which must see committed rows, and ``fingerprint()`` must be the
outermost transaction (under plain ``django_db`` its READ ONLY would outlive it inside the
test's transaction). The teardown truncates the ``system_state`` singleton, so each test
writes the row it needs. All times are fixed aware datetimes on 2026-10-01 UTC; the
display TZ is pinned to Europe/Kyiv (UTC+3 that day).
"""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

import pytest
from conftest import FakeClock
from django.core.management import call_command

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import all_silent, restore, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.web.management.commands import post_restore as post_restore_command

pytestmark = pytest.mark.django_db(transaction=True)

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0, microsecond: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, microsecond, tzinfo=UTC)


# The moment the admin runs post_restore.
NOW = _at(10, 20)


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    return settings


def _system(
    cursor: datetime | None, resumed: datetime | None = None, web: datetime | None = None
) -> None:
    """Write the system_state singleton as the dump left it: the cursor and the anchors."""
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "last_cycle_completed_at": cursor,
            "detection_resumed_at": resumed,
            "web_started_at": web,
        },
    )


def _beat_every_minute(location: Any, first: datetime, last: datetime) -> None:
    at = first
    while at <= last:
        transitions.record_heartbeat(location.pk, at)
        at += timedelta(minutes=1)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _state(location: Any) -> tuple[str, Any, Any, Any, Any]:
    """(status, last_heartbeat_at, on_since, outage_started_at, window_start_at)."""
    s = LocationState.objects.get(pk=location.pk)
    return (s.status, s.last_heartbeat_at, s.on_since, s.outage_started_at, s.window_start_at)


def _version(location: Any) -> int:
    return LocationState.objects.get(pk=location.pk).state_version


def _queue(
    location: Any,
    kind: str,
    status: str,
    event_at: datetime,
    channel: str = outbox.CHANNEL_SUBSCRIBER,
) -> OutboxMessage:
    """An outbox row as the dump holds it, in ``status``."""
    return OutboxMessage.objects.create(
        channel=channel,
        location=location,
        kind=kind,
        event_at=event_at,
        recorded_at=event_at,
        payload={},
        status=status,
        next_attempt_at=event_at,
        expires_at=event_at + timedelta(hours=6),
    )


def _post_restore(monkeypatch: pytest.MonkeyPatch, now: datetime = NOW) -> str:
    """Run ``manage.py post_restore`` with its clock at ``now``; return what it printed."""
    monkeypatch.setattr(post_restore_command.Command, "clock", FakeClock(now))
    out = StringIO()
    call_command("post_restore", stdout=out)
    return out.getvalue()


def _cursor() -> datetime | None:
    return SystemState.objects.get(pk=1).last_cycle_completed_at


def _on_since_8(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """A location on since 08:00 with a heartbeat every minute until 10:00 (open on piece)."""
    location = location_factory(**overrides)
    _beat_every_minute(location, _at(8, 0), _at(10, 0))
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    return location


# D-13 / D-14: one location, end to end through the command (tracer)


def test_D13_D14_post_restore_restarts_a_location_silently(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=_at(10, 0, 5), resumed=_at(7, 0))
    location = _on_since_8(location_factory)
    LocationState.objects.filter(pk=location.pk).update(window_start_at=_at(9, 0))
    version = _version(location)
    pending = _queue(location, outbox.KIND_POWER_OFF, "pending", _at(7, 0))
    sending = _queue(location, outbox.KIND_POWER_ON, "sending", _at(7, 30))
    incident = OpsIncident.objects.create(
        kind=all_silent.KIND_ALL_SILENT, location=None, started_at=_at(9, 30)
    )

    out = _post_restore(monkeypatch)

    assert _state(location) == ("waiting", None, None, None, None)
    assert _version(location) == version + 1
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0, 5), None),
        ("not_monitored", _at(10, 0, 5), None, None),
    ]
    for row in (pending, sending):
        row.refresh_from_db()
        assert (row.status, row.last_error) == ("dropped", restore.RESTORED)
    incident.refresh_from_db()
    assert incident.ended_at == NOW
    # Nothing new is queued: no expiry, no uncertain notice, no gap notice yet.
    assert OutboxMessage.objects.count() == 2
    # The dump's cursor is untouched: the worker's first carve starts from it.
    assert _cursor() == _at(10, 0, 5)
    assert out == (
        "post_restore: 1 location(s) now wait for their first heartbeat, "
        "2 queued message(s) dropped, 1 open incident(s) closed\n"
    )
    assert location.bot_token not in out
    assert location.device_key not in out


def test_D13_first_heartbeat_after_post_restore_starts_silently(
    location_factory: Callable[..., Any],
) -> None:
    _system(cursor=_at(10, 0, 5), resumed=_at(7, 0))
    location = _on_since_8(location_factory)
    restore.restart_after_restore(NOW)
    queued = OutboxMessage.objects.count()

    assert transitions.record_heartbeat(location.pk, _at(10, 30)) == "started"

    assert _state(location) == ("on", _at(10, 30), _at(10, 30), None, None)
    # The lost hours stay not monitored, closed at the first heartbeat (KD3).
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0, 5), None),
        ("not_monitored", _at(10, 0, 5), _at(10, 30), None),
        ("on", _at(10, 30), None, None),
    ]
    # K-1: the first heartbeat is silent.
    assert OutboxMessage.objects.count() == queued == 0


def test_post_restore_on_an_empty_database_prints_zero_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    singletons = SystemState.objects.count()

    out = _post_restore(monkeypatch)

    assert out == (
        "post_restore: 0 location(s) now wait for their first heartbeat, "
        "0 queued message(s) dropped, 0 open incident(s) closed\n"
    )
    # Nothing is written, not even a system_state singleton.
    assert SystemState.objects.count() == singletons
    assert not OutboxMessage.objects.exists()
    assert not OpsIncident.objects.exists()
