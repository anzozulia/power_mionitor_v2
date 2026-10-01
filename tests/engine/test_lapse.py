"""Server downtime is not monitored, never an outage (MON-05, OPS-02; INV-10, INV-11, D-02/3/4).

What these scenarios prove:
- INV-10 #1: after the whole stack was down 10:00-10:10, every location's timeline shows
  that window as not monitored, subscribers get nothing for it, and the admin gets exactly
  one gap notice with its start, end and length (D-04, D-11 #1).
- INV-11 #1 (alert part): a location that went silent during the downtime gets one OFF,
  backdated to the end of the not-monitored window, with "was ON for" = last heartbeat -
  on time (D-03), and "was OFF for" counts from that start.
- INV-11 #2 (alert part): an outage already running when the downtime started stays one
  outage, with no second OFF, and its ON says "was OFF for" = restore - the original start
  (D-02).
- The carve is crash safe and exactly-once: the cursor UPDATE is conditional on the cursor
  that was read, so a re-run after a crash or a second carver adds no second notice.
- Every detection cycle checks for a lapse before it decides anything (D-04, D-15): a new
  lease generation, a re-established connection or a failed cycle forces the carve
  whatever the gap's length; otherwise a gap over 15 s since the last completed cycle (a
  stall, INV-10 #3 and #4) does. A wall-clock step over 5 s against the monotonic clock
  skips the cycle's decisions: forward it is a lapse, backward it records nothing.
- A heartbeat that waited on the carve's row lock restores at the carve end with 200,
  never a CHECK violation (Pitfall 5).

Histories are built through ``record_heartbeat`` and ``run_cycle`` wherever a scenario
describes device behaviour, and cycles run through ``detection.run_detection`` with a
``FakeClock``, a ``CycleTracker`` and explicit lease generations. The tests are
``django_db(transaction=True)``: ``run_cycle`` calls ``close_old_connections()``, which
would close the connection inside pytest-django's per-test transaction, and the race test
needs each actor's commits to be visible to the other; the teardown truncates the
system_state singleton, so each test writes the row it needs. All times are fixed aware
datetimes on 2026-10-01 UTC; the display TZ is pinned to Europe/Kyiv (UTC+3 that day).
"""

import dataclasses
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import Actor, FakeClock, blocked_on_lock, terminate_backends, wait_for
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.backends.utils import CursorWrapper
from django.test import RequestFactory

from powermon.alerts import ops, outbox, texts
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import lapse, timeline, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.web.views import HeartbeatView
from powermon.worker import detection

Interval = tuple[str, datetime, datetime | None, datetime | None]
OPS_LOGGER = "powermon.alerts.ops"
GAP_10_00_10_10 = (
    "⏸ Monitoring gap 01.10 13:00:00 – 13:10:00 (10m). "
    "Recorded as not monitored; no subscriber alerts were sent for it."
)


def _at(hour: int, minute: int, second: int = 0, microsecond: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, microsecond, tzinfo=UTC)


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    return settings


@pytest.fixture
def no_ops_chat(settings: Any) -> Any:
    """``settings.CFG`` with no ops chat, whatever the env file says (D-09)."""
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token="", ops_chat_id=None)
    return settings


def _system(
    cursor: datetime | None, resumed: datetime | None = None, web: datetime | None = None
) -> None:
    """Write the system_state singleton: the cursor and the detection anchors."""
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "last_cycle_completed_at": cursor,
            "detection_resumed_at": resumed,
            "web_started_at": web,
        },
    )


def _anchors() -> tuple[datetime | None, datetime | None]:
    """``(last_cycle_completed_at, detection_resumed_at)``."""
    row = SystemState.objects.get(pk=1)
    return row.last_cycle_completed_at, row.detection_resumed_at


def _beat_every_minute(location: Any, first: datetime, last: datetime) -> None:
    at = first
    while at <= last:
        transitions.record_heartbeat(location.pk, at)
        at += timedelta(minutes=1)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _incidents() -> list[tuple[str, int | None, datetime, datetime | None]]:
    rows = OpsIncident.objects.order_by("id")
    return [(r.kind, r.location_id, r.started_at, r.ended_at) for r in rows]


def _ops_rows() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS).order_by("id"))


def _subscriber_rows(location: Any = None) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_SUBSCRIBER).order_by("id")
    return list(rows if location is None else rows.filter(location=location))


def _gap_payload(start: datetime, end: datetime) -> dict[str, int]:
    return {"start_us": ops.instant_us(start), "end_us": ops.instant_us(end)}


def _gap(start: datetime, end: datetime) -> tuple[str, None, datetime, datetime]:
    """A closed monitoring_gap incident as ``_incidents()`` lists it."""
    return (lapse.KIND_MONITORING_GAP, None, start, end)


def _spy_on_decisions(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Record the ``now`` of every decisions step (``detection.run_cycle``), then run it."""
    calls: list[datetime] = []
    real = detection.run_cycle

    def spy(now: datetime, tick: Callable[[], None] | None = None) -> int:
        calls.append(now)
        return real(now, tick=tick)

    monkeypatch.setattr(detection, "run_cycle", spy)
    return calls


# INV-10 #1: the whole stack down 10:00-10:10


@pytest.mark.django_db(transaction=True)
def test_INV10_stack_down_10_min_zero_alerts_one_gap_notice(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))
    locations = [location_factory(name=f"Location {n}") for n in range(3)]
    for location in locations:
        _beat_every_minute(location, _at(9, 55), _at(10, 0))

    gap = lapse.carve_if_needed(_at(10, 10), force=True)

    assert gap == lapse.Gap(_at(10, 0), _at(10, 10))
    for location in locations:
        assert _intervals(location) == [
            ("on", _at(9, 55), _at(10, 0), None),
            ("not_monitored", _at(10, 0), _at(10, 10), None),
            ("on", _at(10, 10), None, None),
        ]
    assert _anchors() == (_at(10, 10), _at(10, 10))
    assert _incidents() == [(lapse.KIND_MONITORING_GAP, None, _at(10, 0), _at(10, 10))]
    [notice] = _ops_rows()
    assert (notice.kind, notice.location_id) == (outbox.KIND_OPS_GAP, None)
    assert notice.payload == _gap_payload(_at(10, 0), _at(10, 10))
    assert (notice.recorded_at, notice.next_attempt_at) == (_at(10, 10), _at(10, 10))
    text = ops.render_text(notice.kind, notice.payload, notice.location_id, now=_at(10, 10))
    assert text == GAP_10_00_10_10

    # Devices are back at 10:10:30; the fresh window from 10:10 means no OFF follows.
    for location in locations:
        transitions.record_heartbeat(location.pk, _at(10, 10, 30))
    for now in (_at(10, 11), _at(10, 11, 30), _at(10, 12)):
        assert detection.run_cycle(now) == 0
    assert _subscriber_rows() == []
    assert len(_ops_rows()) == 1


# INV-11 #1: an outage that starts during the downtime


@pytest.mark.django_db(transaction=True)
def test_INV11_outage_during_downtime_one_off_was_off_50m(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 59)) == "plain"

    assert lapse.carve_if_needed(_at(10, 10), force=True) == lapse.Gap(_at(10, 0), _at(10, 10))

    # The window counts from the end of the lapse: 10:11:30 is exactly 90 s after it.
    assert detection.run_cycle(_at(10, 11, 30)) == 0
    assert detection.run_cycle(_at(10, 11, 31)) == 1
    [off] = _subscriber_rows(location)
    assert (off.kind, off.event_at) == (outbox.KIND_POWER_OFF, _at(10, 10))
    # D-03: "was ON for" = last heartbeat - on time; the downtime never counts as ON.
    assert off.payload == {"was_on_us": (_at(9, 59) - _at(8, 0)) // timedelta(microseconds=1)}
    assert _intervals(location)[-2:] == [
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("off", _at(10, 10), None, _at(10, 10)),
    ]

    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    _off, on = _subscriber_rows(location)
    assert (on.kind, on.payload) == (outbox.KIND_POWER_ON, {"was_off_us": 50 * 60 * 1_000_000})
    assert texts.render_alert(on.kind, "en", on.payload["was_off_us"]) == (
        "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>50m</b>"
    )


# INV-11 #2: an outage already in progress stays one outage


@pytest.mark.django_db(transaction=True)
def test_INV11_outage_in_progress_stays_one_was_off_2h(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(7, 0))
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1  # OFF since 09:00, its alert queued
    _system(cursor=_at(10, 0), resumed=_at(7, 0))

    assert lapse.carve_if_needed(_at(10, 10), force=True) == lapse.Gap(_at(10, 0), _at(10, 10))

    # The location stays off: no fresh-window re-detection, no second OFF.
    assert detection.run_cycle(_at(10, 12)) == 0
    assert detection.run_cycle(_at(10, 30)) == 0
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("off", _at(10, 10), None, _at(9, 0)),
    ]

    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    rows = _subscriber_rows(location)
    assert [r.kind for r in rows] == [outbox.KIND_POWER_OFF, outbox.KIND_POWER_ON]
    # D-02: "was OFF for" = restore - the original outage start, the lapse included.
    assert rows[1].payload == {"was_off_us": 2 * 3600 * 1_000_000}
    assert texts.render_alert(rows[1].kind, "en", rows[1].payload["was_off_us"]) == (
        "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>2h</b>"
    )
    assert _intervals(location)[-2:] == [
        ("off", _at(10, 10), _at(11, 0), _at(9, 0)),
        ("on", _at(11, 0), None, None),
    ]


# Edges: the first ever start, the threshold, a backward step, no data


@pytest.mark.django_db(transaction=True)
def test_MON05_first_ever_start_sets_the_cursor_without_carve_or_notice(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    # The first Phase 2 deploy finds last_cycle_completed_at NULL (Pitfall 13).
    _system(cursor=None, resumed=_at(9, 0))
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 30))
    before = _intervals(location)

    assert lapse.carve_if_needed(_at(10, 10), force=True) is None

    assert _anchors() == (_at(10, 10), _at(10, 10))
    assert _intervals(location) == before == [("on", _at(9, 30), None, None)]
    assert _incidents() == []
    assert _ops_rows() == []


@pytest.mark.django_db(transaction=True)
def test_MON05_start_fresh_only_moves_a_missing_cursor() -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))

    assert lapse.start_fresh(_at(10, 10)) is False
    assert _anchors() == (_at(10, 0), _at(9, 0))

    _system(cursor=None, resumed=_at(9, 0))
    assert lapse.start_fresh(_at(10, 10)) is True
    assert _anchors() == (_at(10, 10), _at(10, 10))


@pytest.mark.django_db(transaction=True)
def test_MON05_read_cursor_recreates_a_missing_singleton() -> None:
    # Transactional tests truncate the singleton; detection must not stop on that.
    SystemState.objects.all().delete()

    assert lapse.read_cursor() is None
    assert SystemState.objects.filter(pk=1).exists()


@pytest.mark.django_db(transaction=True)
def test_MON05_gap_at_the_threshold_is_not_a_lapse(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    now = _at(10, 10)
    _system(cursor=now - lapse.LAPSE_THRESHOLD, resumed=_at(9, 0))

    # Exactly 15 s since the last completed cycle: not a lapse (strict >).
    assert lapse.carve_if_needed(now, force=False) is None
    assert _intervals(location) == [("on", _at(9, 0), None, None)]
    assert _anchors() == (now - lapse.LAPSE_THRESHOLD, _at(9, 0))
    assert (_incidents(), _ops_rows()) == ([], [])

    # 15 s + 1 us is one.
    start = now - lapse.LAPSE_THRESHOLD - timedelta(microseconds=1)
    _system(cursor=start, resumed=_at(9, 0))
    assert lapse.carve_if_needed(now, force=False) == lapse.Gap(start, now)
    assert _intervals(location) == [
        ("on", _at(9, 0), start, None),
        ("not_monitored", start, now, None),
        ("on", now, None, None),
    ]
    [notice] = _ops_rows()
    # The exact window, in integer microseconds (no float on the way).
    assert notice.payload == _gap_payload(start, now)
    assert notice.payload["end_us"] - notice.payload["start_us"] == 15_000_001


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("now", [_at(10, 0), _at(9, 0)], ids=["same-instant", "an-hour-back"])
def test_MON05_backward_clock_step_never_carves(
    location_factory: Callable[..., Any], ops_settings: Any, now: datetime
) -> None:
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(8, 0))
    _system(cursor=_at(10, 0), resumed=_at(8, 0))

    # Even a forced carve has an empty window when now is not after the cursor.
    assert lapse.carve_if_needed(now, force=True) is None

    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    assert _anchors() == (_at(10, 0), _at(8, 0))
    assert (_incidents(), _ops_rows()) == ([], [])


@pytest.mark.django_db(transaction=True)
def test_MON05_waiting_deleted_and_maintenance_locations_get_no_piece(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))
    waiting = location_factory(name="Never heard from")
    deleted = location_factory(name="Deleted")
    transitions.record_heartbeat(deleted.pk, _at(9, 0))
    type(deleted).objects.filter(pk=deleted.pk).update(deleted_at=_at(9, 30))
    maintenance = location_factory(name="In maintenance", maintenance=True)
    transitions.record_heartbeat(maintenance.pk, _at(9, 0))
    ticks: list[int] = []

    assert lapse.carve_window(_at(10, 0), _at(10, 10), tick=lambda: ticks.append(1)) == 0

    # No data stays no data (K-1), and a deleted location's history is not rewritten.
    assert _intervals(waiting) == []
    assert _intervals(deleted) == [("on", _at(9, 0), None, None)]
    # Maintenance is already not monitored: visited (one tick) but not changed.
    assert _intervals(maintenance) == [("not_monitored", _at(9, 0), None, None)]
    assert ticks == [1]


# Crash safety and exactly one notice (D-04, ARCHITECTURE Pattern 4)


@pytest.mark.django_db(transaction=True)
def test_INV10_crash_mid_carve_reruns_idempotently_one_notice(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    real_record_gap = lapse.record_gap
    crashes = [RuntimeError("the worker died before the cursor transaction")]

    def crash_once(cursor: datetime, now: datetime) -> bool:
        if crashes:
            raise crashes.pop()
        return real_record_gap(cursor, now)

    monkeypatch.setattr(lapse, "record_gap", crash_once)

    with pytest.raises(RuntimeError, match="died"):
        lapse.carve_if_needed(_at(10, 10), force=True)

    # The per-location overwrites committed; the cursor, incident and notice did not.
    assert _intervals(location) == [
        ("on", _at(9, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("on", _at(10, 10), None, None),
    ]
    assert _anchors() == (_at(10, 0), _at(9, 0))
    assert (_incidents(), _ops_rows()) == ([], [])

    # The restarted worker carves [cursor, now') again: one notice, the longer window.
    later = _at(10, 10, 5)
    assert lapse.carve_if_needed(later, force=True) == lapse.Gap(_at(10, 0), later)

    assert _incidents() == [(lapse.KIND_MONITORING_GAP, None, _at(10, 0), later)]
    [notice] = _ops_rows()
    assert notice.payload == _gap_payload(_at(10, 0), later)
    # Contiguous not monitored [10:00, 10:10:05): the re-run only appended.
    assert _intervals(location) == [
        ("on", _at(9, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("not_monitored", _at(10, 10), later, None),
        ("on", later, None, None),
    ]
    assert _anchors() == (later, later)


@pytest.mark.django_db(transaction=True)
def test_INV10_a_second_carver_with_a_stale_cursor_records_nothing(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    assert lapse.carve_if_needed(_at(10, 10), force=True) == lapse.Gap(_at(10, 0), _at(10, 10))
    rows = _intervals(location)
    # A second carver that read the cursor before the first one committed.
    monkeypatch.setattr(lapse, "read_cursor", lambda: _at(10, 0))

    assert lapse.carve_if_needed(_at(10, 10), force=True) is None

    assert _intervals(location) == rows
    assert _incidents() == [_gap(_at(10, 0), _at(10, 10))]
    assert len(_ops_rows()) == 1


@pytest.mark.django_db(transaction=True)
def test_INV10_a_losing_carver_with_a_later_now_never_blocks_the_off(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D2 (final audit): two carvers read the same cursor. The winner records the gap with
    # now 10:10:00.000; the loser carves up to 10:10:00.200 and loses the cursor CAS. The
    # open on piece then starts after the fresh window the winner opened.
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 59)) == "plain"
    winner, loser = _at(10, 10), _at(10, 10, 0, 200_000)
    assert lapse.carve_if_needed(winner, force=True) == lapse.Gap(_at(10, 0), winner)
    monkeypatch.setattr(lapse, "read_cursor", lambda: _at(10, 0))

    assert lapse.carve_if_needed(loser, force=True) is None

    assert _anchors() == (winner, winner)
    assert _intervals(location)[-2:] == [
        ("not_monitored", winner, loser, None),
        ("on", loser, None, None),
    ]

    # Silent since 09:59: the fresh window from 10:10:00.000 runs out after 10:11:30.
    assert detection.run_cycle(_at(10, 11, 30)) == 0
    assert detection.run_cycle(_at(10, 11, 31)) == 1
    for later in (_at(10, 30), _at(12, 0)):
        assert detection.run_cycle(later) == 0

    # Exactly one OFF and one alert, starting where the open piece starts, never before.
    [off] = _subscriber_rows(location)
    assert (off.kind, off.event_at, off.recorded_at) == (
        outbox.KIND_POWER_OFF,
        loser,
        _at(10, 11, 31),
    )
    # D-03: "was ON for" is still last heartbeat - on time.
    assert off.payload == {"was_on_us": (_at(9, 59) - _at(8, 0)) // timedelta(microseconds=1)}
    assert _intervals(location)[-2:] == [
        ("not_monitored", winner, loser, None),
        ("off", loser, None, loser),
    ]
    assert LocationState.objects.get(pk=location.pk).outage_started_at == loser

    # "Was OFF for" then counts from that start.
    assert transitions.record_heartbeat(location.pk, _at(12, 10)) == "restored"
    _off, on = _subscriber_rows(location)
    assert on.payload == {"was_off_us": (_at(12, 10) - loser) // timedelta(microseconds=1)}


@pytest.mark.django_db(transaction=True)
def test_INV10_two_carvers_one_gap_notice(ops_settings: Any) -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))

    assert lapse.record_gap(_at(10, 0), _at(10, 10)) is True
    # A second carver that read the same cursor loses the conditional UPDATE.
    assert lapse.record_gap(_at(10, 0), _at(10, 10)) is False

    assert _incidents() == [(lapse.KIND_MONITORING_GAP, None, _at(10, 0), _at(10, 10))]
    assert [n.payload for n in _ops_rows()] == [_gap_payload(_at(10, 0), _at(10, 10))]
    assert _anchors() == (_at(10, 10), _at(10, 10))


@pytest.mark.django_db(transaction=True)
def test_gap_notice_is_logged_when_the_ops_chat_is_not_configured(
    no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _system(cursor=_at(10, 0), resumed=_at(9, 0))
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    assert lapse.record_gap(_at(10, 0), _at(10, 10)) is True

    lines = [r.getMessage() for r in caplog.records if r.name == OPS_LOGGER]
    assert lines == [f"ops notice (ops chat not configured): {GAP_10_00_10_10}"]
    assert _ops_rows() == []
    assert len(_incidents()) == 1


# The incident table's contract (D-11; 02-08 opens and closes all-silent incidents on it)


@pytest.mark.django_db(transaction=True)
def test_ops_incident_allows_one_open_incident_per_kind_and_location(
    location_factory: Callable[..., Any],
) -> None:
    first, second = location_factory(name="First"), location_factory(name="Second")
    OpsIncident.objects.create(kind="all_silent", started_at=_at(10, 0))
    # A NULL location counts as one value: a second open global incident is refused.
    with pytest.raises(IntegrityError, match="ops_incident_one_open"), transaction.atomic():
        OpsIncident.objects.create(kind="all_silent", started_at=_at(10, 1))

    # Closed incidents, other kinds and other locations are unaffected.
    OpsIncident.objects.create(kind="all_silent", started_at=_at(9, 0), ended_at=_at(9, 5))
    OpsIncident.objects.create(kind="all_silent", started_at=_at(8, 0), ended_at=_at(8, 5))
    OpsIncident.objects.create(kind="delivery_failing", started_at=_at(10, 0))
    OpsIncident.objects.create(kind="delivery_failing", location=first, started_at=_at(10, 0))
    OpsIncident.objects.create(kind="delivery_failing", location=second, started_at=_at(10, 0))
    with pytest.raises(IntegrityError, match="ops_incident_one_open"), transaction.atomic():
        OpsIncident.objects.create(kind="delivery_failing", location=first, started_at=_at(11, 0))

    assert OpsIncident.objects.filter(ended_at__isnull=True).count() == 4
    assert str(OpsIncident.objects.get(location=first)).startswith("ops incident ")


# The cycle tracker: what the previous cycle saw (D-04 triggers, D-15 clock step)


def test_cycle_tracker_measures_the_clock_step_against_the_monotonic_clock() -> None:
    tracker = lapse.CycleTracker()
    assert (tracker.generation, tracker.pid, tracker.db_failed) == (0, None, False)
    # The first cycle has nothing to compare with.
    assert tracker.clock_step(_at(10, 0), 100.0) is None

    tracker.db_failed = True
    tracker.remember(3, 4242, _at(10, 0), 100.0)

    assert (tracker.generation, tracker.pid, tracker.db_failed) == (3, 4242, False)
    assert tracker.clock_step(_at(10, 0, 5), 105.0) == 0.0
    assert tracker.clock_step(_at(10, 0, 13), 105.0) == 8.0
    assert tracker.clock_step(_at(9, 0, 5), 105.0) == -3600.0


# INV-10 #3 and #4: a stalled detection loop


@pytest.mark.django_db(transaction=True)
def test_INV10_detection_stall_is_not_monitored_one_notice(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None)
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    locations = [location_factory(name=f"Location {n}") for n in range(3)]

    def beat_on_the_minute() -> None:
        if clock.now().second == 0:
            for location in locations:
                transitions.record_heartbeat(location.pk, clock.now())

    while clock.now() <= _at(10, 10):
        beat_on_the_minute()
        assert detection.run_detection(clock, 1, tracker) == 0
        clock.advance(seconds=5)
    # The detection loop stalls 10:10-10:14; the web app keeps accepting heartbeats.
    while clock.now() < _at(10, 14):
        beat_on_the_minute()
        clock.advance(seconds=5)
    beat_on_the_minute()

    assert detection.run_detection(clock, 1, tracker) == 0

    for location in locations:
        assert _intervals(location) == [
            ("on", _at(10, 0), _at(10, 10), None),
            ("not_monitored", _at(10, 10), _at(10, 14), None),
            ("on", _at(10, 14), None, None),
        ]
    assert _subscriber_rows() == []
    assert _incidents() == [_gap(_at(10, 10), _at(10, 14))]
    assert [n.payload for n in _ops_rows()] == [_gap_payload(_at(10, 10), _at(10, 14))]
    assert _anchors() == (_at(10, 14), _at(10, 14))


@pytest.mark.django_db(transaction=True)
def test_INV10_stall_with_heartbeat_inside_timeline_split(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None)
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(10, 0))
    while clock.now() <= _at(10, 1):
        detection.run_detection(clock, 1, tracker)
        clock.advance(seconds=5)
    # Cycles stall from 10:01:00; the next heartbeat arrives at 10:01:35.
    clock.advance(seconds=30)
    assert transitions.record_heartbeat(location.pk, clock.now()) == "plain"
    clock.advance(seconds=5)

    assert detection.run_detection(clock, 1, tracker) == 0

    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 1), None),
        ("not_monitored", _at(10, 1), _at(10, 1, 40), None),
        ("on", _at(10, 1, 40), None, None),
    ]
    assert _subscriber_rows() == []
    assert _incidents() == [_gap(_at(10, 1), _at(10, 1, 40))]
    assert len(_ops_rows()) == 1


# The forced triggers (D-04): new generation, re-established connection, failed cycle


@pytest.mark.django_db(transaction=True)
def test_MON05_new_lease_generation_forces_a_carve_whatever_the_gap(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=None)
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    detection.run_detection(clock, 1, tracker)
    seen_at_decisions: list[tuple[datetime | None, datetime | None]] = []
    real = detection.run_cycle

    def decisions(now: datetime, tick: Callable[[], None] | None = None) -> int:
        # The carve's cursor transaction committed before any timeout decision (D-04).
        seen_at_decisions.append(_anchors())
        return real(now, tick=tick)

    monkeypatch.setattr(detection, "run_cycle", decisions)
    clock.advance(seconds=5)

    # The lease was lost and reacquired: generation 2, a 5 s gap.
    detection.run_detection(clock, 2, tracker)

    assert _incidents() == [_gap(_at(10, 0), _at(10, 0, 5))]
    assert len(_ops_rows()) == 1
    assert _intervals(location) == [
        ("on", _at(9, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), _at(10, 0, 5), None),
        ("on", _at(10, 0, 5), None, None),
    ]
    assert seen_at_decisions == [(_at(10, 0, 5), _at(10, 0, 5))]
    assert tracker.generation == 2


@pytest.mark.django_db(transaction=True)
def test_MON05_two_normal_cycles_with_the_same_connection_do_not_carve(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    # Pitfall 3: a recycled connection would look like a reconnect and carve every cycle.
    _system(cursor=None)
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    detection.run_detection(clock, 1, tracker)
    pid = tracker.pid
    clock.advance(seconds=5)

    detection.run_detection(clock, 1, tracker)

    assert pid is not None
    assert tracker.pid == pid == connection.connection.info.backend_pid
    assert (_incidents(), _ops_rows()) == ([], [])
    assert _anchors() == (_at(10, 0, 5), _at(10, 0))
    assert _intervals(location) == [("on", _at(9, 0), None, None)]


@pytest.mark.django_db(transaction=True)
def test_MON05_failed_cycle_forces_a_carve_on_the_next_success(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None)
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    detection.run_detection(clock, 1, tracker)
    # The detection loop saw the connection drop in the previous cycle.
    tracker.db_failed = True
    clock.advance(seconds=5)

    detection.run_detection(clock, 1, tracker)

    assert _incidents() == [_gap(_at(10, 0), _at(10, 0, 5))]
    assert tracker.db_failed is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "error",
    [RuntimeError("a bug"), OperationalError("canceling statement due to statement timeout")],
    ids=["bug", "statement-timeout"],
)
def test_MON05_a_failing_decisions_step_forces_no_further_carve(
    location_factory: Callable[..., Any],
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    # Plan-check advisory 1: the lapse check commits before the decisions, so a decisions
    # step that raises on every cycle neither keeps the generation trigger armed nor lets
    # the cursor fall behind the 15 s threshold: at most one gap notice in total.
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    _system(cursor=_at(9, 59, 55), resumed=_at(9, 0))
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()

    def failing(now: datetime, tick: Callable[[], None] | None = None) -> int:
        raise error

    monkeypatch.setattr(detection, "run_cycle", failing)

    for _ in range(8):  # 40 s of failing cycles, 5 s apart
        with pytest.raises(type(error)):
            detection.run_detection(clock, 1, tracker)
        clock.advance(seconds=5)

    # Only the generation's first cycle carved; the cursor kept up with every cycle.
    assert _incidents() == [_gap(_at(9, 59, 55), _at(10, 0))]
    assert len(_ops_rows()) == 1
    assert _anchors() == (_at(10, 0, 35), _at(10, 0))
    assert tracker.generation == 1


# D-15: a wall-clock step skips the cycle's decisions


@pytest.mark.django_db(transaction=True)
def test_MON05_forward_clock_step_records_lapse_skips_decisions(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=None)
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(9, 0))
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    detection.run_detection(clock, 1, tracker)
    decisions = _spy_on_decisions(monkeypatch)
    clock.advance(seconds=5)
    # The wall clock jumps 8 s ahead of the monotonic clock (Δwall 13 s, Δmono 5 s).
    clock.set(clock.now() + timedelta(seconds=8))

    assert detection.run_detection(clock, 1, tracker) == 0

    assert decisions == []
    assert _incidents() == [_gap(_at(10, 0), _at(10, 0, 13))]
    assert len(_ops_rows()) == 1
    assert _anchors() == (_at(10, 0, 13), _at(10, 0, 13))

    clock.advance(seconds=5)
    detection.run_detection(clock, 1, tracker)
    assert decisions == [_at(10, 0, 18)]
    assert len(_incidents()) == 1


@pytest.mark.django_db(transaction=True)
def test_MON05_backward_clock_step_no_carve_no_off(
    location_factory: Callable[..., Any],
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _system(cursor=None)
    location = location_factory()
    transitions.record_heartbeat(location.pk, _at(10, 0))
    clock = FakeClock(_at(10, 0))
    tracker = lapse.CycleTracker()
    detection.run_detection(clock, 1, tracker)
    decisions = _spy_on_decisions(monkeypatch)
    caplog.set_level(logging.WARNING, logger=detection.__name__)
    clock.advance(seconds=5)
    # The wall clock steps an hour back (Δwall -3595 s, Δmono 5 s: a step of -3600 s).
    clock.set(clock.now() - timedelta(hours=1))

    assert detection.run_detection(clock, 1, tracker) == 0

    assert decisions == []
    assert (_incidents(), _ops_rows()) == ([], [])
    # The cursor never moves backwards and history before it is never rewritten.
    assert _anchors() == (_at(10, 0), _at(10, 0))
    assert _intervals(location) == [("on", _at(10, 0), None, None)]
    warnings = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == detection.__name__]
    assert warnings == [
        (logging.WARNING, "wall clock stepped back 3600 s; skipping this cycle's decisions")
    ]

    # Later cycles decide again, and the stored anchors (now in the future) give no OFF.
    for _ in range(3):
        clock.advance(seconds=5)
        assert detection.run_detection(clock, 1, tracker) == 0
    assert len(decisions) == 3
    assert _subscriber_rows() == []
    assert LocationState.objects.get(pk=location.pk).status == "on"
    assert len([r for r in caplog.records if r.name == detection.__name__]) == 1


# Pitfall 5: a heartbeat that waited on the carve's row lock


def _beat(location: Any, clock: FakeClock) -> int:
    """One device request to /hb with the location's Bearer key; the response status code."""
    request = RequestFactory().get(
        "/hb", headers={"authorization": f"Bearer {location.device_key}"}
    )
    response = HeartbeatView.as_view(clock=clock)(request)
    return int(response.status_code)


@pytest.mark.django_db(transaction=True)
def test_INV10_heartbeat_during_carve_clamps_no_500(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=None, resumed=_at(7, 0))
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1  # off since 09:00
    _system(cursor=_at(10, 0), resumed=_at(7, 0))
    inside, release = threading.Event(), threading.Event()
    real = timeline.overwrite

    def paused(
        cur: CursorWrapper, location_id: int, a: datetime, b: datetime, state: str = "not_monitored"
    ) -> int:
        changed = real(cur, location_id, a, b, state)
        # Still inside the carve's transaction, holding the location's row lock.
        inside.set()
        if not release.wait(5):
            raise AssertionError("the race hook was never released")
        return changed

    monkeypatch.setattr(timeline, "overwrite", paused)
    carve = Actor(lambda: lapse.carve_window(_at(10, 0), _at(10, 10)))
    # The device's request is received at 10:09:59, before the carve's end.
    beat = Actor(lambda: _beat(location, FakeClock(_at(10, 9, 59))))
    try:
        carve.start()
        assert inside.wait(5)
        beat.start()
        assert wait_for(lambda: beat.pid is not None and blocked_on_lock(beat.pid))
        release.set()
        carve.join(5)
        beat.join(5)
    finally:
        release.set()
        for actor in (carve, beat):
            if actor.ident is not None:
                actor.join(5)
        if carve.is_alive() or beat.is_alive():
            terminate_backends(Actor.APPLICATION_NAME)

    assert carve.exc is None, carve.exc
    assert beat.exc is None, beat.exc  # never an IntegrityError (power_interval_end_after_start)
    assert (carve.result, beat.result) == (1, 200)
    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.on_since) == ("on", _at(10, 10))
    [_off, on] = _subscriber_rows(location)
    assert (on.kind, on.event_at) == (outbox.KIND_POWER_ON, _at(10, 10))
    assert on.payload == {"was_off_us": 70 * 60 * 1_000_000}
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("on", _at(10, 10), None, None),
    ]
