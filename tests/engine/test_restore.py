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
import hashlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, OPS_BOT_TOKEN, OPS_CHAT_ID, FakeClock
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.db.models import F, Model, Value
from django.db.models.functions import Greatest
from django.test.utils import CaptureQueriesContext

from powermon.alerts import outbox
from powermon.alerts.delivery import KIND_DELIVERY_FAILING
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart.lifecycle import KIND_CHART_PIN_FAILED
from powermon.chart.models import ChartMessage
from powermon.engine import all_silent, lapse, maintenance, restore, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations.models import Location
from powermon.web.management.commands import post_restore as post_restore_command
from powermon.worker import detection, io_loop
from powermon.worker.lease import Lease, LeaseState

pytestmark = pytest.mark.django_db(transaction=True)

# Every table the restart may write, for "nothing changed" checks.
TABLES: dict[str, type[Model]] = {
    "location": Location,
    "location_state": LocationState,
    "power_interval": PowerInterval,
    "outbox_message": OutboxMessage,
    "ops_incident": OpsIncident,
    "system_state": SystemState,
}

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


def _off_since_9(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """On since 08:00, silent after its 09:00 heartbeat: OFF since 09:00, its alert pending.

    The OFF comes from the detector's cycle at 09:01:31 (period 60 s + grace 30 s), so the
    caller writes ``system_state`` (the detection window) first.
    """
    location = location_factory(**overrides)
    _beat_every_minute(location, _at(8, 0), _at(9, 0))
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), None, _at(9, 0)),
    ]
    return location


def _snapshot() -> dict[str, list[dict[str, Any]]]:
    """Every row of every table the restart may write, by table, in primary-key order."""
    return {
        name: list(model._default_manager.order_by("pk").values()) for name, model in TABLES.items()
    }


def _subscriber_rows() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(channel=outbox.CHANNEL_SUBSCRIBER).order_by("id"))


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass with charts, the detection cursor moved to the clock's now (never back).

    The worker's detection thread keeps the cursor within a cycle of now.
    """
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
    return io_loop.run_iteration(clock, state, charts=True)


# D-13 / D-14: one location, end to end through the command (tracer)


def test_D13_D14_post_restore_restarts_a_location_silently(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, ops_settings: Any
) -> None:
    # The ops chat is configured, so any notice would be queued as a row (none may be).
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


# SC4: the worker's first start on the restored database


def test_SC4_worker_after_post_restore_sends_only_the_gap_notice(
    location_factory: Callable[..., Any], ops_settings: Any, fake_telegram: Any
) -> None:
    # The dump: one location off since 09:00 with its OFF alert still pending (Telegram
    # was down), one on since 08:00; the last completed detection cycle at 10:00.
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    off = _off_since_9(location_factory, name="Off location")
    on = _on_since_8(location_factory, name="On location")
    [off_alert] = _subscriber_rows()
    assert (off_alert.kind, off_alert.status) == (outbox.KIND_POWER_OFF, "pending")
    assert restore.restart_after_restore(_at(10, 15)) == restore.RestoreCounts(2, 1, 0)

    # The worker starts at 10:20: activation, then the forced carve of a new lease term.
    clock = FakeClock(_at(10, 20))
    relay = io_loop.RelayState()
    assert io_loop.activate(relay, clock) == 0
    assert lapse.carve_if_needed(_at(10, 20), force=True) == lapse.Gap(_at(10, 0), _at(10, 20))

    [notice] = OutboxMessage.objects.exclude(status="dropped")
    assert (notice.channel, notice.kind, notice.location_id) == (
        outbox.CHANNEL_OPS,
        outbox.KIND_OPS_GAP,
        None,
    )
    # The carve skips waiting locations: both keep the not-monitored piece from 10:00.
    assert _intervals(off)[-2:] == [
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), None, None),
    ]
    assert _intervals(on)[-1] == ("not_monitored", _at(10, 0), None, None)

    fake_telegram.accept(OPS_BOT_TOKEN)
    assert _pass(clock, relay) is True

    # One request in all: the gap notice to the admin. No alert to the location's chat,
    # no chart call (waiting locations make none).
    assert len(fake_telegram.calls) == 1
    assert [message["chat_id"] for message in fake_telegram.sent] == [OPS_CHAT_ID]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendMessage") == 0
    notice.refresh_from_db()
    assert notice.status == "sent"
    assert [(row.kind, row.status) for row in _subscriber_rows()] == [
        (outbox.KIND_POWER_OFF, "dropped")
    ]


# D-13: maintenance, outages in progress, the cursor rule, untouched locations


def test_D13_maintenance_location_keeps_its_flag_and_piece(
    location_factory: Callable[..., Any],
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = location_factory()
    _beat_every_minute(location, _at(8, 0), _at(9, 30))
    assert maintenance.set_maintenance(location.pk, True, _at(9, 30))
    in_maintenance = [
        ("on", _at(8, 0), _at(9, 30), None),
        ("not_monitored", _at(9, 30), None, None),
    ]
    assert _intervals(location) == in_maintenance

    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(1, 0, 0)

    assert _state(location) == ("waiting", None, None, None, None)
    location.refresh_from_db()
    assert location.maintenance is True
    # Already not monitored: the open piece is kept as it is.
    assert _intervals(location) == in_maintenance

    # The first heartbeat during maintenance opens not monitored (Phase 4 D-02): the piece
    # stays as it is.
    assert transitions.record_heartbeat(location.pk, _at(10, 30)) == "started"
    assert _state(location) == ("on", _at(10, 30), _at(10, 30), None, None)
    assert _intervals(location) == in_maintenance
    assert not OutboxMessage.objects.exists()


def test_D13_outage_in_progress_at_the_dump_never_gets_its_on_alert(
    location_factory: Callable[..., Any],
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = _off_since_9(location_factory)

    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(1, 1, 0)

    assert _state(location) == ("waiting", None, None, None, None)
    # The off piece ends at the dump's cursor and keeps its outage start.
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), None, None),
    ]

    # Power is back by the first heartbeat: a silent start, no ON alert (the accepted
    # trade-off of D-13), and the dump's OFF alert never goes out either.
    assert transitions.record_heartbeat(location.pk, _at(10, 30)) == "started"
    assert _intervals(location)[-2:] == [
        ("not_monitored", _at(10, 0), _at(10, 30), None),
        ("on", _at(10, 30), None, None),
    ]
    assert [(row.kind, row.status) for row in _subscriber_rows()] == [
        (outbox.KIND_POWER_OFF, "dropped")
    ]


def test_D13_cursor_before_the_open_start_uses_the_open_start(
    location_factory: Callable[..., Any],
) -> None:
    # Monitoring started at 10:10, after the last completed cycle (10:00).
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = location_factory()
    _beat_every_minute(location, _at(10, 10), _at(10, 12))

    restore.restart_after_restore(NOW)

    # Never before the open piece's start: the piece is replaced from that start.
    assert _intervals(location) == [("not_monitored", _at(10, 10), None, None)]


def test_D13_no_cursor_uses_the_open_start(location_factory: Callable[..., Any]) -> None:
    _system(cursor=None, resumed=_at(7, 0))
    location = _on_since_8(location_factory)

    restore.restart_after_restore(NOW)

    assert _intervals(location) == [("not_monitored", _at(8, 0), None, None)]
    # Still no cursor: the worker starts fresh, with no gap notice.
    assert _cursor() is None


def test_D13_deleted_and_waiting_locations_are_untouched(
    location_factory: Callable[..., Any],
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    deleted = _on_since_8(location_factory, name="Deleted location")
    Location.objects.filter(pk=deleted.pk).update(deleted_at=_at(9, 0))
    location_factory(name="Waiting location")
    before = _snapshot()

    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(0, 0, 0)

    assert _snapshot() == before


# D-14: every open incident closes quietly


def test_D14_every_open_incident_closes_quietly(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    location = location_factory()
    still_open = [
        OpsIncident.objects.create(
            kind=all_silent.KIND_ALL_SILENT, location=None, started_at=_at(9, 0)
        ),
        OpsIncident.objects.create(
            kind=KIND_DELIVERY_FAILING,
            location=location,
            started_at=_at(9, 10),
            details={"http_status": 403},
        ),
        OpsIncident.objects.create(
            kind=KIND_CHART_PIN_FAILED, location=location, started_at=_at(9, 20)
        ),
    ]
    closed = OpsIncident.objects.create(
        kind=lapse.KIND_MONITORING_GAP, location=None, started_at=_at(8, 0), ended_at=_at(8, 5)
    )

    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(0, 0, 3)

    for incident in still_open:
        incident.refresh_from_db()
        assert incident.ended_at == NOW
    closed.refresh_from_db()
    assert closed.ended_at == _at(8, 5)
    # No recovery or end notice, with the ops chat configured.
    assert not OutboxMessage.objects.exists()


# Idempotency, the worker-lock guard, a naive now


def test_post_restore_twice_changes_nothing_the_second_time(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    _off_since_9(location_factory, name="Off location")
    _on_since_8(location_factory, name="On location")
    OpsIncident.objects.create(kind=all_silent.KIND_ALL_SILENT, location=None, started_at=_at(9, 0))
    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(2, 1, 1)
    after_first = _snapshot()

    assert restore.restart_after_restore(_at(10, 25)) == restore.RestoreCounts(0, 0, 0)

    assert _snapshot() == after_first


def test_post_restore_refuses_while_a_worker_holds_the_lock(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = _on_since_8(location_factory)
    _queue(location, outbox.KIND_POWER_OFF, "pending", _at(7, 0))
    OpsIncident.objects.create(kind=all_silent.KIND_ALL_SILENT, location=None, started_at=_at(9, 0))
    before = _snapshot()
    lease = Lease(connection.settings_dict, FakeClock(NOW))
    try:
        assert lease.ensure_held().state == LeaseState.HELD
        assert restore.worker_lock_held() is True

        with pytest.raises(restore.WorkerActive):
            restore.restart_after_restore(NOW)
        with pytest.raises(CommandError, match="stop web and worker before post_restore"):
            _post_restore(monkeypatch)

        assert _snapshot() == before
    finally:
        lease.close()

    assert restore.worker_lock_held() is False
    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(1, 1, 1)


def test_restart_after_restore_rejects_a_naive_now() -> None:
    with pytest.raises(ValueError, match="aware now"):
        restore.restart_after_restore(datetime(2026, 10, 1, 10, 20))  # noqa: DTZ001


def test_post_restore_refuses_in_build_mode(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    _on_since_8(location_factory)
    before = _snapshot()
    settings.CFG = dataclasses.replace(settings.CFG, build=True)

    with pytest.raises(CommandError, match="build mode"):
        _post_restore(monkeypatch)

    assert _snapshot() == before


def test_D13_status_is_read_again_under_the_row_lock(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # As if, after the ids were read, one location went back to waiting and another lost
    # its state row: under the row lock both are skipped, with nothing written.
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    on = _on_since_8(location_factory, name="On location")
    waiting = location_factory(name="Waiting location")
    stateless = location_factory(name="No state row")
    LocationState.objects.filter(pk=stateless.pk).delete()
    monkeypatch.setattr(restore, "MONITORED_SQL", "SELECT id FROM location ORDER BY id")
    waiting_before = LocationState.objects.filter(pk=waiting.pk).values().get()

    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(1, 0, 0)

    assert LocationState.objects.filter(pk=waiting.pk).values().get() == waiting_before
    assert not LocationState.objects.filter(pk=stateless.pk).exists()
    assert not PowerInterval.objects.exclude(location=on).exists()
    assert _state(on)[0] == "waiting"


def test_D13_location_without_an_open_interval_only_waits(
    location_factory: Callable[..., Any],
) -> None:
    # A monitored status with no open interval (nothing to mark as not monitored): the
    # location only goes back to waiting, and no interval is written.
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = location_factory()
    LocationState.objects.filter(pk=location.pk).update(
        status="on", on_since=_at(8, 0), last_heartbeat_at=_at(10, 0)
    )

    assert restore.restart_after_restore(NOW) == restore.RestoreCounts(1, 0, 0)

    assert _state(location) == ("waiting", None, None, None, None)
    assert _intervals(location) == []


# RESEARCH Pitfall 3: the FIRST gate after a restore


def test_Pitfall3_first_gate_clamps_to_the_open_start(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = location_factory()
    _beat_every_minute(location, _at(8, 0), _at(9, 59))
    restore.restart_after_restore(NOW)
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), None, None),
    ]
    monkeypatch.setattr(transitions, "_restore_clamp_warned", False)
    caplog.set_level(logging.WARNING, logger=transitions.__name__)

    # The new server's clock is behind the dump: the first heartbeat is received at 09:59.
    assert transitions.record_heartbeat(location.pk, _at(9, 59)) == "started"

    assert _state(location) == ("on", _at(10, 0), _at(10, 0), None, None)
    # The not-monitored piece would end where it starts: it is deleted, never closed
    # before its start (no IntegrityError, no 500 on every heartbeat).
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0), None),
        ("on", _at(10, 0), None, None),
    ]
    assert not OutboxMessage.objects.exists()
    warnings = [r for r in caplog.records if r.name == transitions.__name__]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert f"location {location.pk} " in warnings[0].getMessage()


def test_first_gate_without_an_open_interval_is_unchanged(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()

    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"

    assert _state(location) == ("on", _at(8, 0), _at(8, 0), None, None)
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    assert not OutboxMessage.objects.exists()


# D-16: the read-only history fingerprint of the restore drill

FINGERPRINT_LINE = re.compile(r"^(location|power_interval|chart_message) (\d+) ([0-9a-f]{32})$")


def _fingerprint_command() -> str:
    """What ``manage.py history_fingerprint`` prints."""
    out = StringIO()
    call_command("history_fingerprint", stdout=out)
    return out.getvalue()


def _by_table() -> dict[str, tuple[int, str]]:
    """``restore.fingerprint()`` as {table: (count, checksum)}."""
    return {table: (count, checksum) for table, count, checksum in restore.fingerprint()}


def _history(location_factory: Callable[..., Any]) -> Any:
    """Rows in all three tables: an off location with a chart record, and an on one.

    Three intervals (the off location's on and open off pieces, the other's open on
    piece), two locations, one chart record. Returns the off location.
    """
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = _off_since_9(location_factory, name="Office")
    _on_since_8(location_factory, name="Home")
    ChartMessage.objects.create(
        location=location,
        local_date=date(2026, 10, 1),
        chat_id=location.chat_id,
        bot_key="0123456789ab",
        message_id=1001,
        pinned=True,
        last_rendered_at=_at(10, 0),
        created_at=_at(8, 0),
    )
    return location


def _chart_rows() -> list[dict[str, Any]]:
    return list(ChartMessage.objects.order_by("pk").values())


def test_D16_fingerprint_is_stable_for_equal_data(location_factory: Callable[..., Any]) -> None:
    _history(location_factory)

    first = _fingerprint_command()
    second = _fingerprint_command()

    assert first == second
    lines = first.splitlines()
    matches = [FINGERPRINT_LINE.match(line) for line in lines]
    assert [m is not None for m in matches] == [True, True, True]
    assert [line.split(" ")[0] for line in lines] == list(restore.FINGERPRINT_TABLES)
    assert restore.FINGERPRINT_TABLES == ("location", "power_interval", "chart_message")
    assert [int(line.split(" ")[1]) for line in lines] == [2, 3, 1]
    # The command prints what fingerprint() returns, one line per table.
    assert restore.fingerprint() == [
        (table, int(count), checksum)
        for table, count, checksum in (line.split(" ") for line in lines)
    ]


def test_D16_fingerprint_changes_when_any_row_changes(
    location_factory: Callable[..., Any],
) -> None:
    location = _history(location_factory)
    before = _by_table()

    # One interval ends one microsecond earlier: only power_interval's checksum moves.
    piece = PowerInterval.objects.filter(location=location, end_at__isnull=False).get()
    PowerInterval.objects.filter(pk=piece.pk).update(end_at=F("end_at") - timedelta(microseconds=1))
    moved = _by_table()
    assert moved["location"] == before["location"]
    assert moved["chart_message"] == before["chart_message"]
    assert moved["power_interval"][0] == before["power_interval"][0] == 3
    assert moved["power_interval"][1] != before["power_interval"][1]

    # A new bot token changes the location checksum (through its md5), nothing else.
    Location.objects.filter(pk=location.pk).update(bot_token="987654321:" + "B" * 35)
    rotated = _by_table()
    assert rotated["location"][0] == 2
    assert rotated["location"][1] != moved["location"][1]
    assert (rotated["power_interval"], rotated["chart_message"]) == (
        moved["power_interval"],
        moved["chart_message"],
    )

    # Counts track inserts.
    location_factory(name="New location")
    assert _by_table()["location"][0] == 3


def test_D16_fingerprint_is_read_only(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(location_factory)
    rows, charts = _snapshot(), _chart_rows()

    _fingerprint_command()
    restore.fingerprint()

    assert (_snapshot(), _chart_rows()) == (rows, charts)
    # READ ONLY ended with fingerprint()'s own transaction.
    with connection.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone() == ("off",)

    # A statement that writes is refused inside the fingerprint's transaction.
    monkeypatch.setitem(
        restore.FINGERPRINT_SQL,
        "power_interval",
        "UPDATE power_interval SET end_at = end_at - interval '1 microsecond' "
        "WHERE end_at IS NOT NULL RETURNING 1, ''",
    )
    with pytest.raises(DatabaseError, match="read-only transaction"):
        restore.fingerprint()
    assert (_snapshot(), _chart_rows()) == (rows, charts)


def test_D16_fingerprint_refuses_to_run_inside_a_transaction(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()

    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        with pytest.raises(RuntimeError, match="must run outside a transaction"):
            restore.fingerprint()
    # Refused before any statement ran, so no READ ONLY was set.
    assert len(queries) == 0

    # A write on the same connection still succeeds afterwards.
    Location.objects.filter(pk=location.pk).update(name="Renamed")
    assert Location.objects.get(pk=location.pk).name == "Renamed"
    with connection.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone() == ("off",)


def test_D16_fingerprint_prints_no_secret(location_factory: Callable[..., Any]) -> None:
    location = _history(location_factory)

    out = _fingerprint_command()

    token = location.bot_token
    assert token == DEFAULT_BOT_TOKEN
    secret = token.split(":", 1)[1]
    for value in (token, secret, location.device_key):
        assert value not in out
    # Not even their md5: the hashes only enter the aggregate.
    for value in (token, location.device_key):
        assert hashlib.md5(value.encode(), usedforsecurity=False).hexdigest() not in out


def test_D16_fingerprint_of_empty_tables() -> None:
    assert restore.fingerprint() == [
        ("location", 0, ""),
        ("power_interval", 0, ""),
        ("chart_message", 0, ""),
    ]
    assert _fingerprint_command().splitlines() == [
        "location 0 ",
        "power_interval 0 ",
        "chart_message 0 ",
    ]
