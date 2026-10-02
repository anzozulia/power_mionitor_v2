"""The chart reads the stored timeline: one overlap query, then the pure model (KD1, INV-03).

The chart-spec §10 sample week (tests/chart/chart_fixtures.py) is stored in
``power_interval`` and read back through ``source.load_week``. The seven rows must carry
the spec's expected totals: Mon ``7h 35m · 2``, Tue ``6h 16m · 2``, Wed ``5h 5m · 3``,
Thu (today) ``4h 10m · 2``, then last week's Fri 25.09 ``3h 55m · 1``, Sat 26.09
``7h 40m · 2`` and Sun 27.09 ``no outages``, dimmed. The query is scoped to one location
and never reads anything but ``power_interval``.

The real writers of ``power_interval`` then drive the chart through their public
functions (the engine modules are only read here):
- INV-03 #1: heartbeats until 10:00 and the next at 12:00 (local) give an ON alert whose
  "was OFF for" is ``2h``, and today's row shows exactly that: off 10:00-12:00, ``2h · 1``.
- INV-10 #1 (chart part): a downtime window recorded by the lapse carve reads back as not
  monitored, never as on and never as off.
Kyiv is UTC+3 on every date here.
"""

from datetime import date
from typing import Any

import pytest
from chart_fixtures import (
    KYIV,
    SAMPLE_NAMES,
    SAMPLE_NOW,
    SAMPLE_TODAY,
    insert_pieces,
    kyiv,
    local_pieces,
    monitor,
    sample_pieces,
    set_status,
)

from powermon.alerts.models import OutboxMessage
from powermon.chart import model, source
from powermon.chart.model import DAY_US, Segment
from powermon.engine import lapse, transitions
from powermon.engine.models import SystemState
from powermon.i18n.duration import format_alert_duration, format_total_duration
from powermon.worker import detection

pytestmark = pytest.mark.django_db

H = model.HOUR_US
M = 60_000_000

# (shown date, dimmed, today, off time in en or None when there is none, count, monitored)
EXPECTED_ROWS = [
    (date(2026, 9, 28), False, False, "7h 35m", 2, True),
    (date(2026, 9, 29), False, False, "6h 16m", 2, True),
    (date(2026, 9, 30), False, False, "5h 5m", 3, True),
    (date(2026, 10, 1), False, True, "4h 10m", 2, True),
    (date(2026, 9, 25), True, False, "3h 55m", 1, True),
    (date(2026, 9, 26), True, False, "7h 40m", 2, True),
    (date(2026, 9, 27), True, False, None, 0, True),
]

Summary = tuple[date, bool, bool, str | None, int, bool]


def _summary(week: model.Week) -> list[Summary]:
    return [
        (
            row.day,
            row.dimmed,
            row.is_today,
            format_total_duration(row.off_us, "en") if row.off_us else None,
            row.count,
            row.monitored,
        )
        for row in week.rows
    ]


def _load(location: Any) -> model.Week:
    return source.load_week(location.pk, today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)


@pytest.fixture
def sample_location(location_factory: Any) -> Any:
    """A location that is on since 13:02 today, with the sample week stored."""
    location = location_factory(name=SAMPLE_NAMES["en"])
    set_status(location, "on", at=kyiv("2026-10-01 13:02"))
    insert_pieces(location, sample_pieces())
    return location


def test_sample_week_from_the_stored_timeline(sample_location: Any) -> None:
    week = _load(sample_location)

    assert _summary(week) == EXPECTED_ROWS
    assert (week.monday, week.sunday, week.today, week.live) == (
        date(2026, 9, 28),
        date(2026, 10, 4),
        SAMPLE_TODAY,
        True,
    )
    assert week.today_row.day == SAMPLE_TODAY
    # Today's open piece (on since 13:02) is drawn up to now, 14:37 local, and no further.
    assert week.today_row.segments[-1] == model.Segment("on", 13 * H + 2 * M, 14 * H + 37 * M)
    # Wednesday's server downtime 03:10-03:52 is drawn, and never counts as off time.
    wednesday = week.rows[2]
    assert model.Segment("not_monitored", 3 * H + 10 * M, 3 * H + 52 * M) in wednesday.segments
    assert wednesday.nm_us == 42 * M


def test_sample_week_from_the_pure_model_is_identical(sample_location: Any) -> None:
    pure = model.build_week(sample_pieces(), today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)

    assert _summary(pure) == EXPECTED_ROWS
    assert pure.rows == _load(sample_location).rows


def test_week_query_is_scoped_to_one_location(sample_location: Any, location_factory: Any) -> None:
    other = location_factory(name="No intervals yet")

    empty = _load(other)

    assert len(empty.rows) == 7
    assert all(
        row.segments == () and not row.monitored and (row.off_us, row.count) == (0, 0)
        for row in empty.rows
    )
    assert _summary(_load(sample_location)) == EXPECTED_ROWS


def test_load_week_rejects_a_naive_now(sample_location: Any) -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        source.load_week(
            sample_location.pk,
            today=SAMPLE_TODAY,
            now=SAMPLE_NOW.replace(tzinfo=None),
            tz=KYIV,
            live=True,
        )


# The real writers of power_interval (engine, lapse carve) read back as the chart


def _today(location: Any, now: Any) -> model.Row:
    week = source.load_week(location.pk, today=SAMPLE_TODAY, now=now, tz=KYIV, live=True)
    return week.today_row


def test_INV10_1_lapse_carve_is_drawn_not_monitored(location_factory: Any) -> None:
    location = location_factory()
    monitor(location, kyiv("2026-10-01 08:00"))

    # The stack was down 10:00-10:10 local (07:00-07:10 UTC); the worker's carve records it.
    assert lapse.carve_window(kyiv("2026-10-01 10:00"), kyiv("2026-10-01 10:10")) == 1
    today = _today(location, kyiv("2026-10-01 10:30"))

    assert today.segments == (
        Segment("on", 8 * H, 10 * H),
        Segment("not_monitored", 10 * H, 10 * H + 10 * M),
        Segment("on", 10 * H + 10 * M, 10 * H + 30 * M),
    )
    assert (today.off_us, today.count, today.nm_us) == (0, 0, 10 * M)
    assert not any(
        s.state == "on" and s.start_us < 10 * H + 10 * M and s.end_us > 10 * H
        for s in today.segments
    )


@pytest.mark.django_db(transaction=True)
def test_INV03_1_off_span_and_total_match_the_on_alert(location_factory: Any) -> None:
    # Transactional: run_cycle calls close_old_connections() first, and the teardown
    # truncates system_state, so the test writes the whole singleton it needs.
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "detection_resumed_at": kyiv("2026-10-01 09:00"),
            "web_started_at": None,
            "last_cycle_completed_at": None,
        },
    )
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, kyiv("2026-10-01 09:00")) == "started"
    assert transitions.record_heartbeat(location.pk, kyiv("2026-10-01 10:00")) == "plain"
    # 90 s after the last heartbeat (period 60 s + grace 30 s) the outage is recorded,
    # starting at that heartbeat.
    assert detection.run_cycle(kyiv("2026-10-01 10:01:31")) == 1
    assert transitions.record_heartbeat(location.pk, kyiv("2026-10-01 12:00")) == "restored"

    [alert] = OutboxMessage.objects.filter(kind="power_on")
    was_off_us = alert.payload["was_off_us"]
    today = _today(location, kyiv("2026-10-01 12:05"))

    assert format_alert_duration(was_off_us, "en") == "2h"
    assert today.off_us == was_off_us
    assert (format_total_duration(today.off_us, "en"), today.count) == ("2h", 1)
    assert today.segments == (
        Segment("on", 9 * H, 10 * H),
        Segment("off", 10 * H, 12 * H),
        Segment("on", 12 * H, 12 * H + 5 * M),
    )


def test_week_query_includes_an_interval_that_started_before_the_week(
    location_factory: Any,
) -> None:
    location = location_factory()
    monitor(location, kyiv("2026-09-01 08:00"))

    week = source.load_week(location.pk, today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)

    assert all(row.monitored for row in week.rows)
    assert week.today_row.segments == (Segment("on", 0, 14 * H + 37 * M),)
    assert all(row.segments == (Segment("on", 0, DAY_US),) for row in week.rows if not row.is_today)


def test_read_pieces_returns_states_in_start_order_for_one_location(
    location_factory: Any,
) -> None:
    mine, other = location_factory(name="Mine"), location_factory(name="Other")
    pieces = local_pieces(
        [
            ("on", "2026-10-01 00:00", "2026-10-01 02:00"),
            ("off", "2026-10-01 02:00", "2026-10-01 03:00"),
            ("on", "2026-10-01 03:00", None),
        ]
    )
    # Stored newest first and interleaved in time with another location's pieces.
    insert_pieces(mine, pieces[::-1])
    insert_pieces(
        other,
        local_pieces(
            [("off", "2026-10-01 01:00", "2026-10-01 02:30"), ("on", "2026-10-01 02:30", None)]
        ),
    )

    read = source.read_pieces(mine.pk, kyiv("2026-10-01 00:00"), kyiv("2026-10-01 12:00"))

    assert read == pieces
    assert [p.state for p in read] == ["on", "off", "on"]


def test_read_pieces_with_an_empty_window_reads_nothing(sample_location: Any) -> None:
    # The open piece (on since 13:02) spans both bounds; an empty window overlaps nothing.
    later, earlier = kyiv("2026-10-01 14:00"), kyiv("2026-10-01 13:30")

    assert source.read_pieces(sample_location.pk, later, earlier) == []
    assert source.read_pieces(sample_location.pk, later, later) == []
    # The same location still has pieces in a real window.
    assert source.read_pieces(sample_location.pk, earlier, later) != []
