"""The stored power timeline (KD1, MON-01; K-1 timeline part, PITFALLS 15).

Heartbeats write the timeline in the same transaction as the state change, and
PostgreSQL itself rejects overlapping, zero-length, second-open and inconsistent
intervals, so no code path can corrupt the one source of truth. 01-08 adds K-1's
"no alert" assertion and K-3 to this file.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from powermon.engine import transitions
from powermon.engine.models import LocationState

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    from powermon.engine.models import PowerInterval

    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _set_open_state(
    location: Any, at: datetime, state: str | None, outage_start_at: datetime | None = None
) -> None:
    from powermon.engine import timeline

    with transaction.atomic(), connection.cursor() as cur:
        timeline.set_open_state(cur, location.pk, at, state, outage_start_at)


def _insert(location: Any, state: str, start: datetime, end: datetime | None, **extra: Any) -> None:
    from powermon.engine.models import PowerInterval

    PowerInterval.objects.create(
        location=location, state=state, start_at=start, end_at=end, **extra
    )


def _assert_rejected(constraint: str, location: Any, *args: Any, **extra: Any) -> None:
    # The savepoint keeps the test transaction usable after the failed INSERT.
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        _insert(location, *args, **extra)


# The first heartbeat opens the timeline (K-1, MON-01)


@pytest.mark.django_db
def test_K1_first_heartbeat_opens_one_on_interval(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    from powermon.engine.models import PowerInterval

    location = location_factory()
    assert fixed_now == _at(8, 0)

    assert transitions.record_heartbeat(location.pk, fixed_now) == "started"

    assert LocationState.objects.get(pk=location.pk).status == "on"
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    # No data before the first heartbeat: nothing starts before 08:00:00.
    assert not PowerInterval.objects.filter(location=location, start_at__lt=fixed_now).exists()


@pytest.mark.django_db
def test_plain_heartbeats_keep_one_open_interval(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    from powermon.engine.models import PowerInterval

    location = location_factory()
    transitions.record_heartbeat(location.pk, fixed_now)
    opened = PowerInterval.objects.get(location=location)

    results = [transitions.record_heartbeat(location.pk, _at(8, m)) for m in (1, 2)]

    assert results == ["plain", "plain"]
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    assert PowerInterval.objects.get(location=location).pk == opened.pk


@pytest.mark.django_db
def test_first_heartbeat_in_maintenance_opens_not_monitored(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(maintenance=True)

    assert transitions.record_heartbeat(location.pk, fixed_now) == "started"

    # The live status is on; the timeline records the span as not monitored (LOC-08).
    assert LocationState.objects.get(pk=location.pk).status == "on"
    assert _intervals(location) == [("not_monitored", _at(8, 0), None, None)]


@pytest.mark.django_db
def test_record_heartbeat_for_an_unknown_location_writes_no_interval(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    from powermon.engine.models import PowerInterval

    location_factory()

    assert transitions.record_heartbeat(10**9, fixed_now) == "ignored"

    assert not PowerInterval.objects.exists()


# set_open_state: close then open, never zero length, idempotent


@pytest.mark.django_db
def test_set_open_state_closes_then_opens(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(8, 0), "on")

    _set_open_state(location, _at(10, 5), "off", _at(10, 5))

    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 5), None),
        ("off", _at(10, 5), None, _at(10, 5)),
    ]


@pytest.mark.django_db
def test_set_open_state_deletes_zero_length_interval(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(10, 0), "on")

    _set_open_state(location, _at(10, 0), "off", _at(10, 0))

    assert _intervals(location) == [("off", _at(10, 0), None, _at(10, 0))]


@pytest.mark.django_db
def test_set_open_state_is_idempotent(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(8, 0), "on")
    _set_open_state(location, _at(9, 0), "on")
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    _set_open_state(location, _at(10, 5), "off", _at(10, 5))
    before = _intervals(location)

    # Same state and same outage start as the open interval: nothing changes.
    _set_open_state(location, _at(10, 6), "off", _at(10, 5))
    _set_open_state(location, _at(10, 7), "off", _at(10, 5))

    assert _intervals(location) == before


@pytest.mark.django_db
def test_set_open_state_new_outage_start_is_a_new_off_interval(
    location_factory: Callable[..., Any],
) -> None:
    # INV-01 acceptance 2 shape: off since 10:01, restore at 13:00, silence again.
    location = location_factory()
    _set_open_state(location, _at(10, 1), "off", _at(10, 1))
    _set_open_state(location, _at(13, 0), "on")

    _set_open_state(location, _at(13, 0), "off", _at(13, 0))

    assert _intervals(location) == [
        ("off", _at(10, 1), _at(13, 0), _at(10, 1)),
        ("off", _at(13, 0), None, _at(13, 0)),
    ]


@pytest.mark.django_db
def test_set_open_state_none_only_closes(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(8, 0), "on")

    _set_open_state(location, _at(9, 0), None)
    _set_open_state(location, _at(9, 30), None)

    assert _intervals(location) == [("on", _at(8, 0), _at(9, 0), None)]


@pytest.mark.django_db
def test_set_open_state_rejects_closing_before_the_open_start(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    _set_open_state(location, _at(10, 0), "on")

    with pytest.raises(IntegrityError, match="power_interval_end_after_start"):
        _set_open_state(location, _at(9, 59), "off", _at(9, 59))

    assert _intervals(location) == [("on", _at(10, 0), None, None)]


# PostgreSQL enforces the timeline invariants (KD1, PITFALLS 15)


@pytest.mark.django_db
def test_db_rejects_overlapping_intervals(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    other = location_factory()
    _insert(location, "on", _at(8, 0), _at(10, 0))

    _assert_rejected("power_interval_no_overlap", location, "on", _at(9, 0), _at(11, 0))

    # Half-open [start, end): an adjacent interval does not overlap.
    _insert(location, "off", _at(10, 0), _at(11, 0), outage_start_at=_at(10, 0))
    # Another location's interval may overlap freely.
    _insert(other, "on", _at(8, 30), _at(9, 30))
    assert len(_intervals(location)) == 2
    assert len(_intervals(other)) == 1


@pytest.mark.django_db
def test_db_rejects_second_open_interval(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _insert(location, "on", _at(8, 0), None)

    _assert_rejected("power_interval_one_open", location, "on", _at(9, 0), None)


@pytest.mark.django_db
def test_db_rejects_zero_length_interval(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    _assert_rejected("power_interval_end_after_start", location, "on", _at(8, 0), _at(8, 0))
    _assert_rejected("power_interval_end_after_start", location, "on", _at(8, 0), _at(7, 59))


@pytest.mark.django_db
def test_db_rejects_off_without_outage_start(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    _assert_rejected("power_interval_outage_start_iff_off", location, "off", _at(8, 0), None)


@pytest.mark.django_db
def test_db_rejects_on_with_outage_start(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    for state in ("on", "not_monitored"):
        _assert_rejected(
            "power_interval_outage_start_iff_off",
            location,
            state,
            _at(8, 0),
            None,
            outage_start_at=_at(8, 0),
        )


@pytest.mark.django_db
def test_db_rejects_unknown_interval_state(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    _assert_rejected("power_interval_state_valid", location, "waiting", _at(8, 0), None)


@pytest.mark.django_db
def test_system_state_is_a_singleton() -> None:
    from powermon.engine.models import SystemState

    row = SystemState.objects.get()
    assert row.pk == 1
    assert (row.web_started_at, row.last_cycle_completed_at, row.detection_resumed_at) == (
        None,
        None,
        None,
    )

    with pytest.raises(IntegrityError, match="system_state_singleton"), transaction.atomic():
        SystemState.objects.create(id=2)
