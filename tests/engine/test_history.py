"""History corrections in the engine (DATA-02; D-01, D-02, D-04; INV-07 #1, #3).

``history.recent_outages`` reads the stored timeline as the chart does (KD1): one outage per
``outage_start_at`` (D-01, the chart's count rule), newest first, over the last 14 local
days plus the current outage. ``history.remove_outage`` is one transaction that takes the
location's ``location_state`` row lock first:

- an outage in progress (stored status off with that outage start, also while its open
  piece is not monitored) is refused under the lock (INV-07 #3);
- each off piece of the outage becomes on, one piece at a time, so not-monitored time inside
  it stays not monitored (D-02);
- the outage's queued alerts are dropped only when its OFF alert never went out (D-04 as
  refined on 2026-10-03): an OFF that is sending, sent or uncertain keeps its ON;
- live state (status, last heartbeat, on since, outage start, window start, state version)
  is never written, nothing is queued and nothing is sent (INV-07 #1).

Histories are built only through ``transitions.record_heartbeat``, ``detection.run_cycle``
and ``maintenance.set_maintenance``, with aware UTC times on the fixed day 2026-10-01. Tests
that run ``detection.run_cycle`` or race actors are ``django_db(transaction=True)``: the
cycle calls ``close_old_connections()``, which would close the connection inside
pytest-django's per-test transaction, and actors must see each other's commits.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from conftest import FakeTelegram

from powermon.alerts.models import OutboxMessage
from powermon.chart import source
from powermon.engine import history, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.i18n import chart_texts
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


def _live(location: Any) -> tuple[Any, ...]:
    """Every live detection field of the location's state row (INV-07: never written)."""
    state = LocationState.objects.get(location=location)
    return (
        state.status,
        state.last_heartbeat_at,
        state.on_since,
        state.outage_started_at,
        state.window_start_at,
        state.state_version,
    )


def _outbox() -> list[tuple[str, datetime, str, str]]:
    """Every outbox row as (kind, event_at, status, last_error), oldest first."""
    rows = OutboxMessage.objects.order_by("id")
    return [(r.kind, r.event_at, r.status, r.last_error) for r in rows]


def _no_anchors() -> None:
    """The process anchors stay out of the way: only the location's own window counts."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _today_row(location: Any, now: datetime) -> Any:
    week = source.load_week(location.pk, today=TODAY, now=now, tz=KYIV, live=True)
    return week.today_row


def _total(row: Any) -> tuple[str, str]:
    return chart_texts.row_total(row.off_us, row.count, row.monitored, "en")


def _two_outages(location_factory: Callable[..., Any]) -> Any:
    """INV-07 #1's day: on since 08:00, outages 09:00-10:00 and 15:00-15:30, on again."""
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    # Period 60 s + grace 30 s after the last heartbeat: OFF from 09:00 (K-2).
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    assert transitions.record_heartbeat(location.pk, _at(15, 0)) == "plain"
    assert detection.run_cycle(_at(15, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(15, 30)) == "restored"
    return location


# INV-07 #1: the removal changes the stored timeline and that day's totals only


@pytest.mark.django_db(transaction=True)
def test_INV07_1_removing_the_first_outage_changes_only_that_day_total(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _two_outages(location_factory)
    now = _at(16, 0)
    assert _total(_today_row(location, now)) == ("1h 30m", " · 2")
    live = _live(location)
    rows = OutboxMessage.objects.count()

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    assert _total(_today_row(location, now)) == ("30m", " · 1")
    # 09:00-10:00 is on now; the 15:00 outage's off piece is unchanged.
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("on", _at(9, 0), _at(10, 0), None),
        ("on", _at(10, 0), _at(15, 0), None),
        ("off", _at(15, 0), _at(15, 30), _at(15, 0)),
        ("on", _at(15, 30), None, None),
    ]
    # Live detection is untouched, nothing is queued and nothing is sent.
    assert _live(location) == live
    assert OutboxMessage.objects.count() == rows
    assert len(fake_telegram.calls) == 0


# D-01: the list


@pytest.mark.django_db(transaction=True)
def test_recent_outages_lists_both_outages_newest_first(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)

    recent = history.recent_outages(location.pk, _at(16, 0), KYIV)

    assert recent == history.RecentOutages(
        outages=(
            history.Outage(
                start=_at(15, 0), end=_at(15, 30), off_us=_us(minutes=30), in_progress=False
            ),
            history.Outage(start=_at(9, 0), end=_at(10, 0), off_us=_us(hours=1), in_progress=False),
        ),
        has_history=True,
    )
