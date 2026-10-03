"""History corrections in the engine (DATA-02, DATA-03; D-01, D-02, D-04, D-05, D-06, D-07,
UI5-D9; INV-07 #1, #2, #3).

``history.recent_outages`` reads the stored timeline as the chart does (KD1): one outage per
``outage_start_at`` (D-01, the chart's count rule), newest first, over the last 14 local
days plus the current outage. ``history.remove_outage`` is one transaction that takes the
location's ``location_state`` row lock first:

- an outage in progress (stored status off with that outage start, also while its open
  piece is not monitored) is refused under the lock (INV-07 #3);
- each off piece of the outage becomes on, one piece at a time, so not-monitored time inside
  it stays not monitored (D-02);
- the outage's queued alerts are dropped only when its OFF alert never went out (D-04 as
  refined on 2026-10-03): an OFF that is sent or uncertain keeps its ON; while the OFF is
  sending the removal is deferred with nothing written (wave-1 audit amendment, W1-A1);
- live state (status, last heartbeat, on since, outage start, window start, state version)
  is never written, nothing is queued and nothing is sent (INV-07 #1).

``history.reset_history`` is one transaction under the same row lock: refused while the
locked status is off, maintenance or not (D-06); nothing written without history (UI5-D9);
otherwise the whole timeline is deleted, the location waits for its first heartbeat with
``state_version`` bumped (D-05), and its active chart records are marked for the worker's
release (D-08). Configuration, switches, open incidents and queued alerts stay (D-05,
D-07). The next heartbeat restarts silently, and a later silence is alerted once (INV-07
#2).

Histories are built only through ``transitions.record_heartbeat``, ``detection.run_cycle``
and ``maintenance.set_maintenance``, with aware UTC times on the fixed day 2026-10-01. Tests
that run ``detection.run_cycle`` or race actors are ``django_db(transaction=True)``: the
cycle calls ``close_old_connections()``, which would close the connection inside
pytest-django's per-test transaction, and actors must see each other's commits.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import (
    DEFAULT_BOT_TOKEN,
    Actor,
    FakeTelegram,
    blocked_on_lock,
    terminate_backends,
    wait_for,
)
from django.db import connection, transaction

from powermon.alerts import delivery, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import source
from powermon.chart.models import ChartMessage
from powermon.engine import history, maintenance, restore, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.i18n import chart_texts
from powermon.locations.models import Location
from powermon.worker import detection, io_loop

Interval = tuple[str, datetime, datetime | None, datetime | None]

TODAY = date(2026, 10, 1)
KYIV = "Europe/Kyiv"


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _day(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """An aware UTC instant on day ``day`` of October 2026 (the window tests span days)."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


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


def _row_of(location: Any, kind: str, event_at: datetime) -> OutboxMessage:
    """The location's one subscriber outbox row of ``kind`` dated ``event_at``."""
    return OutboxMessage.objects.get(location=location, kind=kind, event_at=event_at)


def _status(row: OutboxMessage) -> tuple[str, str]:
    row.refresh_from_db()
    return row.status, row.last_error


def _off_since_9(location_factory: Callable[..., Any]) -> Any:
    """On since 08:00, last heartbeat 09:00, OFF recorded by the cycle at 09:01:31."""
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    state = LocationState.objects.get(location=location)
    assert (state.status, state.outage_started_at) == ("off", _at(9, 0))
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


def test_group_outages_by_outage_start_newest_first() -> None:
    pieces = [
        (_at(15, 0), _at(15, 30), _at(15, 0)),
        # The older outage is split by not-monitored time 10:00-10:10: still one outage.
        (_at(10, 10), _at(11, 0), _at(9, 0)),
        (_at(9, 0), _at(10, 0), _at(9, 0)),
    ]

    outages = history.group_outages(pieces, now=_at(16, 0), current=None)

    assert outages == [
        history.Outage(
            start=_at(15, 0), end=_at(15, 30), off_us=_us(minutes=30), in_progress=False
        ),
        history.Outage(
            start=_at(9, 0), end=_at(11, 0), off_us=_us(hours=1, minutes=50), in_progress=False
        ),
    ]


def test_group_outages_open_piece_counts_up_to_now_and_is_in_progress() -> None:
    pieces = [(_at(9, 0), _at(10, 0), _at(9, 0)), (_at(10, 10), None, _at(9, 0))]

    [outage] = history.group_outages(pieces, now=_at(11, 0), current=_at(9, 0))

    assert outage == history.Outage(
        start=_at(9, 0), end=None, off_us=_us(hours=1, minutes=50), in_progress=True
    )
    # The current outage while its open piece is not monitored: every off piece is closed,
    # and it is still in progress, with no end.
    [paused] = history.group_outages(pieces[:1], now=_at(11, 0), current=_at(9, 0))
    assert paused == history.Outage(
        start=_at(9, 0), end=None, off_us=_us(hours=1), in_progress=True
    )
    # An open piece that starts after now (a clock step back) never counts negative time.
    [early] = history.group_outages([(_at(12, 0), None, _at(12, 0))], now=_at(11, 0), current=None)
    assert (early.off_us, early.in_progress, early.end) == (0, True, None)
    assert history.group_outages([], now=_at(11, 0), current=None) == []


def test_group_outages_rejects_a_naive_now() -> None:
    naive = datetime(2026, 10, 1, 11, 0)  # noqa: DTZ001

    with pytest.raises(ValueError, match="naive"):
        history.group_outages([(_at(9, 0), _at(10, 0), _at(9, 0))], now=naive, current=None)


def test_window_start_rejects_a_naive_now() -> None:
    naive = datetime(2026, 10, 1, 11, 0)  # noqa: DTZ001

    with pytest.raises(ValueError, match="naive"):
        history.window_start(naive, KYIV)


@pytest.mark.parametrize(
    ("now", "first_day", "expected"),
    [
        # The fall-back Sunday (a 25 h day): the window starts 2026-10-12 00:00 EEST.
        (_day(25, 12, 0), date(2026, 10, 12), _day(11, 21, 0)),
        # The spring-forward Sunday (a 23 h day): it starts 2027-03-15 00:00 EET.
        (
            datetime(2027, 3, 28, 12, 0, tzinfo=UTC),
            date(2027, 3, 15),
            datetime(2027, 3, 14, 22, 0, tzinfo=UTC),
        ),
    ],
    ids=["2026-10-25", "2027-03-28"],
)
def test_D01_window_starts_at_local_midnight_on_dst_days(
    now: datetime, first_day: date, expected: datetime
) -> None:
    start = history.window_start(now, KYIV)

    assert start == expected
    local = start.astimezone(ZoneInfo(KYIV))
    assert (local.date(), local.hour, local.minute) == (first_day, 0, 0)
    # RESEARCH Pitfall 6: never now - 14 x 24 h.
    assert start != now - timedelta(days=history.WINDOW_DAYS)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # Today is in EET, the window's first day in EEST: 13 local days are 13 x 24 h + 1 h.
        (datetime(2026, 11, 2, 10, 0, tzinfo=UTC), _day(19, 21, 0)),
        # Today is in EEST, the first day in EET: 13 local days are 13 x 24 h - 1 h.
        (datetime(2027, 4, 5, 10, 0, tzinfo=UTC), datetime(2027, 3, 22, 22, 0, tzinfo=UTC)),
    ],
    ids=["across-fall-back", "across-spring-forward"],
)
def test_D01_window_across_a_dst_change_keeps_local_midnight(
    now: datetime, expected: datetime
) -> None:
    start = history.window_start(now, KYIV)

    assert start == expected
    today = now.astimezone(ZoneInfo(KYIV)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Each bound is converted on its own: today's midnight minus 13 x 24 h is an hour off.
    assert abs(today.astimezone(UTC) - timedelta(days=13) - start) == timedelta(hours=1)


@pytest.mark.django_db(transaction=True)
def test_D01_current_outage_in_maintenance_is_in_progress(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True

    recent = history.recent_outages(location.pk, _at(11, 0), KYIV)

    # Its open piece is not monitored now: the off time stops at 10:00, it has no end.
    assert recent == history.RecentOutages(
        outages=(history.Outage(start=_at(9, 0), end=None, off_us=_us(hours=1), in_progress=True),),
        has_history=True,
    )


@pytest.mark.django_db(transaction=True)
def test_D01_outage_that_started_before_the_window_is_listed_with_its_real_start(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)
    # Another outage from 2026-10-06 18:00 to 2026-10-07 02:00 UTC.
    assert transitions.record_heartbeat(location.pk, _day(6, 18)) == "plain"
    assert detection.run_cycle(_day(6, 18, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _day(7, 2)) == "restored"
    now = _day(20, 12)
    # The window starts at 2026-10-07 00:00 in Kyiv (EEST), 2026-10-06 21:00 UTC.
    assert history.window_start(now, KYIV) == _day(6, 21)

    recent = history.recent_outages(location.pk, now, KYIV)

    # Listed with its real start and its full off time; the 2026-10-01 outages ended before
    # the window and are not listed, while the location keeps its history.
    assert recent == history.RecentOutages(
        outages=(
            history.Outage(
                start=_day(6, 18), end=_day(7, 2), off_us=_us(hours=8), in_progress=False
            ),
        ),
        has_history=True,
    )
    assert history.recent_outages(location.pk, _day(30, 12), KYIV) == history.RecentOutages(
        outages=(), has_history=True
    )


@pytest.mark.django_db(transaction=True)
def test_D01_current_outage_is_listed_whatever_its_age(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)
    # Maintenance since 10:00 on 2026-10-01: every off piece ended long before the window.
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True

    recent = history.recent_outages(location.pk, _day(30, 12), KYIV)

    assert recent.outages == (
        history.Outage(start=_at(9, 0), end=None, off_us=_us(hours=1), in_progress=True),
    )


@pytest.mark.django_db
def test_no_history_and_no_outages(location_factory: Callable[..., Any]) -> None:
    waiting = location_factory(name="Waiting")
    only_on = location_factory(name="Only on")
    assert transitions.record_heartbeat(only_on.pk, _at(8, 0)) == "started"
    stateless = location_factory(name="No state row")
    LocationState.objects.filter(location=stateless).delete()

    empty = history.RecentOutages(outages=(), has_history=False)
    assert history.recent_outages(waiting.pk, _at(9, 0), KYIV) == empty
    assert history.recent_outages(stateless.pk, _at(9, 0), KYIV) == empty
    assert history.recent_outages(only_on.pk, _at(9, 0), KYIV) == history.RecentOutages(
        outages=(), has_history=True
    )
    with pytest.raises(ValueError, match="naive"):
        history.recent_outages(waiting.pk, datetime(2026, 10, 1, 9, 0), KYIV)  # noqa: DTZ001


@pytest.mark.django_db(transaction=True)
def test_find_outage_reads_one_outage(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)

    assert history.find_outage(location.pk, _at(9, 0), _at(16, 0)) == history.Outage(
        start=_at(9, 0), end=_at(10, 0), off_us=_us(hours=1), in_progress=False
    )
    # No outage starts at 09:30, and none of another location.
    assert history.find_outage(location.pk, _at(9, 30), _at(16, 0)) is None
    assert history.find_outage(location.pk + 1000, _at(9, 0), _at(16, 0)) is None
    with pytest.raises(ValueError, match="naive"):
        history.find_outage(location.pk, _at(9, 0), datetime(2026, 10, 1, 16, 0))  # noqa: DTZ001

    # The current outage reads as in progress, from the stored status.
    current = _off_since_9(location_factory)
    assert history.find_outage(current.pk, _at(9, 0), _at(9, 30)) == history.Outage(
        start=_at(9, 0), end=None, off_us=_us(minutes=30), in_progress=True
    )


# INV-07 #3: an outage in progress is refused under the lock


@pytest.mark.django_db(transaction=True)
def test_INV07_3_outage_in_progress_is_refused(location_factory: Callable[..., Any]) -> None:
    location = _off_since_9(location_factory)
    before = (_intervals(location), _outbox(), _live(location))

    assert history.remove_outage(location.pk, _at(9, 0)) == "in_progress"

    assert (_intervals(location), _outbox(), _live(location)) == before

    # Also while maintenance keeps its open piece not monitored: still the current outage.
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True
    assert _intervals(location)[-1] == ("not_monitored", _at(10, 0), None, None)
    before = (_intervals(location), _outbox(), _live(location))

    assert history.remove_outage(location.pk, _at(9, 0)) == "in_progress"

    assert (_intervals(location), _outbox(), _live(location)) == before


@pytest.mark.django_db(transaction=True)
def test_open_off_piece_is_refused_even_without_the_status(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)
    # Defensive: a state row that no longer names the outage (a writer that skipped the
    # gate) still never gets its open off piece removed.
    LocationState.objects.filter(location=location).update(outage_started_at=_at(8, 0))
    before = _intervals(location)

    assert history.remove_outage(location.pk, _at(9, 0)) == "in_progress"

    assert _intervals(location) == before


# D-02: only off time becomes on


@pytest.mark.django_db(transaction=True)
def test_D02_not_monitored_inside_the_removed_outage_stays(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True
    assert maintenance.set_maintenance(location.pk, False, _at(10, 10)) is True
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("off", _at(10, 10), _at(11, 0), _at(9, 0)),
        ("on", _at(11, 0), None, None),
    ]

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("on", _at(9, 0), _at(10, 0), None),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("on", _at(10, 10), _at(11, 0), None),
        ("on", _at(11, 0), None, None),
    ]
    row = _today_row(location, _at(11, 30))
    assert (row.off_us, row.count, row.nm_us) == (0, 0, _us(minutes=10))
    assert _total(row) == ("no outages", "")


# D-04 as refined: queued alerts are dropped only when the OFF alert never went out


@pytest.mark.django_db(transaction=True)
def test_D04_pending_alerts_of_the_removed_outage_are_dropped(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    assert _outbox() == [
        ("power_off", _at(9, 0), "dropped", history.OUTAGE_REMOVED),
        ("power_on", _at(10, 0), "dropped", history.OUTAGE_REMOVED),
        # The other outage's alerts are untouched.
        ("power_off", _at(15, 0), "pending", ""),
        ("power_on", _at(15, 30), "pending", ""),
    ]


@pytest.mark.django_db(transaction=True)
def test_D04_on_alert_matched_by_payload_when_power_returned_during_maintenance(
    location_factory: Callable[..., Any],
) -> None:
    location = _off_since_9(location_factory)
    assert maintenance.set_maintenance(location.pk, True, _at(10, 0)) is True
    assert transitions.record_heartbeat(location.pk, _at(10, 5)) == "restored"
    assert maintenance.set_maintenance(location.pk, False, _at(10, 10)) is True
    on = _row_of(location, "power_on", _at(10, 5))
    assert on.payload == {"was_off_us": _us(hours=1, minutes=5)}
    # No timeline boundary lies at the restore: the outage's last off piece ends at 10:00.
    assert _intervals(location) == [
        ("on", _at(8, 0), _at(9, 0), None),
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("on", _at(10, 10), None, None),
    ]

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    assert _status(_row_of(location, "power_off", _at(9, 0))) == (
        "dropped",
        history.OUTAGE_REMOVED,
    )
    assert _status(on) == ("dropped", history.OUTAGE_REMOVED)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("off_status", ["sent", "uncertain"])
def test_D04_on_alert_kept_when_the_off_alert_went_out(
    location_factory: Callable[..., Any], off_status: str
) -> None:
    location = _two_outages(location_factory)
    off = _row_of(location, "power_off", _at(9, 0))
    OutboxMessage.objects.filter(pk=off.pk).update(status=off_status)
    on = _row_of(location, "power_on", _at(10, 0))

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    # Subscribers may have seen "power off": its ON alert still goes out.
    assert _status(on) == ("pending", "")
    assert _status(off) == (off_status, "")
    assert _intervals(location)[1] == ("on", _at(9, 0), _at(10, 0), None)


# D-04 wave-1 audit amendment (W1-A1): "sending" is not final. A failed attempt (not sent,
# 429, 4xx, 5xx) puts the OFF back to pending, so a removal during an attempt is deferred
# with nothing written; the next removal keeps the ON (sent) or drops both (failed).


@pytest.mark.django_db(transaction=True)
def test_D04_W1_A1_removal_deferred_while_the_off_alert_is_sending(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)
    off = _row_of(location, "power_off", _at(9, 0))
    on = _row_of(location, "power_on", _at(10, 0))
    # The relay claimed the outage's OFF alert: an attempt is in flight.
    assert outbox.claim(off.pk) is True
    before = (_intervals(location), _outbox(), _live(location))

    assert history.remove_outage(location.pk, _at(9, 0)) == "sending"

    # Nothing written: the timeline, both alerts and the live state are unchanged.
    assert (_intervals(location), _outbox(), _live(location)) == before
    assert _status(off) == ("sending", "")
    assert _status(on) == ("pending", "")


@pytest.mark.django_db(transaction=True)
def test_D04_W1_A1_failed_attempt_then_removal_drops_both(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)
    off = _row_of(location, "power_off", _at(9, 0))
    on = _row_of(location, "power_on", _at(10, 0))
    assert outbox.claim(off.pk) is True
    assert history.remove_outage(location.pk, _at(9, 0)) == "sending"
    # The attempt fails (e.g. a 502): the relay puts the OFF back to pending.
    assert outbox.mark_retry(off.pk, _at(16, 1), "http_502") is True
    assert _status(off) == ("pending", "http_502")

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    # The OFF never went out: both alerts are dropped, so the outage is never announced.
    assert _status(off) == ("dropped", history.OUTAGE_REMOVED)
    assert _status(on) == ("dropped", history.OUTAGE_REMOVED)
    assert _intervals(location)[1] == ("on", _at(9, 0), _at(10, 0), None)


@pytest.mark.django_db(transaction=True)
def test_D04_W1_A1_another_outage_sending_does_not_defer(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)
    other = _row_of(location, "power_off", _at(15, 0))
    assert outbox.claim(other.pk) is True

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    # Only this outage's OFF rows decide; the other outage's attempt is untouched.
    assert _status(_row_of(location, "power_off", _at(9, 0))) == (
        "dropped",
        history.OUTAGE_REMOVED,
    )
    assert _status(other) == ("sending", "")


def _alerts_off_at_the_off(location_factory: Callable[..., Any]) -> Any:
    """Outage 09:00-10:00 recorded with alerts off (no OFF row), restored with alerts on."""
    _no_anchors()
    location = location_factory(alerts_enabled=False)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    Location.objects.filter(pk=location.pk).update(alerts_enabled=True)
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    assert [row[0] for row in _outbox()] == ["power_on"]
    return location


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", ["expired", "no_off_row"])
def test_D04_on_alert_dropped_when_the_off_alert_expired_or_never_existed(
    location_factory: Callable[..., Any], case: str
) -> None:
    if case == "expired":
        location = _two_outages(location_factory)
        off = _row_of(location, "power_off", _at(9, 0))
        OutboxMessage.objects.filter(pk=off.pk).update(status="expired", last_error="expired")
    else:
        location = _alerts_off_at_the_off(location_factory)
    on = _row_of(location, "power_on", _at(10, 0))

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    # The subscribers never saw this outage's OFF alert: its ON alert is never sent.
    assert _status(on) == ("dropped", history.OUTAGE_REMOVED)
    if case == "expired":
        assert _status(off) == ("expired", "expired")


@pytest.mark.django_db(transaction=True)
def test_D04_on_alert_with_a_foreign_payload_is_kept(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    on = _row_of(location, "power_on", _at(10, 0))
    # A row whose payload does not name this outage's duration is never matched.
    OutboxMessage.objects.filter(pk=on.pk).update(payload={"was_off_us": True})

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    assert _status(on) == ("pending", "")
    assert _status(_row_of(location, "power_off", _at(9, 0))) == (
        "dropped",
        history.OUTAGE_REMOVED,
    )


@pytest.mark.django_db(transaction=True)
def test_D04_nothing_to_drop_when_no_alert_was_queued(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    _no_anchors()
    location = location_factory(alerts_enabled=False)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    # Alerts were off for the whole outage: nothing was queued, nothing is dropped.
    assert _outbox() == []
    caplog.set_level(logging.INFO, logger=history.__name__)

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    assert _outbox() == []
    assert _intervals(location)[1] == ("on", _at(9, 0), _at(10, 0), None)
    [record] = [r for r in caplog.records if r.name == history.__name__]
    assert record.getMessage().endswith("removed, 0 queued alert(s) dropped")


@pytest.mark.django_db(transaction=True)
def test_removal_queues_nothing_and_logs_one_info_line(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    location = _two_outages(location_factory)
    rows = OutboxMessage.objects.count()
    caplog.set_level(logging.INFO, logger=history.__name__)

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    [record] = [r for r in caplog.records if r.name == history.__name__]
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert message == (
        f"outage {_at(9, 0).isoformat()} of location {location.pk} removed, "
        "2 queued alert(s) dropped"
    )
    assert DEFAULT_BOT_TOKEN not in message
    assert location.device_key not in message
    # No subscriber message and no ops notice.
    assert OutboxMessage.objects.count() == rows


# Idempotency and failure


@pytest.mark.django_db(transaction=True)
def test_removal_twice_is_gone_and_writes_nothing(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"
    before = (_intervals(location), _outbox(), _live(location))

    assert history.remove_outage(location.pk, _at(9, 0)) == "gone"

    assert (_intervals(location), _outbox(), _live(location)) == before


@pytest.mark.django_db(transaction=True)
def test_removal_for_a_deleted_location_is_gone(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    Location.objects.filter(pk=location.pk).update(deleted_at=_at(16, 0))
    before = (_intervals(location), _outbox(), _live(location))

    assert history.remove_outage(location.pk, _at(9, 0)) == "gone"

    assert (_intervals(location), _outbox(), _live(location)) == before
    # An unknown location (no state row) is gone too, and a naive start is refused.
    assert history.remove_outage(location.pk + 1000, _at(9, 0)) == "gone"
    with pytest.raises(ValueError, match="naive"):
        history.remove_outage(location.pk, datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001
    assert _intervals(location) == before[0]


# DATA-01 adjacency: unmerged on pieces read as one span


@pytest.mark.django_db(transaction=True)
def test_adjacent_on_pieces_after_removal_are_one_span(
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory)

    assert history.remove_outage(location.pk, _at(9, 0)) == "removed"

    row = _today_row(location, _at(16, 0))
    on = [(s.start_us, s.end_us) for s in row.segments if s.state == "on"]
    # 08:00-15:00 UTC is 11:00-18:00 in Kyiv: three stored pieces that abut, no gap.
    assert on[:3] == [
        (_us(hours=11), _us(hours=12)),
        (_us(hours=12), _us(hours=13)),
        (_us(hours=13), _us(hours=18)),
    ]
    assert all(a[1] == b[0] for a, b in zip(on[:2], on[1:3], strict=True))
    assert row.on_us == _us(hours=7, minutes=30)
    assert row.count == 1


# Concurrency: the removal serializes with every writer on the row lock


@pytest.mark.django_db(transaction=True)
def test_removal_waits_on_the_row_lock(
    location_factory: Callable[..., Any],
    lock_holder: tuple[threading.Event, threading.Event],
) -> None:
    location = _two_outages(location_factory)
    inside, release = lock_holder

    def hold_the_row_lock() -> None:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(transitions.LOCK_SQL, [location.pk])
            cur.fetchone()
            inside.set()
            if not release.wait(5):
                raise AssertionError("the lock holder was never released")

    holder = Actor(hold_the_row_lock)
    remover = Actor(lambda: history.remove_outage(location.pk, _at(9, 0)))
    try:
        holder.start()
        assert inside.wait(5)
        remover.start()
        assert wait_for(lambda: remover.pid is not None and blocked_on_lock(remover.pid))
        # Still waiting on the state row lock: nothing is written yet.
        assert remover.is_alive()
        assert _intervals(location)[1] == ("off", _at(9, 0), _at(10, 0), _at(9, 0))
        release.set()
        remover.join(5)
    finally:
        _finish(holder, remover, release=release)

    assert holder.exc is None, holder.exc
    assert remover.exc is None, remover.exc
    assert remover.result == "removed"
    assert _intervals(location)[1] == ("on", _at(9, 0), _at(10, 0), None)


@pytest.mark.django_db(transaction=True)
def test_removal_rereads_the_status_under_the_lock(
    location_factory: Callable[..., Any],
    lock_holder: tuple[threading.Event, threading.Event],
) -> None:
    location = _two_outages(location_factory)
    inside, release = lock_holder

    def new_outage_at_the_same_start() -> None:
        # A writer holding the lock commits a status that names the 15:00 outage as the
        # current one (status off, outage_started_at 15:00) while the removal waits.
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(transitions.LOCK_SQL, [location.pk])
            cur.fetchone()
            LocationState.objects.filter(location=location).update(
                status="off", outage_started_at=_at(15, 0)
            )
            inside.set()
            if not release.wait(5):
                raise AssertionError("the lock holder was never released")

    holder = Actor(new_outage_at_the_same_start)
    remover = Actor(lambda: history.remove_outage(location.pk, _at(15, 0)))
    try:
        holder.start()
        assert inside.wait(5)
        remover.start()
        assert wait_for(lambda: remover.pid is not None and blocked_on_lock(remover.pid))
        release.set()
        remover.join(5)
    finally:
        _finish(holder, remover, release=release)

    assert holder.exc is None, holder.exc
    assert remover.exc is None, remover.exc
    # The removal saw the committed status, not the one before it waited: refused.
    assert remover.result == "in_progress"
    assert ("off", _at(15, 0), _at(15, 30), _at(15, 0)) in _intervals(location)


# The history reset (DATA-03; D-05, D-06, D-07, UI5-D9; INV-07 #2)

YESTERDAY = date(2026, 9, 30)
Marks = list[tuple[int, datetime | None, datetime | None]]


def _chart(
    location: Any, day: date = TODAY, *, message_id: int, retired_at: datetime | None = None
) -> ChartMessage:
    """A chart record an earlier I/O pass left in the location's chat, by its bot."""
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=location.chat_id,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=message_id,
        pinned=retired_at is None,
        last_rendered_at=_at(8, 0),
        created_at=_at(8, 0),
        retired_at=retired_at,
    )


def _marks(location: Any) -> Marks:
    """The location's chart records as (message id, history_reset_at, retired_at)."""
    rows = ChartMessage.objects.filter(location=location).order_by("id")
    return list(rows.values_list("message_id", "history_reset_at", "retired_at"))


def _config(location: Any) -> tuple[Any, ...]:
    """Everything a reset keeps on the location row (D-05)."""
    stored = Location.objects.get(pk=location.pk)
    return (
        stored.name,
        stored.period_s,
        stored.grace_s,
        stored.chat_id,
        stored.bot_token,
        stored.language,
        stored.device_key,
        stored.maintenance,
        stored.alerts_enabled,
        stored.router_grace,
        stored.deleted_at,
    )


def _everything(location: Any) -> tuple[Any, ...]:
    """What a reset could write: timeline, live state, chart marks, outbox, configuration."""
    return (_intervals(location), _live(location), _marks(location), _outbox(), _config(location))


@pytest.mark.django_db(transaction=True)
def test_D05_reset_clears_history_and_waits(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    today = _chart(location, message_id=501)
    # An older record already retired (a release done long ago): never marked again.
    _chart(location, YESTERDAY, message_id=401, retired_at=_at(7, 0))
    assert delivery.open_failing(location.pk, _at(15, 45), 403) is True
    config = _config(location)
    version = _live(location)[5]
    now = _at(16, 0)

    assert history.reset_history(location.pk, now) == "reset"

    assert _intervals(location) == []
    assert _live(location) == ("waiting", None, None, None, None, version + 1)
    assert _marks(location) == [(today.message_id, now, None), (401, None, _at(7, 0))]
    # Configuration, device key and the three switches are kept, and so is the open
    # delivery_failing incident (delivery health, D-05).
    assert _config(location) == config
    incident = OpsIncident.objects.get(kind=delivery.KIND_DELIVERY_FAILING, location=location)
    assert incident.ended_at is None


@pytest.mark.django_db(transaction=True)
def test_D07_queued_alerts_are_kept(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    rows = _outbox()
    assert [row[2] for row in rows] == ["pending"] * 4

    assert history.reset_history(location.pk, _at(16, 0)) == "reset"

    # They report real events, and D-06 means every queued OFF already has its ON.
    assert _outbox() == rows


@pytest.mark.django_db(transaction=True)
def test_D06_reset_refused_while_off(location_factory: Callable[..., Any]) -> None:
    location = _off_since_9(location_factory)
    _chart(location, message_id=501)

    for maintenance_on in (False, True):
        if maintenance_on:
            # In maintenance the open piece is not monitored, but the status is still off.
            assert maintenance.set_maintenance(location.pk, True, _at(9, 30)) is True
            assert _intervals(location)[-1] == ("not_monitored", _at(9, 30), None, None)
        before = _everything(location)

        assert history.reset_history(location.pk, _at(9, 40)) == "in_progress"

        assert _everything(location) == before
    assert _live(location)[0] == "off"


@pytest.mark.django_db(transaction=True)
def test_UI5_D9_nothing_to_reset_writes_nothing(location_factory: Callable[..., Any]) -> None:
    # A location that never sent a heartbeat: no interval, nothing to reset, no version bump.
    waiting = location_factory()
    _chart(waiting, message_id=301)
    before = _everything(waiting)

    assert history.reset_history(waiting.pk, _at(9, 0)) == "nothing"

    assert _everything(waiting) == before
    # The second click of a double submit: the first reset wrote, the second writes nothing.
    location = _two_outages(location_factory)
    _chart(location, message_id=501)
    assert history.reset_history(location.pk, _at(16, 0)) == "reset"
    after = _everything(location)

    assert history.reset_history(location.pk, _at(16, 0, 5)) == "nothing"

    assert _everything(location) == after
    assert _marks(location) == [(501, _at(16, 0), None)]


@pytest.mark.django_db(transaction=True)
def test_reset_of_a_deleted_location_is_gone(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    _chart(location, message_id=501)
    Location.objects.filter(pk=location.pk).update(deleted_at=_at(16, 0))
    before = _everything(location)

    assert history.reset_history(location.pk, _at(16, 5)) == "gone"

    assert _everything(location) == before
    # An unknown location (no state row) is gone too.
    assert history.reset_history(location.pk + 1000, _at(16, 5)) == "gone"


@pytest.mark.django_db
def test_reset_rejects_a_naive_now(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    before = _everything(location)

    with pytest.raises(ValueError, match="naive"):
        history.reset_history(location.pk, datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001

    assert _everything(location) == before


@pytest.mark.django_db(transaction=True)
def test_INV07_2_reset_right_after_start_then_silence_gives_one_off(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(8, 0, 30)) == "plain"

    assert history.reset_history(location.pk, _at(8, 1)) == "reset"

    # The next heartbeat restarts monitoring silently (K-1, MON-01).
    assert transitions.record_heartbeat(location.pk, _at(8, 2)) == "started"
    assert not OutboxMessage.objects.exists()
    assert transitions.record_heartbeat(location.pk, _at(8, 3)) == "plain"
    assert _intervals(location) == [("on", _at(8, 2), None, None)]
    # A 1 h silence after that: exactly one OFF, from the last heartbeat (K-2).
    assert detection.run_cycle(_at(9, 3)) == 1
    assert detection.run_cycle(_at(9, 10)) == 0
    assert detection.run_cycle(_at(10, 0)) == 0
    assert _outbox() == [("power_off", _at(8, 3), "pending", "")]
    assert _intervals(location) == [
        ("on", _at(8, 2), _at(8, 3), None),
        ("off", _at(8, 3), None, _at(8, 3)),
    ]
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_snapshot_taken_before_the_reset_loses_its_off(
    monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    _no_anchors()
    location = location_factory()
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    real = transitions.read_snapshots

    def snapshot_then_reset() -> Any:
        snapshots = real()
        # The admin resets between the detector's snapshot and its decision.
        assert history.reset_history(location.pk, _at(10, 6, 30)) == "reset"
        return snapshots

    monkeypatch.setattr(transitions, "read_snapshots", snapshot_then_reset)

    # The snapshot's state_version is stale after the reset: its OFF CAS changes no row.
    assert detection.run_cycle(_at(10, 6, 31)) == 0

    assert _live(location)[:5] == ("waiting", None, None, None, None)
    assert _intervals(location) == []
    assert _outbox() == []


@pytest.mark.django_db(transaction=True)
def test_reset_waits_on_the_row_lock(
    location_factory: Callable[..., Any],
    lock_holder: tuple[threading.Event, threading.Event],
) -> None:
    location = _two_outages(location_factory)
    inside, release = lock_holder

    def hold_the_row_lock() -> None:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(transitions.LOCK_SQL, [location.pk])
            cur.fetchone()
            inside.set()
            if not release.wait(5):
                raise AssertionError("the lock holder was never released")

    holder = Actor(hold_the_row_lock)
    resetter = Actor(lambda: history.reset_history(location.pk, _at(16, 0)))
    try:
        holder.start()
        assert inside.wait(5)
        resetter.start()
        assert wait_for(lambda: resetter.pid is not None and blocked_on_lock(resetter.pid))
        # Still waiting on the state row lock: nothing is deleted yet.
        assert resetter.is_alive()
        assert len(_intervals(location)) == 5
        release.set()
        resetter.join(5)
    finally:
        _finish(holder, resetter, release=release)

    assert holder.exc is None, holder.exc
    assert resetter.exc is None, resetter.exc
    assert resetter.result == "reset"
    assert _intervals(location) == []
    # A heartbeat after the reset takes the FIRST gate.
    assert transitions.record_heartbeat(location.pk, _at(16, 1)) == "started"
    assert _intervals(location) == [("on", _at(16, 1), None, None)]


@pytest.mark.django_db(transaction=True)
def test_reset_rereads_the_status_under_the_lock(
    location_factory: Callable[..., Any],
    lock_holder: tuple[threading.Event, threading.Event],
) -> None:
    location = _two_outages(location_factory)
    inside, release = lock_holder

    def outage_starts_meanwhile() -> None:
        # A writer holding the lock commits status off while the reset waits.
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(transitions.LOCK_SQL, [location.pk])
            cur.fetchone()
            LocationState.objects.filter(location=location).update(
                status="off", outage_started_at=_at(15, 45)
            )
            inside.set()
            if not release.wait(5):
                raise AssertionError("the lock holder was never released")

    holder = Actor(outage_starts_meanwhile)
    resetter = Actor(lambda: history.reset_history(location.pk, _at(16, 0)))
    try:
        holder.start()
        assert inside.wait(5)
        resetter.start()
        assert wait_for(lambda: resetter.pid is not None and blocked_on_lock(resetter.pid))
        release.set()
        resetter.join(5)
    finally:
        _finish(holder, resetter, release=release)

    assert holder.exc is None, holder.exc
    assert resetter.exc is None, resetter.exc
    # The reset saw the committed status, not the one before it waited: refused.
    assert resetter.result == "in_progress"
    assert len(_intervals(location)) == 5


@pytest.mark.django_db(transaction=True)
def test_reset_of_a_waiting_location_with_history(location_factory: Callable[..., Any]) -> None:
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    # After a restore (05-03) the location waits but keeps its history: the open piece is
    # not monitored from the dump's last known moment.
    restore.restart_after_restore(_at(9, 0))
    assert _live(location)[0] == "waiting"
    assert _intervals(location) == [("not_monitored", _at(8, 0), None, None)]
    version = _live(location)[5]

    assert history.reset_history(location.pk, _at(9, 5)) == "reset"

    assert _intervals(location) == []
    assert _live(location) == ("waiting", None, None, None, None, version + 1)


@pytest.mark.django_db(transaction=True)
def test_reset_logs_one_info_line(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    location = _two_outages(location_factory)
    caplog.set_level(logging.INFO, logger=history.__name__)

    assert history.reset_history(location.pk, _at(16, 0)) == "reset"

    [record] = [r for r in caplog.records if r.name == history.__name__]
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert message == f"history of location {location.pk} reset at {_at(16, 0).isoformat()}"
    assert DEFAULT_BOT_TOKEN not in message
    assert DEFAULT_BOT_TOKEN.split(":", 1)[1] not in message
    assert location.device_key not in message
    # A reset that writes nothing logs nothing.
    caplog.clear()
    assert history.reset_history(location.pk, _at(16, 1)) == "nothing"
    assert [r for r in caplog.records if r.name == history.__name__] == []
