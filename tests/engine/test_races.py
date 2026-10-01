"""Races on real PostgreSQL: exactly one transition and one alert per event (MON-04, ALRT-05).

What the races prove:
- WR-01: a device heartbeat that arrives while the detector's OFF transaction holds the
  location's row lock waits for it and then restores the location. It is never dropped
  as "ignored" (HB-03: it still does no network I/O and answers right after the lock).

Every writer of a location's state and timeline takes that location's ``location_state``
row lock first (``SELECT ... FOR UPDATE`` in ``record_heartbeat``, the CAS UPDATE in
``mark_off``), so writers of one location are serialized and two of them can neither both
win nor both lose (RESEARCH Pattern 6).

Each actor is a thread on its own connection, so these tests are
``django_db(transaction=True)``: the actors must see each other's commits, which the
per-test transaction of a plain ``django_db`` test would hide. The interleaving is
deterministic, never a ``time.sleep`` race (RESEARCH Pitfall 8):
- a hook wraps ``timeline.set_open_state`` (the module attribute the gates call), so one
  transaction pauses inside itself after it holds the row lock, for well under 2 s;
- the waiting actor is confirmed blocked through ``pg_stat_activity.wait_event_type``;
- the hook is released and every actor joined in ``finally``, and a stuck actor's session
  is terminated, so pytest-django's teardown TRUNCATE never waits on a stray transaction.

All times are fixed aware datetimes on 2026-10-01 UTC.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import Actor, FakeClock, blocked_on_lock, terminate_backends, wait_for
from django.db.backends.utils import CursorWrapper
from django.test import RequestFactory

from powermon.alerts.models import OutboxMessage
from powermon.engine import rules, timeline, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.web.views import HeartbeatView

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _resume_detection(at: datetime) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": at, "web_started_at": None}
    )


def _state(location: Any) -> LocationState:
    return LocationState.objects.get(pk=location.pk)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _outbox(location: Any = None) -> list[OutboxMessage]:
    """The outbox rows (of one location, or all), oldest first."""
    rows = OutboxMessage.objects.order_by("id")
    return list(rows if location is None else rows.filter(location=location))


def _kinds(location: Any = None) -> list[str]:
    """The kinds of the outbox rows (of one location, or all), oldest first."""
    return [row.kind for row in _outbox(location)]


def _beat(location: Any, clock: FakeClock) -> int:
    """One device request to /hb with the location's Bearer key; the response status code."""
    request = RequestFactory().get(
        "/hb", headers={"authorization": f"Bearer {location.device_key}"}
    )
    response = HeartbeatView.as_view(clock=clock)(request)
    return int(response.status_code)


def _pause_in_set_open_state(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> tuple[threading.Event, threading.Event]:
    """Pause the first transaction that writes ``state`` to the timeline; (inside, release).

    The gates call ``timeline.set_open_state`` after they hold the location's row lock, so
    the paused transaction keeps that lock until the test sets ``release`` (at most 5 s).
    ``state`` "off" pauses an OFF (``mark_off``), "on" pauses a restore.
    """
    inside, release = threading.Event(), threading.Event()
    real = timeline.set_open_state

    def hooked(
        cur: CursorWrapper,
        location_id: int,
        at: datetime,
        new_state: str | None,
        outage_start_at: datetime | None = None,
    ) -> None:
        if new_state == state and not inside.is_set():
            inside.set()
            if not release.wait(5):
                raise AssertionError("the race hook was never released")
        real(cur, location_id, at, new_state, outage_start_at)

    monkeypatch.setattr(timeline, "set_open_state", hooked)
    return inside, release


def _finish(release: threading.Event, *actors: Actor) -> None:
    """Release the hook and join every started actor; end any session still stuck."""
    release.set()
    started = [actor for actor in actors if actor.ident is not None]
    for actor in started:
        actor.join(5)
    if any(actor.is_alive() for actor in started):
        terminate_backends(Actor.APPLICATION_NAME)
        for actor in started:
            actor.join(5)


def _on_since_1000_silent_after_1005(location_factory: Callable[..., Any]) -> Any:
    """K-2: heartbeats every minute 10:00-10:05, detection resumed at 09:00."""
    _resume_detection(_at(9, 0))
    location = location_factory()
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    return location


# WR-01: a heartbeat racing the OFF transaction restores the location


@pytest.mark.django_db(transaction=True)
def test_MON04_WR01_heartbeat_during_off_cas_restores(
    monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _on_since_1000_silent_after_1005(location_factory)
    [(snap, _alerts_enabled)] = transitions.read_snapshots()
    decision = rules.decide(snap, rules.Anchors(detection_resumed_at=_at(9, 0)), _at(10, 6, 31))
    assert (decision.off, decision.outage_start) == (True, _at(10, 5))
    inside, release = _pause_in_set_open_state(monkeypatch, "off")

    off = Actor(lambda: transitions.mark_off(snap, decision, _at(10, 6, 31), True))
    hb = Actor(lambda: _beat(location, FakeClock(_at(10, 6, 32))))
    try:
        off.start()
        assert inside.wait(5)
        # The device reports while the OFF transaction holds the row lock.
        hb.start()
        assert wait_for(lambda: hb.pid is not None and blocked_on_lock(hb.pid))
        release.set()
        released = time.monotonic()
        hb.join(5)
        answered_after = time.monotonic() - released
    finally:
        _finish(release, off, hb)

    assert off.exc is None, off.exc
    assert hb.exc is None, hb.exc
    assert (off.result, hb.result) == (True, 200)
    # HB-03: the waiting heartbeat is applied right after the OFF commits.
    assert answered_after < 1.0
    state = _state(location)
    assert (state.status, _kinds()) == ("on", ["power_off", "power_on"])
    assert (state.on_since, state.last_heartbeat_at) == (_at(10, 6, 32), _at(10, 6, 32))
    _off_row, on_row = _outbox()
    assert (on_row.event_at, on_row.recorded_at) == (_at(10, 6, 32), _at(10, 6, 32))
    # "Was OFF for" = 10:06:32 - 10:05:00.
    assert on_row.payload == {"was_off_us": 92_000_000}
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), _at(10, 6, 32), _at(10, 5)),
        ("on", _at(10, 6, 32), None, None),
    ]
