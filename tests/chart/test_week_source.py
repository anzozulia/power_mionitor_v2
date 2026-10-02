"""The chart reads the stored timeline: one overlap query, then the pure model (KD1, INV-03).

The chart-spec §10 sample week (tests/chart/chart_fixtures.py) is stored in
``power_interval`` and read back through ``source.load_week``. The seven rows must carry
the spec's expected totals: Mon ``7h 35m · 2``, Tue ``6h 16m · 2``, Wed ``5h 5m · 3``,
Thu (today) ``4h 10m · 2``, then last week's Fri 25.09 ``3h 55m · 1``, Sat 26.09
``7h 40m · 2`` and Sun 27.09 ``no outages``, dimmed. The query is scoped to one location
and never reads anything but ``power_interval``.
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
    sample_pieces,
    set_status,
)

from powermon.chart import model, source
from powermon.i18n.duration import format_total_duration

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
