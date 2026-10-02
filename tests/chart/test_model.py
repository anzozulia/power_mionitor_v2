"""The pure week model: local days, wall-clock positions, real-time totals and row mapping.

Scenarios: INV-03 #2 and #3 (partition, no data before the first heartbeat), INV-04 (not
monitored is never off), INV-08 #1-#3 (DST days, cross-midnight, now), INV-11 (an outage
split by not-monitored time counts once), K-5 (previous-week rows) and the interval-level
checks of docs/chart-spec.md sections 9 and 10.

Inputs are aware UTC instants, as stored. Expected wall positions are written as
multiples of ``HOUR_US`` and were computed by hand for Europe/Kyiv: UTC+3 (EEST) until
2026-10-25 01:00 UTC, UTC+2 (EET) until 2027-03-28 01:00 UTC, then UTC+3 again; Kyiv
also jumped from EET to EEST on 2026-03-29 at 01:00 UTC. The model is pure, so these
tests need no database.
"""

import ast
import pathlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfoNotFoundError

import pytest
from chart_fixtures import (
    DST_FALL_TODAY,
    DST_SPRING_TODAY,
    KYIV,
    SAMPLE_NOW,
    SAMPLE_TODAY,
    assert_partition,
    dst_fall_week,
    dst_spring_week,
    kyiv,
    local_pieces,
    sample_pieces,
)

from powermon.chart import model
from powermon.chart.model import DAY_US, HOUR_US, Piece, Segment
from powermon.i18n.duration import format_total_duration

H = HOUR_US
M = 60_000_000
NAIVE = datetime(2026, 10, 1, 12, 0)  # noqa: DTZ001
CHART_DIR = pathlib.Path(__file__).resolve().parents[2] / "powermon" / "chart"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _day(pieces: list[Piece], d: date, now: datetime) -> model.Row:
    return model.build_row(pieces, d, KYIV, now, dimmed=False, is_today=False)


def _drawn(row: model.Row, state: str) -> list[tuple[int, int]]:
    return [(s.start_us, s.end_us) for s in row.segments if s.state == state]


def _total(row: model.Row) -> str:
    return format_total_duration(row.off_us, "en")


# INV-08: DST days, cross-midnight outages and now (CHRT-06)


def test_INV08_1_spring_2026_03_29() -> None:
    # 2026-03-29 has 23 h: 00:00 EET is 2026-03-28 22:00 UTC, 03:00 EET (01:00 UTC) jumps
    # to 04:00 EEST and the next midnight is 21:00 UTC. 10:00-12:00 EEST is 07:00-09:00 UTC.
    pieces = [
        Piece("on", _utc("2026-03-28 22:00"), _utc("2026-03-29 07:00"), None),
        Piece("off", _utc("2026-03-29 07:00"), _utc("2026-03-29 09:00"), _utc("2026-03-29 07:00")),
        Piece("on", _utc("2026-03-29 09:00"), _utc("2026-03-29 21:00"), None),
    ]

    row = _day(pieces, date(2026, 3, 29), _utc("2026-03-30 09:00"))

    # v1 drew this outage at 09:00-11:00.
    assert _drawn(row, "off") == [(10 * H, 12 * H)]
    assert (row.off_us, row.count, _total(row)) == (2 * H, 1, "2h")
    assert _drawn(row, "on") == [(0, 3 * H), (4 * H, 10 * H), (12 * H, DAY_US)]
    assert_partition(row, day_length_us=23 * H, no_data_us=0)


def test_INV08_2_fall_2026_10_25() -> None:
    # 2026-10-25 has 25 h (21:00 UTC on 24.10 to 22:00 UTC). 08:00-10:00 UTC is 10:00-12:00
    # EET and 21:30-21:50 UTC is 23:30-23:50 EET. v1 drew 11:00-13:00 and lost 23:30.
    row = _day(dst_fall_week(repeated_hour=False), DST_FALL_TODAY, _utc("2026-10-26 09:00"))

    assert _drawn(row, "off") == [(10 * H, 12 * H), (23 * H + 30 * M, 23 * H + 50 * M)]
    assert (row.off_us, row.count, _total(row)) == (2 * H + 20 * M, 2, "2h 20m")
    # The first 03:00-04:00 (00:00-01:00 UTC, fold 0) is not drawn, so the on and off
    # segments tile wall 00:00-24:00 exactly once, while the totals keep all 25 h.
    assert _drawn(row, "on") == [
        (0, 3 * H),
        (3 * H, 10 * H),
        (12 * H, 23 * H + 30 * M),
        (23 * H + 50 * M, DAY_US),
    ]
    spans = [(s.start_us, s.end_us) for s in row.segments]
    assert spans[0][0] == 0 and spans[-1][1] == DAY_US
    assert all(left[1] == right[0] for left, right in pairwise(spans))
    assert row.on_us + row.off_us == 25 * H


def test_INV08_2_fall_repeated_hour_counts_both_occurrences() -> None:
    # 00:30 UTC is 03:30 EEST (the first 03:30) and 01:30 UTC is 03:30 EET (the second).
    pieces = [
        Piece("on", _utc("2026-10-24 21:00"), _utc("2026-10-25 00:30"), None),
        Piece("off", _utc("2026-10-25 00:30"), _utc("2026-10-25 01:30"), _utc("2026-10-25 00:30")),
        Piece("on", _utc("2026-10-25 01:30"), _utc("2026-10-25 22:00"), None),
    ]

    row = _day(pieces, DST_FALL_TODAY, _utc("2026-10-26 09:00"))

    assert (row.off_us, row.count, _total(row)) == (H, 1, "1h")
    # Only the later occurrence is drawn: 01:00-01:30 UTC is 03:00-03:30 EET.
    assert _drawn(row, "off") == [(3 * H, 3 * H + 30 * M)]
    assert_partition(row, day_length_us=25 * H, no_data_us=0)

    # The DST fixture week has all three outages on that Sunday.
    full = _day(dst_fall_week(), DST_FALL_TODAY, _utc("2026-10-26 09:00"))
    assert _drawn(full, "off") == [
        (3 * H, 3 * H + 30 * M),
        (10 * H, 12 * H),
        (23 * H + 30 * M, 23 * H + 50 * M),
    ]
    assert (full.off_us, full.count) == (3 * H + 20 * M, 3)


def test_dst_2027_03_28_spring_gap_is_no_data() -> None:
    # 01:00 UTC is 03:00 EET = 04:00 EEST: the wall hour 03:00-04:00 does not exist.
    row = _day(dst_spring_week(), DST_SPRING_TODAY, _utc("2027-03-29 09:00"))

    assert _drawn(row, "on") == [(0, 3 * H), (4 * H, 10 * H), (12 * H, DAY_US)]
    assert _drawn(row, "off") == [(10 * H, 12 * H)]
    assert not any(s.start_us < 4 * H and s.end_us > 3 * H for s in row.segments)
    assert (row.off_us, row.count) == (2 * H, 1)
    assert row.on_us + row.off_us == 23 * H


def test_INV08_3_cross_midnight_and_now() -> None:
    later = kyiv("2026-09-29 12:00")
    closed = local_pieces(
        [
            ("on", "2026-09-28 00:00", "2026-09-28 22:30"),
            ("off", "2026-09-28 22:30", "2026-09-29 01:15"),
            ("on", "2026-09-29 01:15", "2026-09-29 12:00"),
        ]
    )

    monday = _day(closed, date(2026, 9, 28), later)
    tuesday = _day(closed, date(2026, 9, 29), later)

    assert _drawn(monday, "off") == [(22 * H + 30 * M, DAY_US)]
    assert (_total(monday), monday.count) == ("1h 30m", 1)
    assert _drawn(tuesday, "off") == [(0, H + 15 * M)]
    assert (_total(tuesday), tuesday.count) == ("1h 15m", 1)

    # At Tue 00:10 (2026-09-28 21:10 UTC) the outage is still open: it is drawn to now.
    still_open = local_pieces(
        [("on", "2026-09-28 00:00", "2026-09-28 22:30"), ("off", "2026-09-28 22:30", None)]
    )
    now = kyiv("2026-09-29 00:10")
    today = model.build_row(still_open, date(2026, 9, 29), KYIV, now, dimmed=False, is_today=True)

    assert today.segments == (Segment("off", 0, 10 * M),)
    assert (today.off_us, today.count, _total(today)) == (10 * M, 1, "10m")
    assert all(s.end_us <= 10 * M for s in today.segments)
    assert _drawn(_day(still_open, date(2026, 9, 28), now), "off") == [(22 * H + 30 * M, DAY_US)]


# INV-03: the timeline partitions every day; no data is never counted (CHRT-01, CHRT-03)


@pytest.mark.parametrize(
    ("pieces", "day", "hours"),
    [
        (dst_fall_week, DST_FALL_TODAY, 25),
        (dst_spring_week, DST_SPRING_TODAY, 23),
        (sample_pieces, date(2026, 10, 1), 24),
        # Wednesday of the sample week has not-monitored time (server downtime 03:10-03:52).
        (sample_pieces, date(2026, 9, 30), 24),
    ],
    ids=["2026-10-25", "2027-03-28", "2026-10-01", "2026-09-30-not-monitored"],
)
def test_INV03_2_partition_23_24_25_hours(
    pieces: Callable[[], list[Piece]], day: date, hours: int
) -> None:
    start, end = model.day_bounds(day, KYIV)

    # A finished day: now is its next local midnight, so the open piece covers the rest.
    row = _day(pieces(), day, end)

    assert (end - start) // model.ONE_US == hours * H
    assert row.monitored
    assert_partition(row, day_length_us=hours * H, no_data_us=0)


def test_INV03_3_no_data_before_first_heartbeat() -> None:
    pieces = local_pieces([("on", "2026-09-29 08:00", None)])

    week = model.build_week(pieces, today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)
    monday, tuesday = week.rows[0], week.rows[1]

    assert monday.segments == ()
    assert (monday.monitored, monday.on_us, monday.off_us, monday.nm_us, monday.count) == (
        False,
        0,
        0,
        0,
        0,
    )
    assert tuesday.segments == (Segment("on", 8 * H, DAY_US),)
    assert (tuesday.on_us, tuesday.off_us, tuesday.count) == (16 * H, 0, 0)
    assert_partition(tuesday, day_length_us=24 * H, no_data_us=8 * H)
    # Last week's rows lie before the first heartbeat too.
    assert all(row.segments == () and not row.monitored for row in week.rows[4:])


# INV-04 and INV-11: not-monitored time is never off time and never an outage


def test_INV04_not_monitored_is_never_off() -> None:
    pieces = local_pieces(
        [
            ("on", "2026-10-01 00:00", "2026-10-01 10:00"),
            ("not_monitored", "2026-10-01 10:00", "2026-10-01 12:00"),
            ("on", "2026-10-01 12:00", "2026-10-02 00:00"),
        ]
    )

    row = _day(pieces, date(2026, 10, 1), kyiv("2026-10-02 09:00"))

    assert _drawn(row, "not_monitored") == [(10 * H, 12 * H)]
    assert _drawn(row, "off") == []
    assert (row.off_us, row.count, row.nm_us, row.monitored) == (0, 0, 2 * H, True)
    assert_partition(row, day_length_us=24 * H, no_data_us=0)


def test_only_not_monitored_day_is_not_monitored() -> None:
    whole = local_pieces([("not_monitored", "2026-10-01 00:00", "2026-10-02 00:00")])

    row = _day(whole, date(2026, 10, 1), kyiv("2026-10-02 09:00"))

    assert row.segments == (Segment("not_monitored", 0, DAY_US),)
    assert (row.nm_us, row.on_us, row.off_us, row.count, row.monitored) == (
        DAY_US,
        0,
        0,
        0,
        False,
    )

    # No data until 08:00, then not monitored: still only "—", never monitored.
    late = local_pieces([("not_monitored", "2026-10-01 08:00", "2026-10-02 00:00")])
    row = _day(late, date(2026, 10, 1), kyiv("2026-10-02 09:00"))

    assert row.segments == (Segment("not_monitored", 8 * H, DAY_US),)
    assert (row.nm_us, row.monitored) == (16 * H, False)
    assert_partition(row, day_length_us=24 * H, no_data_us=8 * H)


def test_INV11_row_total_split_outage_counts_once() -> None:
    # Off since 09:00, the stack was down 10:00-10:10, power back at 11:00: one outage
    # whose off time excludes the not-monitored span.
    outage = kyiv("2026-10-01 09:00")
    pieces = [
        Piece("on", kyiv("2026-10-01 00:00"), outage, None),
        Piece("off", outage, kyiv("2026-10-01 10:00"), outage),
        Piece("not_monitored", kyiv("2026-10-01 10:00"), kyiv("2026-10-01 10:10"), None),
        Piece("off", kyiv("2026-10-01 10:10"), kyiv("2026-10-01 11:00"), outage),
        Piece("on", kyiv("2026-10-01 11:00"), kyiv("2026-10-02 00:00"), None),
    ]

    row = _day(pieces, date(2026, 10, 1), kyiv("2026-10-02 09:00"))

    assert (_total(row), row.count) == ("1h 50m", 1)
    assert row.nm_us == 10 * M
    assert _drawn(row, "off") == [(9 * H, 10 * H), (10 * H + 10 * M, 11 * H)]


def test_adjacent_outages_with_different_starts_count_twice() -> None:
    pieces = local_pieces(
        [
            ("on", "2026-10-01 00:00", "2026-10-01 09:00"),
            ("off", "2026-10-01 09:00", "2026-10-01 10:00"),
            ("off", "2026-10-01 10:00", "2026-10-01 10:30"),
            ("on", "2026-10-01 10:30", "2026-10-02 00:00"),
        ]
    )
    later = kyiv("2026-10-02 09:00")

    row = _day(pieces, date(2026, 10, 1), later)

    # Touching pieces stay separate segments and stay two outages.
    assert row.count == 2
    assert _drawn(row, "off") == [(9 * H, 10 * H), (10 * H, 10 * H + 30 * M)]

    # The same two pieces of one outage (one outage start) count once.
    one_outage = [
        replace(p, outage_start=kyiv("2026-10-01 09:00")) if p.state == "off" else p for p in pieces
    ]
    assert _day(one_outage, date(2026, 10, 1), later).count == 1


def test_row_does_not_depend_on_piece_order() -> None:
    pieces = sample_pieces()

    forward = model.build_week(pieces, today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)
    backward = model.build_week(
        list(reversed(pieces)), today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True
    )

    assert backward == forward
    for row in forward.rows:
        assert [(s.start_us, s.end_us) for s in row.segments] == sorted(
            (s.start_us, s.end_us) for s in row.segments
        )


# Midnight and now edges (CHRT-01, CHRT-02)


def test_interval_ending_at_next_midnight_reaches_24h() -> None:
    pieces = local_pieces(
        [
            ("on", "2026-09-28 00:00", "2026-09-28 22:30"),
            ("off", "2026-09-28 22:30", "2026-09-29 00:00"),
            ("on", "2026-09-29 00:00", "2026-09-29 12:00"),
        ]
    )
    later = kyiv("2026-09-29 12:00")

    monday = _day(pieces, date(2026, 9, 28), later)
    tuesday = _day(pieces, date(2026, 9, 29), later)

    # v1 mapped the next midnight to 0.0 and lost this segment.
    assert monday.segments[-1] == Segment("off", 22 * H + 30 * M, DAY_US)
    assert (monday.off_us, monday.count) == (90 * M, 1)
    assert tuesday.segments == (Segment("on", 0, 12 * H),)
    assert (tuesday.off_us, tuesday.count) == (0, 0)


def test_now_at_local_midnight_leaves_today_empty() -> None:
    midnight = kyiv("2026-10-01 00:00")

    week = model.build_week(sample_pieces(), today=SAMPLE_TODAY, now=midnight, tz=KYIV, live=True)
    today = week.today_row

    assert today.is_today and today.segments == ()
    assert (today.monitored, today.on_us, today.off_us, today.nm_us, today.count) == (
        False,
        0,
        0,
        0,
        0,
    )
    # Yesterday is drawn in full, up to 24:00.
    assert week.rows[2].segments[-1].end_us == DAY_US

    # One minute later the open piece (on since 30.09 19:35) is drawn exactly up to now.
    minute = model.build_row(
        sample_pieces(), SAMPLE_TODAY, KYIV, kyiv("2026-10-01 00:01"), dimmed=False, is_today=True
    )
    assert minute.segments == (Segment("on", 0, M),)


# Row mapping (CHRT-07, K-5)


def _mapping(week: model.Week) -> list[tuple[date, bool, bool]]:
    return [(row.day, row.dimmed, row.is_today) for row in week.rows]


def test_K5_wednesday_shows_previous_thu_to_sun_dimmed() -> None:
    week = model.build_week(
        sample_pieces(), today=date(2026, 9, 30), now=kyiv("2026-09-30 12:00"), tz=KYIV, live=True
    )

    assert _mapping(week) == [
        (date(2026, 9, 28), False, False),
        (date(2026, 9, 29), False, False),
        (date(2026, 9, 30), False, True),
        (date(2026, 9, 24), True, False),
        (date(2026, 9, 25), True, False),
        (date(2026, 9, 26), True, False),
        (date(2026, 9, 27), True, False),
    ]
    # Thu 24.09 lies before monitoring started (Fri 25.09 10:42): no data, not monitored.
    assert week.rows[3].segments == () and not week.rows[3].monitored
    # Fri 25.09 shows last week's real data.
    assert (week.rows[4].count, week.rows[4].monitored) == (1, True)


def _week_of(today: date) -> model.Week:
    return model.build_week([], today=today, now=kyiv(f"{today} 12:00"), tz=KYIV, live=True)


def test_row_mapping_thu_mon_sun() -> None:
    thursday = _week_of(date(2026, 10, 1))
    assert _mapping(thursday)[3:] == [
        (date(2026, 10, 1), False, True),
        (date(2026, 9, 25), True, False),
        (date(2026, 9, 26), True, False),
        (date(2026, 9, 27), True, False),
    ]

    monday = _week_of(date(2026, 9, 28))
    assert _mapping(monday)[0] == (date(2026, 9, 28), False, True)
    assert [row.day for row in monday.rows if row.dimmed] == [
        date(2026, 9, d) for d in range(22, 28)
    ]

    sunday = _week_of(date(2026, 10, 4))
    assert not any(row.dimmed for row in sunday.rows)
    assert sunday.rows[-1] == sunday.today_row
    assert (sunday.rows[-1].day, sunday.rows[-1].is_today) == (date(2026, 10, 4), True)
    assert (sunday.monday, sunday.sunday) == (date(2026, 9, 28), date(2026, 10, 4))


# The building blocks: expected, edge and failure cases


def test_local_today_in_kyiv() -> None:
    assert model.local_today(SAMPLE_NOW, KYIV) == SAMPLE_TODAY
    # 2026-09-30 21:00 UTC is already 01.10 00:00 in Kyiv, one microsecond earlier is not.
    assert model.local_today(_utc("2026-09-30 21:00"), KYIV) == date(2026, 10, 1)
    assert model.local_today(_utc("2026-09-30 20:59:59.999999"), KYIV) == date(2026, 9, 30)
    with pytest.raises(ValueError, match="naive"):
        model.local_today(NAIVE, KYIV)


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (date(2026, 10, 1), "2026-09-30 21:00", "2026-10-01 21:00"),  # 24 h (EEST)
        (DST_FALL_TODAY, "2026-10-24 21:00", "2026-10-25 22:00"),  # 25 h
        (DST_SPRING_TODAY, "2027-03-27 22:00", "2027-03-28 21:00"),  # 23 h
    ],
)
def test_day_bounds_have_the_real_day_length(day: date, start: str, end: str) -> None:
    assert model.day_bounds(day, KYIV) == (_utc(start), _utc(end))
    assert model.next_midnight(day, KYIV) == _utc(end)


def test_transitions_on_the_dst_days() -> None:
    fall = _utc("2026-10-25 01:00")
    spring = _utc("2027-03-28 01:00")

    assert model.transitions(*model.day_bounds(DST_FALL_TODAY, KYIV), KYIV) == [
        (fall, timedelta(hours=3), timedelta(hours=2))
    ]
    assert model.transitions(*model.day_bounds(DST_SPRING_TODAY, KYIV), KYIV) == [
        (spring, timedelta(hours=2), timedelta(hours=3))
    ]
    # An ordinary day and a zone without DST have none.
    assert model.transitions(*model.day_bounds(SAMPLE_TODAY, KYIV), KYIV) == []
    assert model.transitions(*model.day_bounds(DST_FALL_TODAY, KYIV), "UTC") == []
    # The window is (a, b]: a change exactly at a is outside it, one exactly at b inside.
    hour = timedelta(hours=1)
    assert model.transitions(fall, fall + hour, KYIV) == []
    assert model.transitions(fall - hour, fall, KYIV) == [
        (fall, timedelta(hours=3), timedelta(hours=2))
    ]
    # A window that starts off the hour scans steps that straddle the change; the
    # bisection still finds the exact second.
    assert model.transitions(_utc("2026-10-25 00:20:07"), _utc("2026-10-25 02:00"), KYIV) == [
        (fall, timedelta(hours=3), timedelta(hours=2))
    ]
    with pytest.raises(ValueError, match="naive"):
        model.transitions(NAIVE, fall, KYIV)


def test_wall_us_maps_an_end_with_the_offset_before_it() -> None:
    start, end = model.day_bounds(SAMPLE_TODAY, KYIV)
    jump = _utc("2027-03-28 01:00")  # 03:00 EET becomes 04:00 EEST
    plain = kyiv("2026-10-01 14:37")

    assert model.wall_us(start, KYIV, end=False) == 0
    # The next local midnight ends the day at 24:00, not 0 (the v1 bug); it starts the next.
    assert model.wall_us(end, KYIV, end=True) == DAY_US
    assert model.wall_us(end, KYIV, end=False) == 0
    assert model.wall_us(jump, KYIV, end=True) == 3 * H
    assert model.wall_us(jump, KYIV, end=False) == 4 * H
    assert model.wall_us(plain, KYIV, end=True) == model.wall_us(plain, KYIV, end=False)
    assert model.wall_us(plain, KYIV, end=False) == 14 * H + 37 * M
    with pytest.raises(ValueError, match="naive"):
        model.wall_us(NAIVE, KYIV, end=False)


def test_week_window_spans_the_seven_shown_days() -> None:
    start = kyiv("2026-09-25 00:00")

    assert model.week_window(SAMPLE_TODAY, SAMPLE_NOW, KYIV) == (start, SAMPLE_NOW)
    # A finished day reads up to its next local midnight and never past it.
    assert model.week_window(SAMPLE_TODAY, kyiv("2026-10-02 09:00"), KYIV) == (
        start,
        kyiv("2026-10-02 00:00"),
    )
    # A now before the window gives an empty window, never an inverted one.
    assert model.week_window(SAMPLE_TODAY, kyiv("2026-09-20 00:00"), KYIV) == (start, start)
    with pytest.raises(ValueError, match="naive"):
        model.week_window(SAMPLE_TODAY, NAIVE, KYIV)


def test_model_rejects_bad_input() -> None:
    at = kyiv("2026-10-01 09:00")
    bad_pieces = [
        ([Piece("on", NAIVE, None, None)], "naive"),
        ([Piece("on", at, NAIVE, None)], "naive"),
        ([Piece("off", at, None, NAIVE)], "naive"),
        ([Piece("unknown", at, None, None)], "unknown interval state"),
        ([Piece("on", at, at, None)], "end after it starts"),
        ([Piece("on", at, at - model.ONE_US, None)], "end after it starts"),
        ([Piece("off", at, None, None)], "outage start"),
    ]

    with pytest.raises(ValueError, match="naive"):
        model.build_week(sample_pieces(), today=SAMPLE_TODAY, now=NAIVE, tz=KYIV, live=True)
    with pytest.raises(ValueError, match="naive"):
        model.build_row([], SAMPLE_TODAY, KYIV, NAIVE, dimmed=False, is_today=True)
    for pieces, message in bad_pieces:
        with pytest.raises(ValueError, match=message):
            model.build_row(pieces, SAMPLE_TODAY, KYIV, SAMPLE_NOW, dimmed=False, is_today=True)
        with pytest.raises(ValueError, match=message):
            model.build_week(pieces, today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)
    with pytest.raises(ZoneInfoNotFoundError):
        model.build_week(
            sample_pieces(), today=SAMPLE_TODAY, now=SAMPLE_NOW, tz="Europe/Kiev-typo", live=True
        )


# Purity guard


def _imported(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def test_model_source_is_pure() -> None:
    # model.py and the package marker: no Django, no Pillow, no clock, no true division.
    offenders: list[str] = []
    imports: list[str] = []
    for name in ("model.py", "__init__.py"):
        tree = ast.parse((CHART_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            where = f"{name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div):
                offenders.append(f"{where} divides with /")
            modules = _imported(node)
            imports += modules
            offenders += [
                f"{where} imports {m}" for m in modules if m.startswith(("django", "PIL"))
            ]
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("now", "today", "utcnow")
            ):
                offenders.append(f"{where} reads the clock with .{node.func.attr}()")

    assert offenders == []
    # The scan saw the real module: its offsets come from zoneinfo.
    assert "zoneinfo" in imports
