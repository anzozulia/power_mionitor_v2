"""OFF detection: a silent location turns OFF at its last heartbeat (MON-02, ALRT-01).

K-2, the D-14 fresh window and the INV-05 and INV-13 shapes. A transition is one
conditional write, committed together with its outbox row (KD2, D-14). Nothing here sends
anything: the worker relay (01-11) drains the outbox.

``run_cycle`` calls ``close_old_connections()``, which would close the connection inside
pytest-django's per-test transaction, so every test that runs a cycle is
``django_db(transaction=True)``. Those tests truncate every table at teardown, the
system_state singleton included, so each test writes the row it needs.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from powermon.alerts import texts
from powermon.alerts.models import OutboxMessage
from powermon.engine import rules, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.worker import detection

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _outbox(location: Any = None) -> list[OutboxMessage]:
    """The outbox rows (of one location, or all), oldest first."""
    rows = OutboxMessage.objects.order_by("id")
    return list(rows if location is None else rows.filter(location=location))


def _resume_detection(at: datetime | None) -> None:
    """Set the worker start anchor (D-14) on the system_state singleton."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": at, "web_started_at": None}
    )


def _beat_every_minute(location: Any, first: datetime, last: datetime) -> None:
    at = first
    while at <= last:
        transitions.record_heartbeat(location.pk, at)
        at += timedelta(minutes=1)


def _state(location: Any) -> LocationState:
    return LocationState.objects.get(pk=location.pk)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _silent_since_1005(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """A location with heartbeats every 60 s from 10:00:00 to 10:05:00, then silence."""
    location = location_factory(**overrides)
    _beat_every_minute(location, _at(10, 0), _at(10, 5))
    return location


# K-2: OFF strictly after period + grace, backdated to the last heartbeat (MON-02)


@pytest.mark.django_db(transaction=True)
def test_K2_off_after_period_plus_grace_not_at_it(location_factory: Callable[..., Any]) -> None:
    _resume_detection(_at(9, 0))
    location = _silent_since_1005(location_factory)

    # 10:06:30 is exactly period + grace after 10:05:00: not yet OFF.
    assert detection.run_cycle(_at(10, 6, 30)) == 0
    assert _state(location).status == "on"
    assert _outbox() == []

    assert detection.run_cycle(_at(10, 6, 31)) == 1

    state = _state(location)
    assert state.status == "off"
    assert state.outage_started_at == _at(10, 5)
    [row] = _outbox()
    assert row.location_id == location.pk
    assert (row.channel, row.kind, row.status) == ("subscriber", "power_off", "pending")
    assert row.event_at == _at(10, 5)
    assert row.recorded_at == _at(10, 6, 31)
    # "Was ON for" = last heartbeat - on time = 10:05:00 - 10:00:00, in integer µs.
    assert row.payload == {"was_on_us": 300_000_000}
    assert row.next_attempt_at == row.recorded_at
    assert row.expires_at == row.recorded_at + timedelta(hours=6)
    assert (row.attempts, row.last_error, row.sent_at) == (0, "", None)
    assert texts.render_alert(row.kind, location.language, row.payload["was_on_us"]) == (
        "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
    )
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), None, _at(10, 5)),
    ]


@pytest.mark.django_db(transaction=True)
def test_K2_alert_uses_the_location_language(location_factory: Callable[..., Any]) -> None:
    _resume_detection(_at(9, 0))
    location = _silent_since_1005(location_factory, language="uk")

    assert detection.run_cycle(_at(10, 6, 31)) == 1

    [row] = _outbox(location)
    assert texts.render_alert(row.kind, location.language, row.payload["was_on_us"]) == (
        "🔴 <b>СВІТЛО ЗНИКЛО</b>\n⚡ Світло було: <b>5 хв</b>"
    )


@pytest.mark.django_db(transaction=True)
def test_second_cycle_does_not_duplicate_off(location_factory: Callable[..., Any]) -> None:
    _resume_detection(_at(9, 0))
    location = _silent_since_1005(location_factory)
    assert detection.run_cycle(_at(10, 6, 31)) == 1
    version = _state(location).state_version
    timeline = _intervals(location)

    # An off location is no longer a snapshot, so later cycles find nothing to do.
    assert detection.run_cycle(_at(10, 6, 36)) == 0
    assert detection.run_cycle(_at(12, 0)) == 0

    state = _state(location)
    assert (state.status, state.state_version) == ("off", version)
    assert len(_outbox()) == 1
    assert _intervals(location) == timeline


@pytest.mark.django_db(transaction=True)
def test_fresh_window_after_worker_start_D14(location_factory: Callable[..., Any]) -> None:
    # The worker started at 10:06:00, after the last heartbeat at 10:05:00: the silence
    # is counted from the worker start, and the outage starts there (D-14).
    _resume_detection(_at(10, 6))
    location = _silent_since_1005(location_factory)

    assert detection.run_cycle(_at(10, 7, 30)) == 0
    assert _state(location).status == "on"

    assert detection.run_cycle(_at(10, 7, 31)) == 1

    state = _state(location)
    assert (state.status, state.outage_started_at) == ("off", _at(10, 6))
    [row] = _outbox(location)
    assert row.event_at == _at(10, 6)
    # "Was ON for" stays last heartbeat - on time.
    assert row.payload == {"was_on_us": 300_000_000}
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 6), None),
        ("off", _at(10, 6), None, _at(10, 6)),
    ]


@pytest.mark.django_db(transaction=True)
def test_alerts_off_records_off_without_alert(location_factory: Callable[..., Any]) -> None:
    # INV-05 shape: alerts off suppresses the alert, never the state or the timeline.
    _resume_detection(_at(9, 0))
    location = _silent_since_1005(location_factory, alerts_enabled=False)

    assert detection.run_cycle(_at(10, 6, 31)) == 1

    state = _state(location)
    assert (state.status, state.outage_started_at) == ("off", _at(10, 5))
    assert _outbox() == []
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), None, _at(10, 5)),
    ]


@pytest.mark.django_db(transaction=True)
def test_maintenance_and_waiting_locations_are_not_marked_off(
    location_factory: Callable[..., Any],
) -> None:
    _resume_detection(_at(9, 0))
    in_maintenance = _silent_since_1005(location_factory, maintenance=True)
    deleted = _silent_since_1005(location_factory)
    deleted.deleted_at = _at(10, 5, 30)
    deleted.save(update_fields=["deleted_at"])
    waiting = location_factory()
    before = list(LocationState.objects.order_by("pk").values())
    timelines = [_intervals(loc) for loc in (in_maintenance, deleted, waiting)]

    assert detection.run_cycle(_at(18, 0)) == 0

    assert list(LocationState.objects.order_by("pk").values()) == before
    assert _state(waiting).status == "waiting"
    assert [_intervals(loc) for loc in (in_maintenance, deleted, waiting)] == timelines
    assert _outbox() == []


@pytest.mark.django_db(transaction=True)
def test_INV13_error_in_one_location_does_not_stop_others(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _resume_detection(_at(9, 0))
    # A is created first, so the cycle reaches it first (snapshots are ordered by id).
    broken = _silent_since_1005(location_factory)
    healthy = _silent_since_1005(location_factory)
    real_mark_off = transitions.mark_off

    def mark_off(snap: rules.Snapshot, d: rules.Decision, now: datetime) -> bool:
        if snap.location_id == broken.pk:
            raise RuntimeError("simulated failure for one location")
        return real_mark_off(snap, d, now)

    monkeypatch.setattr(transitions, "mark_off", mark_off)
    caplog.set_level(logging.ERROR, logger="powermon.worker.detection")

    assert detection.run_cycle(_at(10, 6, 31)) == 1

    assert _state(broken).status == "on"
    assert _state(healthy).status == "off"
    assert [row.location_id for row in _outbox()] == [healthy.pk]
    [record] = caplog.records
    assert record.getMessage() == f"detection failed for location {broken.pk}"
    assert record.exc_info is not None
    assert "simulated failure for one location" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_run_cycle_recreates_a_missing_system_state_row(
    location_factory: Callable[..., Any],
) -> None:
    # A transactional test elsewhere may have truncated the singleton: the cycle must not
    # crash. Without a worker-start anchor the silence counts from the last heartbeat.
    SystemState.objects.all().delete()
    location = _silent_since_1005(location_factory)

    assert detection.run_cycle(_at(10, 6, 31)) == 1

    assert SystemState.objects.get().detection_resumed_at is None
    assert _state(location).outage_started_at == _at(10, 5)


# The pieces run_cycle is built from: read_snapshots and mark_off


@pytest.mark.django_db(transaction=True)
def test_read_snapshots_lists_only_monitored_on_locations(
    location_factory: Callable[..., Any],
) -> None:
    quiet = _silent_since_1005(location_factory, alerts_enabled=False, router_grace=True)
    loud = _silent_since_1005(location_factory, period_s=120, grace_s=45)
    _silent_since_1005(location_factory, maintenance=True)
    location_factory()

    snaps = transitions.read_snapshots()

    # A snapshot carries no alerts setting: mark_off reads it from its CAS row (D-06).
    assert [snap.location_id for snap in snaps] == [quiet.pk, loud.pk]
    snap = snaps[1]
    assert snap == rules.Snapshot(
        location_id=loud.pk,
        state_version=_state(loud).state_version,
        last_heartbeat_at=_at(10, 5),
        on_since=_at(10, 0),
        window_start_at=None,
        period_s=120,
        grace_s=45,
        router_grace=False,
    )
    assert snaps[0].router_grace is True


@pytest.mark.django_db(transaction=True)
def test_mark_off_rejects_a_decision_that_is_not_off(
    location_factory: Callable[..., Any],
) -> None:
    location = _silent_since_1005(location_factory)
    [snap] = transitions.read_snapshots()

    with pytest.raises(ValueError, match="OFF decision"):
        transitions.mark_off(snap, rules.Decision(off=False), _at(10, 6, 31))

    assert _state(location).status == "on"
    assert _outbox() == []
