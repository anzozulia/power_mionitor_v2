"""The maintenance toggle as an engine transition (LOC-08; D-01, D-02; INV-04, INV-11).

``maintenance.set_maintenance`` is applied at the click (D-02): one transaction takes the
location's ``location_state`` row lock first, sets the flag, closes the open interval at
the click and opens the desired state from it (``not_monitored`` in maintenance), and bumps
``state_version`` so a detector snapshot read before it loses its OFF CAS. The status never
changes: an outage in progress stays one outage through maintenance (INV-11) and its ON
alert is sent as usual when power returns (D-01).

Tests that reach OFF through ``detection.run_cycle`` are ``django_db(transaction=True)``:
the cycle calls ``close_old_connections()``, which would close the connection inside
pytest-django's per-test transaction.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from powermon.alerts.models import OutboxMessage
from powermon.engine import maintenance, transitions
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations.models import Location

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _state(location: Any) -> LocationState:
    return LocationState.objects.get(location=location)


def _maintenance(location: Any) -> bool:
    return Location.objects.get(pk=location.pk).maintenance


@pytest.mark.django_db
def test_set_maintenance_on_splits_the_open_interval_at_the_click(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    version = _state(location).state_version

    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True

    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), None, None),
    ]
    state = _state(location)
    assert state.status == "on"
    assert state.state_version == version + 1
    assert _maintenance(location) is True
    assert OutboxMessage.objects.count() == 0
