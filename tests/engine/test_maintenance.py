"""The maintenance toggle as an engine transition (LOC-08; D-01, D-02; INV-04, INV-11).

``maintenance.set_maintenance`` is applied at the click (D-02): one transaction takes the
location's ``location_state`` row lock first, sets the flag, closes the open interval at
the click and opens the desired state from it (``not_monitored`` in maintenance), and bumps
``state_version`` so a detector snapshot read before it loses its OFF CAS. The status never
changes:

- maintenance only suppresses OFF detection (INV-05), and its span is stored as not
  monitored, never as off (INV-04 #1);
- leaving it with the location on starts a fresh detection window (``window_start_at``):
  an outage then starts at the exit, not at the last heartbeat before it (INV-04 #2);
- an outage in progress stays one outage through maintenance (INV-11) and its ON alert is
  sent as usual when power returns, "was OFF for" counted from the original outage start
  (D-01);
- the toggle clamps its instant to the open interval's start, so a lapse carve that moved
  that start never makes it fail (Pitfall 2).

Tests reach OFF only through ``detection.run_cycle`` and restores only through
``transitions.record_heartbeat``. Tests that run ``detection.run_cycle`` or use race actors
are ``django_db(transaction=True)``: the cycle calls ``close_old_connections()``, which
would close the connection inside pytest-django's per-test transaction, and actors must see
each other's commits.
"""

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from conftest import Actor, blocked_on_lock, terminate_backends, wait_for
from django.db import connection, transaction

from powermon.alerts.models import OutboxMessage
from powermon.chart import source
from powermon.engine import lapse, maintenance, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.i18n import chart_texts
from powermon.locations.models import Location
from powermon.worker import detection

Interval = tuple[str, datetime, datetime | None, datetime | None]

TODAY = date(2026, 10, 1)
KYIV = "Europe/Kyiv"


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _us(**kwargs: float) -> int:
    """A duration as integer microseconds."""
    return timedelta(**kwargs) // timedelta(microseconds=1)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _state(location: Any) -> LocationState:
    return LocationState.objects.get(location=location)


def _maintenance(location: Any) -> bool:
    return Location.objects.get(pk=location.pk).maintenance


def _outbox() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.order_by("id"))


def _kinds() -> list[str]:
    return [row.kind for row in _outbox()]


def _no_anchors() -> None:
    """The process anchors stay out of the way: only the location's own window counts."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _today_row(location: Any, now: datetime) -> Any:
    week = source.load_week(location.pk, today=TODAY, now=now, tz=KYIV, live=True)
    return week.today_row


def _off_since_9(location_factory: Callable[..., Any]) -> Any:
    """On since 08:00, last heartbeat 09:00, OFF recorded by the cycle at 09:01:31."""
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    state = _state(location)
    assert (state.status, state.outage_started_at) == ("off", _at(9, 0))
    assert _kinds() == ["power_off"]
    return location


@pytest.fixture
def lock_holder() -> Iterator[tuple[threading.Event, threading.Event]]:
    """(inside, release) for an actor that holds a location's row lock until released."""
    inside, release = threading.Event(), threading.Event()
    yield inside, release
    release.set()


def _finish(*actors: Actor, release: threading.Event) -> None:
    """Release the holder and join every started actor; end any session still stuck."""
    release.set()
    started = [actor for actor in actors if actor.ident is not None]
    for actor in started:
        actor.join(5)
    if any(actor.is_alive() for actor in started):
        terminate_backends(Actor.APPLICATION_NAME)
        for actor in started:
            actor.join(5)


# The click splits the open interval


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
    assert state.window_start_at is None
    assert _maintenance(location) is True
    assert OutboxMessage.objects.count() == 0


# INV-04 #1 and #2: not monitored, never off; leaving starts a fresh window


@pytest.mark.django_db(transaction=True)
def test_INV04_1_maintenance_span_is_not_monitored_and_sends_nothing(
    location_factory: Callable[..., Any],
) -> None:
    _no_anchors()
    location = location_factory()
    for minute in range(121):
        transitions.record_heartbeat(location.pk, _at(8, 0) + timedelta(minutes=minute))

    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True
    for cycle in (_at(10, 2), _at(10, 30), _at(11, 59)):
        assert detection.run_cycle(cycle) == 0
    assert maintenance.set_maintenance(location.pk, False, _at(12, 0)) is True
    assert transitions.record_heartbeat(location.pk, _at(12, 0, 30)) == "plain"

    assert OutboxMessage.objects.count() == 0
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), _at(12, 0), None),
        ("on", _at(12, 0), None, None),
    ]
    row = _today_row(location, _at(12, 1))
    assert (row.count, row.monitored, row.off_us) == (0, True, 0)
    assert row.nm_us == _us(hours=2)
    # 10:00-12:00 UTC is 13:00-15:00 in Kyiv (EEST), drawn as not monitored.
    assert [(s.start_us, s.end_us) for s in row.segments if s.state == "not_monitored"] == [
        (_us(hours=13), _us(hours=15))
    ]
    assert chart_texts.row_total(row.off_us, row.count, row.monitored, "en") == (
        "no outages",
        "",
    )


@pytest.mark.django_db(transaction=True)
def test_INV04_2_maintenance_exit_starts_a_fresh_window(
    location_factory: Callable[..., Any],
) -> None:
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 58)) == "plain"
    assert maintenance.set_maintenance(location.pk, True, _at(9, 59)) is True

    assert maintenance.set_maintenance(location.pk, False, _at(12, 0)) is True

    assert _state(location).window_start_at == _at(12, 0)
    # Period 60 s + grace 30 s from the exit: not OFF at 12:01:30 (strict >, K-2).
    assert detection.run_cycle(_at(12, 1, 30)) == 0
    assert _kinds() == []
    assert detection.run_cycle(_at(12, 1, 31)) == 1
    state = _state(location)
    assert (state.status, state.outage_started_at) == ("off", _at(12, 0))
    [off] = _outbox()
    assert (off.kind, off.event_at) == ("power_off", _at(12, 0))
    # "Was ON for" stays the literal rule: last heartbeat - on time (09:58 - 08:00).
    assert off.payload == {"was_on_us": _us(hours=1, minutes=58)}
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 59), None),
        ("not_monitored", _at(9, 59), _at(12, 0), None),
        ("off", _at(12, 0), None, _at(12, 0)),
    ]


# INV-11 and D-01: an outage in progress stays one outage, with its ON alert


@pytest.mark.django_db(transaction=True)
def test_INV11_outage_in_progress_stays_one_outage_through_maintenance(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)

    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True
    assert detection.run_cycle(_at(10, 5)) == 0
    assert maintenance.set_maintenance(location.pk, False, _at(10, 10)) is True
    assert detection.run_cycle(_at(10, 20)) == 0

    state = _state(location)
    assert (state.status, state.outage_started_at) == ("off", _at(9, 0))
    assert _kinds() == ["power_off"]
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("off", _at(10, 10), None, _at(9, 0)),
    ]

    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    assert _kinds() == ["power_off", "power_on"]
    on = _outbox()[1]
    # "Was OFF for" = restore - original outage start: the maintenance span is included.
    assert on.payload == {"was_off_us": _us(hours=2)}
    row = _today_row(location, _at(11, 5))
    # The chart's off time excludes the maintenance span: 09:00-10:00 + 10:10-11:00.
    assert (row.off_us, row.count) == (_us(hours=1, minutes=50), 1)
    assert chart_texts.row_total(row.off_us, row.count, row.monitored, "en") == (
        "1h 50m",
        " · 1",
    )


@pytest.mark.django_db(transaction=True)
def test_D01_restore_during_maintenance_sends_the_on_alert(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True

    assert transitions.record_heartbeat(location.pk, _at(10, 5)) == "restored"

    assert _kinds() == ["power_off", "power_on"]
    on = _outbox()[1]
    assert (on.event_at, on.payload) == (_at(10, 5), {"was_off_us": _us(hours=1, minutes=5)})
    # The timeline stays not monitored until maintenance ends.
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), None, None),
    ]

    assert maintenance.set_maintenance(location.pk, False, _at(10, 10)) is True

    state = _state(location)
    assert (state.status, state.window_start_at) == ("on", _at(10, 10))
    assert _intervals(location)[-2:] == [
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("on", _at(10, 10), None, None),
    ]
    assert _kinds() == ["power_off", "power_on"]


# Edges: waiting, idempotency, deleted, the clamp


@pytest.mark.django_db
def test_waiting_location_only_changes_the_flag(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    version = _state(location).state_version

    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True

    state = _state(location)
    assert (state.status, state.state_version, state.window_start_at) == (
        "waiting",
        version,
        None,
    )
    assert _maintenance(location) is True
    assert _intervals(location) == []

    # The first heartbeat during maintenance opens not monitored, silently.
    assert transitions.record_heartbeat(location.pk, _at(10, 5)) == "started"

    assert _intervals(location) == [("not_monitored", _at(10, 5), None, None)]
    assert OutboxMessage.objects.count() == 0


@pytest.mark.django_db
def test_second_toggle_is_a_no_op(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    other = location_factory(name="Other")
    for place in (location, other):
        assert transitions.record_heartbeat(place.pk, _at(8, 0)) == "started"
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True
    intervals, version = _intervals(location), _state(location).state_version

    assert maintenance.set_maintenance(location.pk, True, _at(10, 5)) is False

    assert _intervals(location) == intervals
    assert _state(location).state_version == version
    assert _maintenance(location) is True

    other_version = _state(other).state_version
    assert maintenance.set_maintenance(other.pk, False, _at(10, 5)) is False

    assert _intervals(other) == [("on", _at(8, 0), None, None)]
    assert _state(other).state_version == other_version
    assert _maintenance(other) is False


@pytest.mark.django_db
def test_deleted_location_is_not_toggled(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    Location.objects.filter(pk=location.pk).update(deleted_at=_at(9, 0))
    version = _state(location).state_version

    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is False

    assert _maintenance(location) is False
    assert _state(location).state_version == version
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    # No location (and no state row) at all: nothing to toggle.
    assert maintenance.set_maintenance(location.pk + 1000, True, _at(10, 0)) is False


@pytest.mark.django_db
def test_toggle_clamps_to_a_carve_moved_open_start(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    # A lapse carve that committed after the web read its now moved the open piece's start
    # past it (timeline.overwrite re-inserts the tail at the window's end).
    assert lapse.carve_window(_at(9, 59, 30), _at(10, 0, 5)) == 1
    assert _intervals(location)[-1] == ("on", _at(10, 0, 5), None, None)

    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True

    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 59, 30), None),
        ("not_monitored", _at(9, 59, 30), _at(10, 0, 5), None),
        ("not_monitored", _at(10, 0, 5), None, None),
    ]


# Concurrency: a racing snapshot loses its OFF; the toggle waits for the row lock


@pytest.mark.django_db(transaction=True)
def test_snapshot_taken_before_maintenance_loses_its_off(
    monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    _no_anchors()
    location = location_factory()
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    real = transitions.read_snapshots

    def snapshot_then_maintenance() -> Any:
        snapshots = real()
        # The admin clicks between the detector's snapshot and its decision.
        assert maintenance.set_maintenance(location.pk, True, _at(10, 6, 30)) is True
        return snapshots

    monkeypatch.setattr(transitions, "read_snapshots", snapshot_then_maintenance)

    assert detection.run_cycle(_at(10, 6, 31)) == 0

    state = _state(location)
    assert (state.status, state.outage_started_at) == ("on", None)
    assert _kinds() == []
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 6, 30), None),
        ("not_monitored", _at(10, 6, 30), None, None),
    ]


@pytest.mark.django_db(transaction=True)
def test_heartbeat_and_toggle_serialize_on_the_row_lock(
    location_factory: Callable[..., Any],
    lock_holder: tuple[threading.Event, threading.Event],
) -> None:
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    inside, release = lock_holder

    def hold_the_row_lock() -> None:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(transitions.LOCK_SQL, [location.pk])
            cur.fetchone()
            inside.set()
            if not release.wait(5):
                raise AssertionError("the lock holder was never released")

    holder = Actor(hold_the_row_lock)
    toggle = Actor(lambda: maintenance.set_maintenance(location.pk, True, _at(10, 0)))
    try:
        holder.start()
        assert inside.wait(5)
        toggle.start()
        assert wait_for(lambda: toggle.pid is not None and blocked_on_lock(toggle.pid))
        # Still waiting on the state row lock: the flag is not set yet.
        assert toggle.is_alive()
        assert _maintenance(location) is False
        release.set()
        toggle.join(5)
    finally:
        _finish(holder, toggle, release=release)

    assert holder.exc is None, holder.exc
    assert toggle.exc is None, toggle.exc
    assert toggle.result is True
    assert _maintenance(location) is True
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), None, None),
    ]
