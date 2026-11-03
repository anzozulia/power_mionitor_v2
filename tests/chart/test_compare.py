"""The pure location comparison (quick task 261103-lfm, D-3, D-4, D-6, D-8, D-9).

Scenarios: INV-03 (day partition, 23/24/25 h), INV-04 (not monitored is never off and
never an outage), INV-08 (DST positions and real-time totals) and INV-11 (an outage split
by not-monitored time counts once), plus the plan's "Page test fixture" numbers.

Inputs are aware UTC instants. Kyiv is UTC+3 (EEST) until 2026-10-25 01:00 UTC, then
UTC+2 (EET); it went EET -> EEST on 2026-03-29 at 01:00 UTC. The module is pure, so these
tests need no database (no ``django_db`` mark).
"""

import ast
import pathlib
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest
from chart_fixtures import (
    DST_FALL_TODAY,
    KYIV,
    SAMPLE_NOW,
    SAMPLE_TODAY,
    dst_fall_week,
    sample_pieces,
)

from powermon.chart import compare, model
from powermon.chart.model import DAY_US, HOUR_US, Piece

H = HOUR_US
M = 60_000_000
NAIVE = datetime(2026, 10, 1, 12, 0)  # noqa: DTZ001
CHART_DIR = pathlib.Path(__file__).resolve().parents[2] / "powermon" / "chart"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def on(start: str, end: str | None) -> Piece:
    return Piece("on", _utc(start), None if end is None else _utc(end), None)


def nm(start: str, end: str | None) -> Piece:
    return Piece("not_monitored", _utc(start), None if end is None else _utc(end), None)


def off(start: str, end: str | None, outage: str | None = None) -> Piece:
    return Piece("off", _utc(start), None if end is None else _utc(end), _utc(outage or start))


def recs(pieces: Sequence[Piece], now: datetime) -> tuple[compare.OutageRec, ...]:
    """Whole outages of the off pieces, as ``history.group_outages`` would group them."""
    groups: dict[datetime, list[Piece]] = {}
    for p in pieces:
        if p.state == "off" and p.outage_start is not None:
            groups.setdefault(p.outage_start, []).append(p)
    out = []
    for start, ps in sorted(groups.items()):
        open_piece = any(p.end is None for p in ps)
        off_us = sum(((p.end or now) - p.start) // model.ONE_US for p in ps)
        end = None if open_piece else max(p.end for p in ps if p.end is not None)
        out.append(compare.OutageRec(start, end, off_us))
    return tuple(out)


def ser(key: int, name: str, pieces: Sequence[Piece], now: datetime) -> compare.Series:
    return compare.Series(key, name, tuple(pieces), recs(pieces, now))


def run(
    series: Sequence[compare.Series], first: date, last: date, now: datetime
) -> compare.Comparison:
    period = compare.period_of(first, last, now, KYIV)
    return compare.compare(series, period, now, KYIV)


# The plan's page fixture: now 2026-10-01 16:00 UTC (19:00 Kyiv), P = 25.09..01.10.

FIX_NOW = _utc("2026-10-01 16:00")
FIX_FIRST, FIX_LAST = date(2026, 9, 25), date(2026, 10, 1)


def fixture_series() -> list[compare.Series]:
    home = [
        on("2026-09-24 00:00", "2026-09-28 06:00"),
        off("2026-09-28 06:00", "2026-09-28 07:00"),
        nm("2026-09-28 07:00", "2026-09-28 07:30"),
        off("2026-09-28 07:30", "2026-09-28 08:00", "2026-09-28 06:00"),
        on("2026-09-28 08:00", "2026-10-01 09:00"),
        off("2026-10-01 09:00", "2026-10-01 10:00"),
        on("2026-10-01 10:00", None),
    ]
    office = [
        on("2026-09-24 00:00", "2026-10-01 09:00"),
        off("2026-10-01 09:00", "2026-10-01 10:00"),
        on("2026-10-01 10:00", None),
    ]
    shop = [
        on("2026-09-24 00:00", "2026-10-01 09:30"),
        off("2026-10-01 09:30", "2026-10-01 11:00"),
        on("2026-10-01 11:00", "2026-10-01 14:00"),
        off("2026-10-01 14:00", None),
    ]
    return [
        ser(1, "Home", home, FIX_NOW),
        ser(2, "Office", office, FIX_NOW),
        ser(3, "Shop", shop, FIX_NOW),
    ]


@pytest.fixture
def fix() -> compare.Comparison:
    return run(fixture_series(), FIX_FIRST, FIX_LAST, FIX_NOW)


# Purity


def _imported(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        return [base, *(f"{base}.{alias.name}" for alias in node.names)]
    return []


def test_compare_source_is_pure() -> None:
    tree = ast.parse((CHART_DIR / "compare.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        where = f"compare.py:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div):
            offenders.append(f"{where} divides with /")
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            offenders.append(f"{where} has a float constant")
        modules = _imported(node)
        imports += modules
        offenders += [f"{where} imports {m}" for m in modules if m.startswith(("django", "PIL"))]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("now", "today", "utcnow")
        ):
            offenders.append(f"{where} reads the clock with .{node.func.attr}()")

    assert offenders == []
    assert "powermon.chart.model" in imports


# Period, presets, windows, percent


def test_period_of_today_ends_at_now() -> None:
    p = compare.period_of(FIX_FIRST, FIX_LAST, FIX_NOW, KYIV)
    assert p.start == _utc("2026-09-24 21:00")
    assert p.end == FIX_NOW
    assert p.length_us == 163 * H
    assert p.days == tuple(FIX_FIRST + timedelta(days=i) for i in range(7))


def test_period_of_past_day_ends_at_next_midnight_across_fall_back() -> None:
    p = compare.period_of(date(2026, 10, 25), date(2026, 10, 25), FIX_NOW + timedelta(30), KYIV)
    assert (p.start, p.end) == (_utc("2026-10-24 21:00"), _utc("2026-10-25 22:00"))
    assert p.length_us == 25 * H


def test_period_of_before_its_start_is_empty() -> None:
    p = compare.period_of(date(2026, 10, 5), date(2026, 10, 6), FIX_NOW, KYIV)
    assert p.start == p.end
    assert p.length_us == 0


@pytest.mark.parametrize(
    ("first", "last", "now"),
    [
        (date(2026, 10, 2), date(2026, 10, 1), FIX_NOW),
        (date(2026, 9, 25), date(2026, 10, 1), NAIVE),
    ],
)
def test_period_of_rejects_bad_input(first: date, last: date, now: datetime) -> None:
    with pytest.raises(ValueError):
        compare.period_of(first, last, now, KYIV)


def test_preset_dates() -> None:
    today = date(2026, 10, 1)
    assert compare.preset_dates("7d", today) == (date(2026, 9, 25), today)
    assert compare.preset_dates("90d", today) == (today - timedelta(days=89), today)
    with pytest.raises(ValueError):
        compare.preset_dates("1y", today)


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def test_timeline_window_default_and_short_periods() -> None:
    last = date(2026, 10, 31)
    # Expected: a period of at most 31 days is shown whole, with no neighbour.
    assert compare.timeline_window(date(2026, 10, 1), last) == compare.TimelineWindow(
        date(2026, 10, 1), last, None, None
    )
    # Default window of 90 days: the last 31 days, earlier ends the day before.
    first90 = last - _days(89)
    assert compare.timeline_window(first90, last) == compare.TimelineWindow(
        last - _days(30), last, last - _days(31), None
    )
    # Edge: 40 days; the earlier window is clamped at the period's start later on.
    first40 = last - _days(39)
    w = compare.timeline_window(first40, last)
    assert (w.first, w.earlier) == (last - _days(30), last - _days(31))
    assert compare.timeline_window(first40, last, w.earlier) == compare.TimelineWindow(
        first40, last - _days(31), None, last
    )


def test_timeline_window_pages_back_twice_through_90_days() -> None:
    last = date(2026, 10, 31)
    first = last - _days(89)
    w1 = compare.timeline_window(first, last)
    assert w1.earlier is not None
    w2 = compare.timeline_window(first, last, w1.earlier)
    assert w2 == compare.TimelineWindow(last - _days(61), last - _days(31), last - _days(62), last)
    assert w2.earlier is not None
    w3 = compare.timeline_window(first, last, w2.earlier)
    # The first window: it starts at the period's start, so there is no earlier page.
    assert w3 == compare.TimelineWindow(first, last - _days(62), None, last - _days(31))
    # Later from the first window returns the second one.
    assert w3.later is not None
    assert compare.timeline_window(first, last, w3.later) == w2


def test_timeline_window_clamps_the_end_into_the_period() -> None:
    last = date(2026, 10, 31)
    first = last - _days(89)
    # An end after the period is the period's last day (the default window).
    assert compare.timeline_window(first, last, date(2027, 5, 1)) == compare.timeline_window(
        first, last
    )
    # An end before the period is its first day: a one-day window, no earlier page.
    assert compare.timeline_window(first, last, date(2025, 1, 1)) == compare.TimelineWindow(
        first, first, None, first + _days(31)
    )
    # Failure: a reversed period or an empty window.
    with pytest.raises(ValueError):
        compare.timeline_window(last, first)
    with pytest.raises(ValueError):
        compare.timeline_window(first, last, days=0)


@pytest.mark.parametrize(
    ("num", "den", "expected"),
    [(1, 8, 13), (1, 200, 1), (1, 201, 0), (0, 5, 0), (5, 5, 100), (3, 0, None)],
)
def test_percent_rounds_half_up(num: int, den: int, expected: int | None) -> None:
    assert compare.percent(num, den) == expected


# Totals (D-3)


def test_totals_clip_a_piece_crossing_both_ends_of_p() -> None:
    s = ser(1, "A", [on("2026-09-20 00:00", "2026-10-05 00:00")], FIX_NOW)
    c = run([s], date(2026, 9, 25), date(2026, 9, 27), FIX_NOW + timedelta(days=10))
    loc = c.locations[0]
    assert (loc.on_us, loc.off_us, loc.nm_us, loc.nodata_us) == (72 * H, 0, 0, 0)
    assert loc.off_share == 0 and loc.coverage == 100 and not loc.partial_data
    assert loc.off_per_day_us == 0 and loc.days_monitored == 3


def test_totals_count_an_open_piece_to_now_and_no_data() -> None:
    # On from 30.09 03:00 local (open), so 25.09 00:00..30.09 03:00 local is no data.
    s = ser(1, "A", [on("2026-09-30 00:00", None)], FIX_NOW)
    c = run([s], FIX_FIRST, FIX_LAST, FIX_NOW)
    loc = c.locations[0]
    assert loc.on_us == (FIX_NOW - _utc("2026-09-30 00:00")) // model.ONE_US
    assert loc.on_us + loc.off_us + loc.nm_us + loc.nodata_us == loc.period_us == 163 * H
    assert loc.partial_data
    assert loc.days_monitored == 2


def test_coverage_exactly_90_percent_is_not_partial() -> None:
    # 2026-09-25 local is 24 h: 21:00 UTC on 24.09 to 21:00 UTC on 25.09.
    full = ser(1, "A", [on("2026-09-24 21:00", "2026-09-25 18:36")], FIX_NOW)
    short = ser(2, "B", [on("2026-09-24 21:00", "2026-09-25 18:35")], FIX_NOW)
    c = run([full, short], FIX_FIRST, FIX_FIRST, FIX_NOW)
    assert c.locations[0].coverage == 90 and not c.locations[0].partial_data
    assert c.locations[1].partial_data


def test_no_period_gives_no_shares() -> None:
    c = run([ser(1, "A", [], FIX_NOW)], date(2026, 10, 5), date(2026, 10, 5), FIX_NOW)
    loc = c.locations[0]
    assert (loc.off_share, loc.coverage, loc.off_per_day_us) == (None, None, None)
    assert loc.avg_outage_us is None and loc.longest is None


def test_fixture_location_numbers(fix: compare.Comparison) -> None:
    home, office, shop = fix.locations
    assert (home.off_us, home.outage_count) == (150 * M, 2)
    assert (home.avg_outage_us, home.median_outage_us) == (75 * M, 75 * M)
    assert home.longest == compare.OutageRec(
        _utc("2026-09-28 06:00"), _utc("2026-09-28 08:00"), 90 * M
    )
    assert (home.days_with_outages, home.days_total) == (2, 7)
    assert (home.off_share, home.nm_us) == (2, 30 * M)
    assert (office.off_us, office.outage_count, office.off_share) == (60 * M, 1, 1)
    assert (shop.off_us, shop.outage_count, shop.off_share) == (210 * M, 2, 2)
    assert shop.longest == compare.OutageRec(_utc("2026-10-01 14:00"), None, 120 * M)
    assert home.off_per_day_us == 150 * M // 7


# Outages (INV-11, INV-04)


def test_INV11_outage_split_by_not_monitored_counts_once() -> None:
    pieces = [
        on("2026-09-25 00:00", "2026-09-25 06:00"),
        off("2026-09-25 06:00", "2026-09-25 07:00"),
        nm("2026-09-25 07:00", "2026-09-25 07:30"),
        off("2026-09-25 07:30", "2026-09-25 08:00", "2026-09-25 06:00"),
        on("2026-09-25 08:00", None),
    ]
    loc = run([ser(1, "A", pieces, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW).locations[0]
    assert loc.outage_count == 1
    assert (loc.off_us, loc.avg_outage_us, loc.median_outage_us) == (90 * M, 90 * M, 90 * M)


def test_INV11_adjacent_off_pieces_of_two_outages_count_twice() -> None:
    pieces = [
        off("2026-09-25 06:00", "2026-09-25 07:00"),
        off("2026-09-25 07:00", "2026-09-25 08:00"),
    ]
    loc = run([ser(1, "A", pieces, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW).locations[0]
    assert loc.outage_count == 2
    assert loc.days_with_outages == 1


def test_outage_across_p_start_uses_whole_off_time() -> None:
    # The outage started three days before P; only one hour of it is inside P.
    start = "2026-09-21 20:00"
    pieces = [
        off("2026-09-24 20:00", "2026-09-24 22:00", start),
        on("2026-09-24 22:00", None),
    ]
    whole = compare.OutageRec(_utc(start), _utc("2026-09-24 22:00"), 5 * H)
    s = compare.Series(1, "A", tuple(pieces), (whole,))
    loc = run([s], FIX_FIRST, FIX_LAST, FIX_NOW).locations[0]
    assert (loc.off_us, loc.outage_count) == (1 * H, 1)
    assert loc.avg_outage_us == 5 * H and loc.longest == whole


def test_outage_across_midnight_counts_once_and_on_both_days() -> None:
    # 23:30 local on 29.09 to 00:30 local on 30.09.
    pieces = [off("2026-09-29 20:30", "2026-09-29 21:30"), on("2026-09-29 21:30", None)]
    c = run([ser(1, "A", pieces, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    loc = c.locations[0]
    assert (loc.outage_count, loc.days_with_outages) == (1, 2)
    counts = {d.day: d.lanes[0].count for d in c.days}
    assert counts[date(2026, 9, 29)] == counts[date(2026, 9, 30)] == 1


def test_ongoing_outage_counts() -> None:
    pieces = [on("2026-09-25 00:00", "2026-10-01 15:00"), off("2026-10-01 15:00", None)]
    loc = run([ser(1, "A", pieces, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW).locations[0]
    assert loc.outage_count == 1 and loc.off_us == 1 * H
    assert loc.longest is not None and loc.longest.end is None


def test_avg_median_longest_odd_and_even() -> None:
    odd = [
        off("2026-09-25 01:00", "2026-09-25 02:00"),
        off("2026-09-25 04:00", "2026-09-25 07:00"),
        off("2026-09-25 09:00", "2026-09-25 09:30"),
    ]
    loc = run([ser(1, "A", odd, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW).locations[0]
    assert (loc.avg_outage_us, loc.median_outage_us) == (90 * M, 60 * M)
    assert loc.longest is not None and loc.longest.off_us == 3 * H
    # Even count; two longest outages tie: the earlier start wins.
    even = [
        off("2026-09-25 01:00", "2026-09-25 03:00"),
        off("2026-09-25 04:00", "2026-09-25 05:00"),
        off("2026-09-25 09:00", "2026-09-25 11:00"),
        off("2026-09-25 12:00", "2026-09-25 12:15"),
    ]
    loc = run([ser(1, "A", even, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW).locations[0]
    assert loc.median_outage_us == (60 * M + 120 * M) // 2
    assert loc.longest is not None and loc.longest.start == _utc("2026-09-25 01:00")


def test_missing_outage_record_is_a_value_error() -> None:
    s = compare.Series(1, "A", (off("2026-09-25 01:00", "2026-09-25 02:00"),), ())
    with pytest.raises(ValueError):
        run([s], FIX_FIRST, FIX_LAST, FIX_NOW)


def test_naive_now_is_a_value_error() -> None:
    period = compare.period_of(FIX_FIRST, FIX_LAST, FIX_NOW, KYIV)
    with pytest.raises(ValueError):
        compare.compare([], period, NAIVE, KYIV)


def test_INV04_not_monitored_is_never_off_nor_an_outage() -> None:
    a = ser(1, "A", [nm("2026-09-25 00:00", "2026-09-25 12:00")], FIX_NOW)
    b = ser(
        2,
        "B",
        [off("2026-09-25 00:00", "2026-09-25 06:00"), on("2026-09-25 06:00", "2026-09-25 12:00")],
        FIX_NOW,
    )
    c = run([a, b], FIX_FIRST, FIX_FIRST, FIX_NOW)
    loc = c.locations[0]
    assert (loc.off_us, loc.outage_count, loc.nm_us) == (0, 0, 12 * H)
    assert loc.days_monitored == 0 and loc.off_per_day_us is None
    states = [(sp.state, sp.joint) for sp in c.spans]
    # A not monitored is unknown: B off gives some_off, B on gives partial; never all_off.
    assert states == [("partial", False), ("some_off", False), ("partial", False)]
    assert c.together is not None and c.together.all_off_us == 0
    assert c.simultaneous == ()


# Together (D-4)


def test_together_fixture(fix: compare.Comparison) -> None:
    t = fix.together
    assert t is not None
    assert t.all_off_us == 30 * M
    assert t.jointly_monitored_us == 162 * H + 30 * M
    assert t.all_off_share == 0  # "<1%" in the page
    assert t.known_us == 163 * H and t.any_on_us == 163 * H - 30 * M
    assert t.any_on_share == 100


def test_together_states_and_stats() -> None:
    # P = 25.09 local = 24.09 21:00 .. 25.09 21:00 UTC.
    a = [
        on("2026-09-24 21:00", "2026-09-25 03:00"),
        off("2026-09-25 03:00", "2026-09-25 05:00"),
        on("2026-09-25 05:00", "2026-09-25 21:00"),
    ]
    b = [
        on("2026-09-24 21:00", "2026-09-25 02:30"),
        nm("2026-09-25 02:30", "2026-09-25 04:00"),
        off("2026-09-25 04:00", "2026-09-25 06:00"),
        on("2026-09-25 06:00", "2026-09-25 21:00"),
    ]
    c = run([ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW)], FIX_FIRST, FIX_FIRST, FIX_NOW)
    got = [(sp.state, sp.start, sp.end, sp.joint) for sp in c.spans]
    assert got == [
        ("all_on", _utc("2026-09-24 21:00"), _utc("2026-09-25 02:30"), True),
        ("partial", _utc("2026-09-25 02:30"), _utc("2026-09-25 03:00"), False),
        ("some_off", _utc("2026-09-25 03:00"), _utc("2026-09-25 04:00"), False),
        ("all_off", _utc("2026-09-25 04:00"), _utc("2026-09-25 05:00"), True),
        ("some_off", _utc("2026-09-25 05:00"), _utc("2026-09-25 06:00"), True),
        ("all_on", _utc("2026-09-25 06:00"), _utc("2026-09-25 21:00"), True),
    ]
    t = c.together
    assert t is not None
    assert (t.all_off_us, t.any_on_us, t.jointly_monitored_us, t.known_us) == (
        1 * H,
        22 * H,
        22 * H + 30 * M,
        24 * H,
    )
    assert (t.all_off_share, t.any_on_share) == (4, 92)


def test_one_series_has_no_together_and_no_pairs() -> None:
    c = run(fixture_series()[:1], FIX_FIRST, FIX_LAST, FIX_NOW)
    assert c.together is None and c.pairs == () and c.spans == ()
    assert c.simultaneous == () and c.simultaneous_total == 0
    assert all(d.together is None for d in c.days)
    assert [h.key for h in c.hours] == [1]
    assert all(w.together is None for w in c.weeks)


def test_no_series_gives_empty_sections() -> None:
    c = run([], FIX_FIRST, FIX_LAST, FIX_NOW)
    assert c.locations == () and c.hours == () and c.together is None
    assert len(c.days) == 7 and all(d.lanes == () for d in c.days)


# Pairs (D-4)


def test_pairs_fixture(fix: compare.Comparison) -> None:
    by = {(p.a, p.b): p for p in fix.pairs}
    assert list(by) == [(1, 2), (1, 3), (2, 3)]
    ho = by[1, 2]
    assert (ho.both_off_us, ho.share_a, ho.share_b) == (60 * M, 40, 100)
    assert (ho.coinciding_a, ho.outages_a, ho.coinciding_b, ho.outages_b) == (1, 2, 1, 1)
    assert (ho.start_gap_us, ho.end_gap_us) == (0, 0)
    hs = by[1, 3]
    assert (hs.both_off_us, hs.share_a, hs.share_b) == (30 * M, 20, 14)
    assert (hs.start_gap_us, hs.end_gap_us) == (30 * M, 1 * H)
    os_ = by[2, 3]
    assert (os_.both_off_us, os_.share_a, os_.share_b) == (30 * M, 50, 14)
    assert (os_.start_gap_us, os_.end_gap_us) == (30 * M, 1 * H)


def test_pair_end_gap_skips_an_ongoing_outage() -> None:
    a = [
        off("2026-10-01 05:00", "2026-10-01 06:00"),
        on("2026-10-01 06:00", "2026-10-01 13:30"),
        off("2026-10-01 13:30", "2026-10-01 15:00"),
        on("2026-10-01 15:00", None),
    ]
    b = [
        off("2026-10-01 05:10", "2026-10-01 05:40"),
        on("2026-10-01 05:40", "2026-10-01 14:00"),
        off("2026-10-01 14:00", None),
    ]
    c = run([ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    p = c.pairs[0]
    # Start gaps +10m and +30m (median 20m); only the ended pair has an end gap (-20m).
    assert (p.start_gap_us, p.end_gap_us) == (20 * M, -20 * M)
    assert (p.coinciding_a, p.coinciding_b) == (2, 2)


def test_pair_without_overlap_has_no_gaps_and_is_symmetric() -> None:
    a = [off("2026-09-25 01:00", "2026-09-25 02:00"), on("2026-09-25 02:00", None)]
    b = [on("2026-09-25 00:00", "2026-09-25 03:00"), off("2026-09-25 03:00", "2026-09-25 04:00")]
    c1 = run([ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    c2 = run([ser(2, "B", b, FIX_NOW), ser(1, "A", a, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    p = c1.pairs[0]
    assert (p.both_off_us, p.share_a, p.share_b) == (0, 0, 0)
    assert (p.start_gap_us, p.end_gap_us, p.coinciding_a, p.coinciding_b) == (None, None, 0, 0)
    assert c2.pairs[0].both_off_us == 0


def test_pair_share_of_a_location_without_off_time_is_none() -> None:
    a = [on("2026-09-25 00:00", None)]
    b = [off("2026-09-25 00:00", "2026-09-25 01:00"), on("2026-09-25 01:00", None)]
    c = run([ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    assert (c.pairs[0].share_a, c.pairs[0].share_b) == (None, 0)


# Simultaneous outages (D-4)


def test_simultaneous_fixture(fix: compare.Comparison) -> None:
    assert fix.simultaneous_total == 1
    (sim,) = fix.simultaneous
    assert (sim.start, sim.end) == (_utc("2026-10-01 09:00"), _utc("2026-10-01 10:00"))
    assert sim.duration_us == 1 * H
    assert (sim.keys, sim.peak, sim.all_n) == ((1, 2, 3), 3, True)


def test_simultaneous_reaching_now_is_ongoing() -> None:
    a = [on("2026-10-01 00:00", "2026-10-01 14:00"), off("2026-10-01 14:00", None)]
    b = [on("2026-10-01 00:00", "2026-10-01 15:00"), off("2026-10-01 15:00", None)]
    c3 = [on("2026-10-01 00:00", None)]
    c = run(
        [ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW), ser(3, "C", c3, FIX_NOW)],
        FIX_FIRST,
        FIX_LAST,
        FIX_NOW,
    )
    (sim,) = c.simultaneous
    assert (sim.start, sim.end, sim.duration_us) == (_utc("2026-10-01 15:00"), None, 1 * H)
    assert (sim.keys, sim.peak, sim.all_n) == ((1, 2), 2, False)


# WR-02: a simultaneous outage cut by the period's edges. Kyiv is UTC+2 from 25.10, so the
# local day 01.11 runs 31.10 22:00 .. 01.11 22:00 UTC; now is 03.11 10:00 UTC.
CUT_NOW = _utc("2026-11-03 10:00")
CUT_DAY = date(2026, 11, 1)


def _cut(a: list[Piece], b: list[Piece]) -> compare.SimOutage:
    c = run([ser(1, "A", a, CUT_NOW), ser(2, "B", b, CUT_NOW)], CUT_DAY, CUT_DAY, CUT_NOW)
    (sim,) = c.simultaneous
    return sim


def test_simultaneous_cut_by_both_ends_of_p() -> None:
    # The reviewer's probe: both off since 31.10 22:00 local (20:00 UTC), still off at now.
    a = [on("2026-10-30 00:00", "2026-10-31 20:00"), off("2026-10-31 20:00", None)]
    b = [on("2026-10-30 00:00", "2026-10-31 20:00"), off("2026-10-31 20:00", None)]
    sim = _cut(a, b)
    assert (sim.start, sim.end) == (_utc("2026-10-31 22:00"), _utc("2026-11-01 22:00"))
    assert sim.duration_us == 24 * H
    assert (sim.starts_before, sim.ends_after) == (True, True)


def test_simultaneous_exactly_at_the_edges_of_p_is_not_cut() -> None:
    # Edge: off pieces starting exactly at P's start and ending exactly at P's end.
    a = [
        on("2026-10-30 00:00", "2026-10-31 22:00"),
        off("2026-10-31 22:00", "2026-11-01 22:00"),
        on("2026-11-01 22:00", None),
    ]
    sim = _cut(a, list(a))
    assert sim.duration_us == 24 * H
    assert (sim.starts_before, sim.ends_after) == (False, False)


def test_simultaneous_needs_two_locations_beyond_the_edge() -> None:
    # Failure: only A was off before P and only A stays off after it; B is off exactly
    # inside P, so the span is not flagged at either end.
    a = [on("2026-10-30 00:00", "2026-10-31 20:00"), off("2026-10-31 20:00", None)]
    b = [
        on("2026-10-30 00:00", "2026-10-31 22:00"),
        off("2026-10-31 22:00", "2026-11-01 22:00"),
        on("2026-11-01 22:00", None),
    ]
    sim = _cut(a, b)
    assert (sim.starts_before, sim.ends_after) == (False, False)
    # A span inside P is never flagged either (the fixture), nor one ongoing at now.
    fixture = run(fixture_series(), FIX_FIRST, FIX_LAST, FIX_NOW)
    assert not any(s.starts_before or s.ends_after for s in fixture.simultaneous)


def test_simultaneous_ongoing_at_now_is_not_ends_after() -> None:
    # Edge: P ends at now (today), so the span is ongoing, not "after".
    now = _utc("2026-11-01 10:00")
    a = [on("2026-10-30 00:00", "2026-10-31 20:00"), off("2026-10-31 20:00", None)]
    c = run([ser(1, "A", a, now), ser(2, "B", a, now)], CUT_DAY, CUT_DAY, now)
    (sim,) = c.simultaneous
    assert (sim.end, sim.starts_before, sim.ends_after) == (None, True, False)


def test_simultaneous_capped_at_100_newest_first() -> None:
    base0 = _utc("2026-09-25 00:00")
    a: list[Piece] = []
    b: list[Piece] = []
    for i in range(105):
        t = base0 + timedelta(hours=i)
        m = timedelta(minutes=1)
        a += [Piece("off", t, t + 30 * m, t), Piece("on", t + 30 * m, t + 60 * m, None)]
        b += [
            Piece("on", t, t + 10 * m, None),
            Piece("off", t + 10 * m, t + 40 * m, t + 10 * m),
            Piece("on", t + 40 * m, t + 60 * m, None),
        ]
    now = _utc("2026-10-01 16:00")
    c = run([ser(1, "A", a, now), ser(2, "B", b, now)], FIX_FIRST, FIX_LAST, now)
    assert c.simultaneous_total == 105
    assert len(c.simultaneous) == compare.SIM_CAP == 100
    first = c.simultaneous[0]
    assert first.start == base0 + timedelta(hours=104, minutes=10)
    assert first.duration_us == 20 * M
    starts = [s.start for s in c.simultaneous]
    assert starts == sorted(starts, reverse=True)


# Day lanes (D-6, INV-03, INV-08)


def _assert_lanes_equal_build_row(
    pieces: list[Piece], first: date, last: date, now: datetime
) -> None:
    c = run([ser(1, "A", pieces, now)], first, last, now)
    today = model.local_today(now, KYIV)
    assert [d.day for d in c.days] == list(c.period.days)
    for d in c.days:
        expected = model.build_row(pieces, d.day, KYIV, now, dimmed=False, is_today=d.day == today)
        assert d.lanes[0] == expected, d.day
        assert d.is_today == (d.day == today)


def test_INV03_INV08_lanes_equal_build_row_sample_week() -> None:
    _assert_lanes_equal_build_row(sample_pieces(), date(2026, 9, 25), SAMPLE_TODAY, SAMPLE_NOW)


def test_INV08_lanes_equal_build_row_spring_2026_03_29() -> None:
    pieces = [
        Piece("on", _utc("2026-03-27 22:00"), _utc("2026-03-29 07:00"), None),
        Piece("off", _utc("2026-03-29 07:00"), _utc("2026-03-29 09:00"), _utc("2026-03-29 07:00")),
        Piece("on", _utc("2026-03-29 09:00"), None, None),
    ]
    now = _utc("2026-03-30 09:00")
    _assert_lanes_equal_build_row(pieces, date(2026, 3, 28), date(2026, 3, 30), now)
    c = run([ser(1, "A", pieces, now)], date(2026, 3, 29), date(2026, 3, 29), now)
    assert c.locations[0].on_us + c.locations[0].off_us == 23 * H


@pytest.mark.parametrize("repeated_hour", [True, False])
def test_INV08_lanes_equal_build_row_fall_2026_10_25(repeated_hour: bool) -> None:
    now = _utc("2026-10-26 09:00")
    pieces = dst_fall_week(repeated_hour=repeated_hour)
    _assert_lanes_equal_build_row(pieces, date(2026, 10, 19), date(2026, 10, 26), now)


def test_together_lane_on_fall_back_day() -> None:
    now = _utc("2026-10-26 09:00")
    a = ser(1, "A", dst_fall_week(repeated_hour=True), now)
    b = ser(2, "B", dst_fall_week(repeated_hour=False), now)
    c = run([a, b], DST_FALL_TODAY, DST_FALL_TODAY, now)
    (day,) = c.days
    lane = day.together
    assert lane is not None
    spans = [(s.start_us, s.end_us) for s in lane.segments]
    # The segments tile wall 00:00-24:00 once: the fold-0 03:00-04:00 is not drawn.
    assert spans[0][0] == 0 and spans[-1][1] == DAY_US
    assert all(left[1] == right[0] for left, right in pairwise(spans))
    drawn_off = [(s.start_us, s.end_us) for s in lane.segments if s.state == "all_off"]
    assert drawn_off == [(10 * H, 12 * H), (23 * H + 30 * M, 23 * H + 50 * M)]
    # Real time: 25 h jointly monitored and 2h 20m all off.
    assert (lane.all_off_us, lane.joint_us) == (2 * H + 20 * M, 25 * H)
    some = [(s.start_us, s.end_us) for s in lane.segments if s.state == "some_off"]
    # A is off 03:30 EEST -> 03:30 EET: only the drawn (fold 1) half hour shows.
    assert some == [(3 * H, 3 * H + 30 * M)]


def test_timeline_shows_the_last_31_days() -> None:
    now = _utc("2026-10-31 12:00")
    pieces = [on("2026-09-01 00:00", None)]
    c = run([ser(1, "A", pieces, now)], date(2026, 9, 22), date(2026, 10, 31), now)
    assert (c.window.first, c.window.last) == (date(2026, 10, 1), date(2026, 10, 31))
    assert (c.window.earlier, c.window.later) == (date(2026, 9, 30), None)
    assert len(c.days) == 31 and c.days[-1].is_today
    # Every other section still covers all 40 days.
    assert c.locations[0].days_total == 40


def test_timeline_last_moves_only_the_window() -> None:
    now = _utc("2026-10-31 12:00")
    a = [on("2026-09-01 00:00", "2026-09-23 10:00"), off("2026-09-23 10:00", None)]
    b = [on("2026-09-01 00:00", "2026-09-23 11:00"), off("2026-09-23 11:00", None)]
    series = [ser(1, "A", a, now), ser(2, "B", b, now)]
    period = compare.period_of(date(2026, 9, 22), date(2026, 10, 31), now, KYIV)
    default = compare.compare(series, period, now, KYIV)
    paged = compare.compare(series, period, now, KYIV, timeline_last=date(2026, 9, 30))

    assert [d.day for d in paged.days] == [date(2026, 9, 22) + _days(i) for i in range(9)]
    assert (paged.window.earlier, paged.window.later) == (None, date(2026, 10, 31))
    assert not any(d.is_today for d in paged.days)
    # Every number but the window and its days is the same.
    for name in ("locations", "together", "spans", "pairs", "simultaneous", "hours", "weeks"):
        assert getattr(paged, name) == getattr(default, name), name


# Hours (D-8)


def test_hours_fixture(fix: compare.Comparison) -> None:
    home = fix.hours[0]
    assert home.key == 1
    assert home.cells[12] == compare.HourCell(60 * M, 420 * M)
    assert home.cells[12].value == 14
    office = fix.hours[1]
    assert office.cells[3].value == 0
    together = fix.hours[3]
    assert together.key is None
    # All three off 12:30-13:00 local on 01.10.
    assert together.cells[12].off_us == 30 * M


def test_hours_split_an_outage_across_midnight() -> None:
    pieces = [
        on("2026-09-28 21:00", "2026-09-29 20:30"),
        off("2026-09-29 20:30", "2026-09-29 21:30"),
        on("2026-09-29 21:30", "2026-09-30 21:00"),
    ]
    c = run([ser(1, "A", pieces, FIX_NOW)], date(2026, 9, 29), date(2026, 9, 30), FIX_NOW)
    cells = c.hours[0].cells
    assert (cells[23].off_us, cells[0].off_us) == (30 * M, 30 * M)
    assert cells[23].monitored_us == cells[0].monitored_us == 2 * H
    assert cells[5].value == 0


def test_hours_on_dst_days() -> None:
    spring_now = _utc("2026-04-01 00:00")
    spring = [on("2026-03-28 22:00", "2026-03-29 21:00")]
    c = run([ser(1, "A", spring, spring_now)], date(2026, 3, 29), date(2026, 3, 29), spring_now)
    cells = c.hours[0].cells
    assert cells[3].monitored_us == 0 and cells[3].value is None
    assert cells[2].monitored_us == cells[4].monitored_us == 1 * H
    fall_now = _utc("2026-10-27 00:00")
    fall = [on("2026-10-24 21:00", "2026-10-25 22:00")]
    c = run([ser(1, "A", fall, fall_now)], DST_FALL_TODAY, DST_FALL_TODAY, fall_now)
    cells = c.hours[0].cells
    # The repeated hour is drawn once: wall-clock buckets, so 24 h in total.
    assert cells[3].monitored_us == 1 * H
    assert sum(cell.monitored_us for cell in cells) == 24 * H
    assert c.locations[0].on_us == 25 * H


def test_hours_of_a_never_monitored_location_have_no_value() -> None:
    pieces = [nm("2026-09-25 00:00", None)]
    c = run([ser(1, "A", pieces, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    assert all(cell.value is None for cell in c.hours[0].cells)


def test_hours_together_uses_all_off_over_joint_time() -> None:
    a = [
        on("2026-09-24 21:00", "2026-09-25 06:00"),
        off("2026-09-25 06:00", "2026-09-25 07:00"),
        on("2026-09-25 07:00", "2026-09-25 21:00"),
    ]
    b = [
        on("2026-09-24 21:00", "2026-09-25 06:30"),
        off("2026-09-25 06:30", "2026-09-25 07:30"),
        on("2026-09-25 07:30", "2026-09-25 21:00"),
    ]
    c = run([ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW)], FIX_FIRST, FIX_FIRST, FIX_NOW)
    t = c.hours[2]
    # 06:30-07:00 UTC is 09:30-10:00 local.
    assert t.key is None and t.cells[9] == compare.HourCell(30 * M, 1 * H)
    assert t.cells[9].value == 50 and t.cells[10].value == 0


# Weeks (D-9)


def test_weeks_fixture(fix: compare.Comparison) -> None:
    newer, older = fix.weeks
    assert (newer.monday, newer.sunday) == (date(2026, 9, 28), date(2026, 10, 4))
    assert (newer.days_in_period, newer.partial) == (4, True)
    home, office, shop = newer.cells
    assert home == compare.WeekCell(150 * M, 2, True)
    assert office == compare.WeekCell(60 * M, 1, True)
    assert shop == compare.WeekCell(210 * M, 2, True)
    assert newer.together == compare.WeekCell(30 * M, 1, True)
    assert (older.monday, older.days_in_period, older.partial) == (date(2026, 9, 21), 3, True)
    assert older.cells[0] == compare.WeekCell(0, 0, True)
    assert older.together == compare.WeekCell(0, 0, True)


def test_weeks_partial_at_both_ends_newest_first() -> None:
    pieces = [on("2026-09-01 00:00", None)]
    c = run([ser(1, "A", pieces, FIX_NOW)], date(2026, 9, 23), date(2026, 9, 29), FIX_NOW)
    got = [(w.monday, w.days_in_period, w.partial) for w in c.weeks]
    assert got == [(date(2026, 9, 28), 2, True), (date(2026, 9, 21), 5, True)]


def test_week_with_the_fall_back_day_sums_real_time() -> None:
    now = _utc("2026-11-01 00:00")
    # Off from Mon 19.10 00:00 EEST to Mon 26.10 00:00 EET: 7 days, one of them 25 h.
    pieces = [off("2026-10-18 21:00", "2026-10-25 22:00")]
    c = run([ser(1, "A", pieces, now)], date(2026, 10, 19), date(2026, 10, 25), now)
    (week,) = c.weeks
    assert not week.partial
    assert week.cells[0] == compare.WeekCell(169 * H, 1, True)


def test_week_together_counts_all_off_episodes() -> None:
    a = [
        off("2026-09-25 01:00", "2026-09-25 02:00"),
        on("2026-09-25 02:00", "2026-09-25 05:00"),
        off("2026-09-25 05:00", "2026-09-25 06:00"),
    ]
    b = [off("2026-09-25 01:30", "2026-09-25 05:30", "2026-09-25 01:30")]
    c = run([ser(1, "A", a, FIX_NOW), ser(2, "B", b, FIX_NOW)], FIX_FIRST, FIX_LAST, FIX_NOW)
    week = c.weeks[-1]
    assert week.monday == date(2026, 9, 21)
    assert week.together == compare.WeekCell(60 * M, 2, True)
    assert c.weeks[0].together == compare.WeekCell(0, 0, False)


# Property test: random timelines over a 10-day P that includes 2026-10-25


def _random_pieces(rng: random.Random, lo: datetime, hi: datetime) -> list[Piece]:
    t = lo
    pieces: list[Piece] = []
    outage: datetime | None = None
    while t < hi:
        step = timedelta(minutes=rng.randint(5, 900))
        end = min(t + step, hi)
        state = rng.choice(["on", "off", "not_monitored", "gap"])
        if state == "off":
            if outage is None or rng.random() < 0.5:
                outage = t
            pieces.append(Piece("off", t, end, outage))
        else:
            if state == "on" or state == "gap":
                outage = None
            if state != "gap":
                pieces.append(Piece(state, t, end, None))
        t = end
    if pieces and rng.random() < 0.5 and pieces[-1].end == hi:
        last = pieces[-1]
        pieces[-1] = Piece(last.state, last.start, None, last.outage_start)
    return pieces


@pytest.mark.parametrize("seed", range(40))
def test_property_partition_and_overlap_bounds(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 (test data, not a secret)
    now = _utc("2026-10-29 12:00")
    lo, hi = _utc("2026-10-19 00:00"), now
    n = rng.randint(2, 4)
    series = [ser(k, f"L{k}", _random_pieces(rng, lo, hi), now) for k in range(n)]
    c = run(series, date(2026, 10, 20), date(2026, 10, 29), now)
    for loc in c.locations:
        assert loc.on_us + loc.off_us + loc.nm_us + loc.nodata_us == loc.period_us
        assert loc.nodata_us >= 0
    t = c.together
    assert t is not None
    assert t.all_off_us <= min(loc.off_us for loc in c.locations)
    assert t.jointly_monitored_us <= min(loc.monitored_us for loc in c.locations)
    assert t.known_us <= c.period.length_us
    pairs = {(p.a, p.b): p.both_off_us for p in c.pairs}
    for i in range(n):
        for j in range(i + 1, n):
            swapped = run([series[j], series[i]], date(2026, 10, 20), date(2026, 10, 29), now)
            assert swapped.pairs[0].both_off_us == pairs[series[i].key, series[j].key]
    for day in c.days:
        assert day.together is not None
        assert sum(lane.off_us for lane in day.lanes) >= day.together.all_off_us
