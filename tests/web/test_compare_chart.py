"""The Compare page's pure presentation (quick task 261103-lfm, R-7): ``compare_chart``.

Task 2a's part: the text helpers (percent, duration, daily total, signed gap, name
truncation, the summary line), the KPI tiles and the scoreboard rows. Task 2b's part: the
shared x-geometry (the bar mapping, the OFF minimum width), the Together total, the segment
titles, the timeline view (31-day window, the "Earlier/Later days" queries), the matrix
buckets and the
zero-off row, the pairs and simultaneous rows, the hour-cell fill and the weeks. All built
from a real
``compare.compare`` result over the plan's page fixture (Home, Office, Shop over the 7 days
ending Thu 2026-10-01, now 16:00 UTC = 19:00 Kyiv). No database, no Django settings.

The modules stay pure: compare_chart.py and compare_views.py hold no SVG or HTML tag literal
and import nothing from django.utils.safestring or django.utils.html (D-6: markup is the
templates' job, escaped by Django).
"""

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from powermon.chart import compare, model
from powermon.web import compare_chart
from powermon.web.compare_chart import (
    bar_width,
    duration_text,
    gap_text,
    kpi_tiles,
    percent_text,
    scoreboard,
    share_text,
    side_count_text,
    summary_text,
    total_text,
    truncate_name,
)

KYIV = "Europe/Kyiv"
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
WEEK = (date(2026, 9, 25), date(2026, 10, 1))
H = model.HOUR_US
M = 60_000_000
WEB = Path(__file__).resolve().parents[2] / "powermon" / "web"


def _t(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


START = _t(9, 24, 0)


def _home() -> compare.Series:
    o1, o2 = _t(9, 28, 6), _t(10, 1, 9)
    return compare.Series(
        1,
        "Home",
        (
            model.Piece("on", START, o1, None),
            model.Piece("off", o1, _t(9, 28, 7), o1),
            model.Piece("not_monitored", _t(9, 28, 7), _t(9, 28, 7, 30), None),
            model.Piece("off", _t(9, 28, 7, 30), _t(9, 28, 8), o1),
            model.Piece("on", _t(9, 28, 8), o2, None),
            model.Piece("off", o2, _t(10, 1, 10), o2),
            model.Piece("on", _t(10, 1, 10), None, None),
        ),
        (
            compare.OutageRec(o1, _t(9, 28, 8), 90 * M),
            compare.OutageRec(o2, _t(10, 1, 10), 60 * M),
        ),
    )


def _office() -> compare.Series:
    o = _t(10, 1, 9)
    return compare.Series(
        2,
        "Office",
        (
            model.Piece("on", START, o, None),
            model.Piece("off", o, _t(10, 1, 10), o),
            model.Piece("on", _t(10, 1, 10), None, None),
        ),
        (compare.OutageRec(o, _t(10, 1, 10), 60 * M),),
    )


def _shop() -> compare.Series:
    o1, o2 = _t(10, 1, 9, 30), _t(10, 1, 14)
    return compare.Series(
        3,
        "Shop",
        (
            model.Piece("on", START, o1, None),
            model.Piece("off", o1, _t(10, 1, 11), o1),
            model.Piece("on", _t(10, 1, 11), o2, None),
            model.Piece("off", o2, None, o2),
        ),
        (compare.OutageRec(o1, _t(10, 1, 11), 90 * M), compare.OutageRec(o2, None, 2 * H)),
    )


def _quiet(key: int, name: str) -> compare.Series:
    return compare.Series(key, name, (model.Piece("on", START, None, None),), ())


def _result(*series: compare.Series, first: date = WEEK[0]) -> compare.Comparison:
    period = compare.period_of(first, WEEK[1], NOW, KYIV)
    return compare.compare(series, period, NOW, KYIV)


FIXTURE = (_home(), _office(), _shop())
STATUSES = {1: "on", 2: "on", 3: "off"}


# Text helpers


def test_percent_text() -> None:
    assert percent_text(42, True) == "42%"
    assert percent_text(0, False) == "0%"
    # Edge: a non-zero value rounding to 0 is "<1%"; 100 stays "100%".
    assert percent_text(0, True) == "<1%"
    assert percent_text(100, True) == "100%"
    # Failure: no denominator gives the dash.
    assert percent_text(None, True) == "—"
    # IN-02: a value below 100 % that rounds to 100 is ">99%", symmetric to "<1%".
    assert percent_text(100, True, full=False) == ">99%"
    assert percent_text(99, True, full=False) == "99%"


def test_share_text() -> None:
    # Expected: half up; 99.4 % prints 99 %.
    assert share_text(994, 1000) == "99%"
    assert share_text(42, 100) == "42%"
    # Edge: 99.5 % rounds to 100 but is not complete; exactly 100 % is "100%".
    assert share_text(995, 1000) == ">99%"
    assert share_text(9_999, 10_000) == ">99%"
    assert share_text(1000, 1000) == "100%"
    assert share_text(4, 1000) == "<1%"
    assert share_text(0, 1000) == "0%"
    # Failure: no denominator gives the dash.
    assert share_text(0, 0) == "—"


def test_duration_and_total_text() -> None:
    assert duration_text(3 * H + 20 * M) == "3h 20m"
    assert duration_text(45 * M) == "45m"
    assert duration_text(1) == "<1m"
    assert total_text(3 * H + 20 * M, 2, True) == "3h 20m · 2"
    # Edge: monitored without an outage; failure: no on or off time at all.
    assert total_text(0, 0, True) == "no outages"
    assert total_text(0, 0, False) == "—"
    with pytest.raises(ValueError):
        total_text(-1, 0, True)


def test_gap_text() -> None:
    # Expected: B's gap against A, named by its side letter, never by its name.
    assert gap_text(4 * M) == "B 4m later"
    assert gap_text(-12 * M) == "B 12m earlier"
    assert gap_text(-(H + 55 * M)) == "B 1h 55m earlier"
    # Edge: below one minute either way is the same minute.
    assert gap_text(59_000_000) == "same minute"
    assert gap_text(-59_000_000) == "same minute"
    assert gap_text(60_000_000) == "B 1m later"
    # Failure: no overlapping outage pair.
    assert gap_text(None) == "—"


def test_side_count_text() -> None:
    # Expected: one short line per side.
    assert side_count_text("A", 8, 9) == "A 8 of 9"
    assert side_count_text("B", 1, 1) == "B 1 of 1"
    # Edge: no coinciding outage, and every outage coinciding.
    assert side_count_text("A", 0, 3) == "A 0 of 3"
    assert side_count_text("B", 3, 3) == "B 3 of 3"
    # Failure: an unknown side or an impossible count.
    for side, coinciding, outages in (("C", 1, 2), ("a", 1, 2), ("A", 3, 2), ("B", -1, 2)):
        with pytest.raises(ValueError):
            side_count_text(side, coinciding, outages)


def test_truncate_name() -> None:
    assert truncate_name("Home", 120) == "Home"
    latin = "Kyiv Obolon district office near the river"
    cut = truncate_name(latin, 120)
    assert cut.endswith("…") and latin.startswith(cut[:-1]) and len(cut) < len(latin)
    cyrillic = "Житомирщина" * 4
    cut_cyr = truncate_name(cyrillic, 120)
    assert cut_cyr.endswith("…") and cyrillic.startswith(cut_cyr[:-1])
    # Wide letters take more room than narrow ones.
    assert len(truncate_name("W" * 40, 120)) < len(truncate_name("i" * 40, 120))
    # Deterministic: the same input gives the same output.
    assert truncate_name(cyrillic, 120) == cut_cyr
    # Edge: a name exactly at the limit stays whole (12 x 10.0 = 120).
    assert truncate_name("m" * 12, 120) == "m" * 12
    assert truncate_name("m" * 13, 120).endswith("…")
    # Failure: no room even for the ellipsis.
    with pytest.raises(ValueError):
        truncate_name("Home", 5)


def test_summary_text() -> None:
    assert summary_text(3, *WEEK) == "3 locations · 25 Sep – 1 Oct 2026 (7 days)"
    # Edge: one location, one day; a range across a year.
    assert summary_text(1, date(2026, 10, 1), date(2026, 10, 1)) == (
        "1 location · 1 Oct 2026 (1 day)"
    )
    assert summary_text(2, date(2026, 12, 30), date(2027, 1, 2)) == (
        "2 locations · 30 Dec 2026 – 2 Jan 2027 (4 days)"
    )
    # Failure: a range that ends before it starts.
    with pytest.raises(ValueError):
        summary_text(1, date(2026, 10, 2), date(2026, 10, 1))


# KPI tiles


def _tiles(result: compare.Comparison) -> dict[str, compare_chart.Tile]:
    return {tile.metric: tile for tile in kpi_tiles(result)}


def test_kpi_tiles_on_the_fixture() -> None:
    tiles = _tiles(_result(*FIXTURE))

    assert list(tiles) == ["off-most", "outages-most", "longest", "all-off", "power-somewhere"]
    assert (tiles["off-most"].name, tiles["off-most"].value) == ("Shop", "3h 30m")
    # Home and Shop both have 2 outages: the tie goes to name order.
    assert (tiles["outages-most"].name, tiles["outages-most"].value) == ("Home", "2")
    longest = tiles["longest"]
    assert (longest.name, longest.value, longest.note) == ("Shop", "2h", "01.10")
    assert tiles["all-off"].value == "30m"
    assert tiles["all-off"].note == "<1% of jointly monitored time"
    # IN-02: power was on somewhere all the time but the 30m all off: ">99%", not "100%".
    assert tiles["power-somewhere"].value == ">99%"
    assert tiles["off-most"].label == "Off time, most"


def test_kpi_tiles_ties_and_edges() -> None:
    # The longest outage ties to the earlier start: Office's 60m and Home's second 60m
    # start together, Home's 90m wins outright; drop Home and Shop to see the tie.
    a = _office()
    b = compare.Series(4, "Yard", a.pieces, a.outages)
    tiles = _tiles(_result(a, b))
    assert (tiles["longest"].name, tiles["off-most"].name) == ("Office", "Office")
    # One location: no all-off and no power-somewhere tile.
    assert [t.metric for t in kpi_tiles(_result(_home()))] == [
        "off-most",
        "outages-most",
        "longest",
    ]
    # Edge: monitored but never off gives "no outages"; the all-off tile says "never all off"
    # (WR-01: monitored together, never all off at once).
    quiet = _tiles(_result(_quiet(5, "Barn"), _quiet(6, "Cellar")))
    assert quiet["off-most"].value == "no outages" and quiet["off-most"].name is None
    assert quiet["outages-most"].value == "no outages"
    assert quiet["longest"].value == "no outages"
    assert quiet["all-off"].value == "never all off"
    assert quiet["all-off"].note == "0% of jointly monitored time"
    assert quiet["power-somewhere"].value == "100%"


def test_kpi_all_off_when_one_location_is_off_alone() -> None:
    # WR-01: Home is off, Barn is on all the time: never all off at once, not "no outages".
    tiles = _tiles(_result(_home(), _quiet(5, "Barn")))
    assert tiles["all-off"].value == "never all off"
    assert tiles["all-off"].note == "0% of jointly monitored time"
    # Failure: never monitored together gives the dash.
    alone = _tiles(_result(_home(), compare.Series(7, "Empty", (), ())))
    assert alone["all-off"].value == "—" and alone["all-off"].note is None


def test_kpi_tiles_without_data_show_a_dash() -> None:
    # Failure: no monitored time at all, nothing can be computed.
    tiles = _tiles(_result(compare.Series(7, "Empty", (), ()), compare.Series(8, "Void", (), ())))
    assert {metric: tile.value for metric, tile in tiles.items()} == {
        "off-most": "—",
        "outages-most": "—",
        "longest": "—",
        "all-off": "—",
        "power-somewhere": "—",
    }


# Scoreboard


def test_scoreboard_on_the_fixture() -> None:
    rows = scoreboard(_result(*FIXTURE), STATUSES)

    assert [(r.rank, r.name, r.status) for r in rows] == [
        (1, "Shop", "off"),
        (2, "Home", "on"),
        (3, "Office", "on"),
    ]
    shop, home, office = rows
    assert (home.off_text, home.off_share_text, home.outages_text) == ("2h 30m", "2%", "2")
    assert (home.avg_text, home.longest_text, home.days_text) == (
        "1h 15m",
        "1h 30m · 28.09",
        "2 of 7",
    )
    assert (office.off_text, office.off_share_text) == ("1h", "1%")
    assert (shop.off_text, shop.longest_text, shop.days_text) == ("3h 30m", "2h · 01.10", "1 of 7")
    assert [r.bar_width for r in rows] == [100, 71, 28]
    # IN-02: Home's 30m not monitored leaves it below 100 %: ">99%".
    assert [r.monitored_text for r in rows] == ["100%", ">99%", "100%"]
    assert not any(r.partial for r in rows)


def test_scoreboard_ties_zero_and_missing_status() -> None:
    a, b = _quiet(5, "Barn"), _quiet(6, "Cellar")
    rows = scoreboard(_result(b, a), {})
    # Ties keep the input (name) order; a missing status reads "waiting".
    assert [(r.rank, r.name, r.status) for r in rows] == [
        (1, "Cellar", "waiting"),
        (2, "Barn", "waiting"),
    ]
    assert [(r.off_text, r.bar_width, r.avg_text, r.longest_text) for r in rows] == [
        ("no outages", 0, "—", "—"),
        ("no outages", 0, "—", "—"),
    ]
    # Edge: partial data below 90% coverage (a 31-day period with 7 days of data).
    wide = scoreboard(_result(*FIXTURE, first=date(2026, 9, 1)), STATUSES)
    assert all(r.partial for r in wide)
    # Failure: no monitored time shows dashes for the off numbers, and 0% monitored.
    (empty,) = scoreboard(_result(compare.Series(7, "Empty", (), ())), {7: "waiting"})
    assert (empty.off_text, empty.off_share_text, empty.monitored_text) == ("—", "—", "0%")
    assert empty.partial


def test_bar_width() -> None:
    assert bar_width(50, 100) == 50
    assert bar_width(100, 100) == 100
    # Edge: a tiny non-zero off time is at least 1.
    assert bar_width(1, 1000 * H) == 1
    # Failure: nothing off, or no maximum, gives 0.
    assert bar_width(0, 100) == 0
    assert bar_width(5, 0) == 0


# Purity (D-6): no markup built in Python


TAGS = ("<svg", "<rect", "<text", "<g ", "<td")
BANNED_IMPORTS = ("django.utils.safestring", "django.utils.html")


def markup_violations(source: str) -> list[str]:
    """Tag literals and banned imports in a module's source."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found += [f"literal {tag!r}" for tag in TAGS if tag in node.value]
        elif isinstance(node, ast.ImportFrom) and node.module in BANNED_IMPORTS:
            found.append(f"import from {node.module}")
        elif isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name in BANNED_IMPORTS]
    return found


@pytest.mark.parametrize("module", ["compare_chart.py", "compare_views.py"])
def test_compare_modules_build_no_markup(module: str) -> None:
    assert markup_violations((WEB / module).read_text(encoding="utf-8")) == []


def test_markup_violations_report_each_break() -> None:
    bad = 'from django.utils.html import format_html\nimport django.utils.safestring\nX = "<svg>"\n'
    assert markup_violations(bad) == [
        "import from django.utils.html",
        "import django.utils.safestring",
        "literal '<svg'",
    ]
    # Edge: a docstring mentioning svg without a tag bracket is fine.
    assert markup_violations('"""An inline svg."""\n') == []


# Task 2b: the shared geometry


DAY = 24 * H


def test_bar_x_maps_the_day_onto_the_bar() -> None:
    assert compare_chart.bar_x(0) == "204.0"
    assert compare_chart.bar_x(DAY) == "828.0"
    assert compare_chart.bar_x(12 * H) == "516.0"
    assert compare_chart.bar_x(3 * H) == "282.0"
    # Edge: a time outside the day is clamped to the bar.
    assert compare_chart.bar_x(-1) == "204.0"
    assert compare_chart.bar_x(DAY + H) == "828.0"
    # The timeline and the hours chart share the bar, so their hour columns line up.
    result = _result(*FIXTURE)
    timeline = compare_chart.timeline(result)
    hours = compare_chart.hours(result)
    assert timeline.grid == hours.grid
    assert timeline.view_box.split()[:3] == hours.view_box.split()[:3] == ["-12", "0", "984"]
    lanes = [lane for group in timeline.groups for lane in group.lanes]
    assert all(lane.transform.startswith("translate(204 ") for lane in lanes)
    assert all(lane.transform.startswith("translate(204 ") for lane in hours.lanes)


def test_tenths() -> None:
    assert compare_chart.tenths(2040) == "204.0"
    assert compare_chart.tenths(69) == "6.9"
    # Edge and failure-adjacent: zero and a negative value keep one decimal.
    assert compare_chart.tenths(0) == "0.0"
    assert compare_chart.tenths(-5) == "-0.5"


def test_off_minimum_width() -> None:
    two = 2 * M
    x, width = compare_chart.seg_span(12 * H, 12 * H + two, minimum=True)
    assert width == "6.9"
    # Widened around its midpoint (12:01 is 312.6 units from the bar start).
    assert abs(float(x) + float(width) / 2 - 312.6) < 0.6
    # Edge: at 00:00 and at 24:00 it stays inside the bar.
    assert compare_chart.seg_span(0, two, minimum=True) == ("0.0", "6.9")
    assert compare_chart.seg_span(DAY - two, DAY, minimum=True) == ("617.1", "6.9")
    # On and not-monitored segments have no minimum; a long OFF keeps its width.
    assert compare_chart.seg_span(0, two, minimum=False) == ("0.0", "0.9")
    assert compare_chart.seg_span(12 * H, 13 * H, minimum=True) == ("312.0", "26.0")
    # Failure: an empty span.
    with pytest.raises(ValueError):
        compare_chart.seg_span(H, H, minimum=True)


def test_together_total() -> None:
    assert compare_chart.together_total(H + 5 * M, 10 * H) == "all off 1h 5m"
    # Edge (R-8): jointly monitored without an all-off moment.
    assert compare_chart.together_total(0, 10 * H) == "never all off"
    # Failure: never jointly monitored.
    assert compare_chart.together_total(0, 0) == "—"


def test_segment_titles() -> None:
    assert compare_chart.segment_title("Home", "off", 12 * H, 13 * H) == (
        "Home · Power off · 12:00–13:00 · 1h"
    )
    # Edge: a segment ending at the day's end shows 24:00.
    assert compare_chart.segment_title("Home", "on", 22 * H, DAY) == (
        "Home · Power on · 22:00–24:00 · 2h"
    )
    assert compare_chart.segment_title("Together", "some_off", 0, 30 * M) == (
        "Together · Some off · 00:00–00:30 · 30m"
    )
    # Failure: partial and no-data spans are never drawn.
    segs = compare_chart._segments("Together", [("partial", 0, H), ("all_on", H, 2 * H)])
    assert [seg.state for seg in segs] == ["all_on"]
    with pytest.raises(KeyError):
        compare_chart.segment_title("Home", "partial", 0, H)


def test_segments_draw_order() -> None:
    segs = compare_chart._segments(
        "Home", [("off", 0, H), ("not_monitored", H, 2 * H), ("on", 2 * H, 3 * H)]
    )
    assert [seg.state for seg in segs] == ["on", "not_monitored", "off"]


# Task 2b: the timeline view


def test_timeline_on_the_fixture() -> None:
    timeline = compare_chart.timeline(_result(*FIXTURE))

    assert [group.day for group in timeline.groups] == [
        "2026-09-25",
        "2026-09-26",
        "2026-09-27",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
    ]
    assert [group.is_today for group in timeline.groups] == [False] * 6 + [True]
    assert all(len(group.lanes) == 4 for group in timeline.groups)
    today = timeline.groups[-1]
    assert today.now is not None and today.now.text == "19:00"
    assert today.now.x == compare_chart.bar_x(19 * H)
    # One week divider, above Monday 28.09.
    dividers = [group.divider for group in timeline.groups]
    assert [d.caption if d else None for d in dividers] == [None] * 3 + ["28.09 – 04.10"] + [
        None
    ] * 3
    home = today.lanes[0]
    offs = [seg for seg in home.segments if seg.state == "off"]
    assert [seg.title for seg in offs] == ["Home · Power off · 12:00–13:00 · 1h"]
    together = today.lanes[-1]
    assert (together.key, together.total) == ("together", "all off 30m")
    assert [seg.title for seg in together.segments if seg.state == "all_off"] == [
        "Together · All off · 12:30–13:00 · 30m"
    ]
    assert timeline.groups[0].lanes[-1].total == "never all off"
    assert home.total == "1h · 1"
    assert timeline.groups[0].lanes[0].total == "no outages"
    assert timeline.columns == ("Home", "Office", "Shop", "Together")
    assert timeline.daily[-1].cells == ("1h · 1", "1h · 1", "3h 30m · 2", "all off 30m")
    assert not timeline.paged
    assert (timeline.earlier_query, timeline.later_query) == (None, None)
    assert len(timeline.legend) == 5
    # The legend items do not overlap.
    xs = [float(item.x) for item in timeline.legend]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)


QUERY = (
    ("loc", ""),
    ("loc", "1"),
    ("loc", "2"),
    ("range", "custom"),
    ("from", "2026-08-22"),
    ("to", "2026-10-01"),
)


def test_timeline_window_and_paging_links() -> None:
    timeline = compare_chart.timeline(_result(*FIXTURE, first=date(2026, 8, 22)), QUERY)

    assert timeline.paged
    assert len(timeline.groups) == 31
    assert timeline.groups[0].day == "2026-09-01"
    assert timeline.window_text == "Showing 01.09 – 01.10 of the period."
    assert timeline.earlier_query == (
        "loc=&loc=1&loc=2&range=custom&from=2026-08-22&to=2026-10-01&tl=2026-08-31"
    )
    assert timeline.later_query is None
    # The first window: no earlier page, a later one back to the period's end.
    period = compare.period_of(date(2026, 8, 22), WEEK[1], NOW, KYIV)
    first = compare.compare(FIXTURE, period, NOW, KYIV, timeline_last=date(2026, 8, 31))
    paged = compare_chart.timeline(first, (*QUERY, ("tl", "2026-08-31")))
    assert [group.day for group in paged.groups][:1] == ["2026-08-22"]
    assert len(paged.groups) == 10
    assert paged.window_text == "Showing 22.08 – 31.08 of the period."
    assert paged.earlier_query is None
    assert paged.later_query == (
        "loc=&loc=1&loc=2&range=custom&from=2026-08-22&to=2026-10-01&tl=2026-10-01"
    )
    # Edge: one location has no Together lane and no "Some off" legend item.
    single = compare_chart.timeline(_result(_home()))
    assert all(len(group.lanes) == 1 for group in single.groups)
    assert [item.kind for item in single.legend] == ["on", "off", "not_monitored", "no_data"]
    assert single.columns == ("Home",)


def test_page_query() -> None:
    # Expected: every other pair kept exactly, in order; tl set last.
    assert compare_chart.page_query((("loc", "1"), ("range", "90d")), date(2026, 8, 31)) == (
        "loc=1&range=90d&tl=2026-08-31"
    )
    # Edge: an old tl (even several) is replaced, and no query at all still works.
    assert compare_chart.page_query(
        (("tl", "x"), ("range", "90d"), ("tl", "2026-01-01")), date(2026, 8, 1)
    ) == ("range=90d&tl=2026-08-01")
    assert compare_chart.page_query((), date(2026, 8, 1)) == "tl=2026-08-01"
    # Failure-proof: user input is urlencoded, never pasted into the URL.
    assert compare_chart.page_query((("loc", "1&tl=x"), ("<b>", '"q"')), date(2026, 8, 1)) == (
        "loc=1%26tl%3Dx&%3Cb%3E=%22q%22&tl=2026-08-01"
    )


def test_timeline_without_now_marker_for_a_past_period() -> None:
    period = compare.period_of(date(2026, 9, 25), date(2026, 9, 30), NOW, KYIV)
    timeline = compare_chart.timeline(compare.compare(FIXTURE, period, NOW, KYIV))

    assert all(group.now is None and not group.is_today for group in timeline.groups)
    assert len(timeline.groups) == 6


# Task 2b: the overlap card


def test_matrix_bucket_edges() -> None:
    assert compare_chart.matrix_bucket(0, False) == 0
    assert compare_chart.matrix_bucket(0, True) == 1  # prints "<1%"
    assert compare_chart.matrix_bucket(9, True) == 1
    assert compare_chart.matrix_bucket(10, True) == 2
    assert compare_chart.matrix_bucket(24, True) == 2
    assert compare_chart.matrix_bucket(25, True) == 3
    assert compare_chart.matrix_bucket(50, True) == 4
    assert compare_chart.matrix_bucket(75, True) == 5
    assert compare_chart.matrix_bucket(100, True) == 5
    # Failure: no share, or a share outside 0..100.
    assert compare_chart.matrix_bucket(None, True) == 0
    with pytest.raises(ValueError):
        compare_chart.matrix_bucket(101, True)


def _off_for(key: int, name: str, minutes: int) -> compare.Series:
    """On from START, off for ``minutes`` from 30.09 00:00 UTC, then on."""
    s = _t(9, 30, 0)
    e = s + timedelta(minutes=minutes)
    pieces = (
        model.Piece("on", START, s, None),
        model.Piece("off", s, e, s),
        model.Piece("on", e, None, None),
    )
    return compare.Series(key, name, pieces, (compare.OutageRec(s, e, minutes * M),))


def test_matrix_bucket_follows_the_printed_percent() -> None:
    # IN-01: 9.6 % prints "10%" and is tinted in the 10-25 bucket; 74.6 % prints "75%".
    def pair(a_min: int, b_min: int) -> compare_chart.MatrixCell:
        view = compare_chart.overlap(_result(_off_for(1, "A", a_min), _off_for(2, "B", b_min)))
        return _cells(view)[(1, 2)]

    edge = pair(1000, 96)
    assert (edge.value, edge.share, edge.bucket) == ("10%", "10", 2)
    below = pair(1000, 94)
    assert (below.value, below.bucket) == ("9%", 1)
    top = pair(1000, 746)
    assert (top.value, top.bucket) == ("75%", 5)


def _cells(view: compare_chart.Overlap) -> dict[tuple[int, int], compare_chart.MatrixCell]:
    return {(row.key, cell.col): cell for row in view.matrix for cell in row.cells}


def test_matrix_zero_overlap_reads_no_overlap_and_no_data_reads_dash() -> None:
    # Pre-ship review: Home (off 28.09 and 01.10) and Night (off 30.09 00:00-01:00 UTC) both
    # had outages that never overlapped, so their cells say "no overlap", never "no outages".
    empty = compare.Series(7, "Empty", (), ())
    view = compare_chart.overlap(_result(_home(), _off_for(6, "Night", 60), empty))
    cells = _cells(view)

    assert (cells[(1, 6)].value, cells[(1, 6)].detail) == ("0%", "no overlap")
    assert (cells[(6, 1)].value, cells[(6, 1)].detail) == ("0%", "no overlap")
    # Edge: a column location with no monitored time at all reads "—", as its own row does.
    assert (cells[(1, 7)].value, cells[(1, 7)].detail) == ("0%", "—")
    assert (cells[(7, 1)].value, cells[(7, 1)].detail) == ("—", "")
    # Failure guard: an overlapping pair still prints its both-off time.
    overlapping = _cells(compare_chart.overlap(_result(_home(), _office())))
    assert (overlapping[(1, 2)].value, overlapping[(1, 2)].detail) == ("40%", "1h")


def test_together_zero_case_with_three_locations_reads_never_all_off() -> None:
    # Pre-ship review: Home and Office overlap 01.10 09:00-10:00 UTC while Quiet stays on,
    # so all three were never off at once; "no overlap" would contradict the pairs table.
    result = _result(_home(), _office(), _quiet(4, "Quiet"))

    assert _tiles(result)["all-off"].value == "never all off"
    view = compare_chart.overlap(result)
    assert [row.both for row in view.pairs][:1] == ["1h"]
    # Edge: with every location off at once the tile shows the duration instead.
    assert _tiles(_result(_home(), _office()))["all-off"].value == "1h"


def test_overlap_on_the_fixture() -> None:
    view = compare_chart.overlap(_result(*FIXTURE))
    cells = _cells(view)

    assert not view.single
    assert (cells[(1, 2)].share, cells[(1, 2)].value, cells[(1, 2)].detail) == ("40", "40%", "1h")
    assert cells[(1, 2)].bucket == 3
    assert (cells[(2, 1)].share, cells[(2, 1)].bucket) == ("100", 5)
    assert (cells[(1, 3)].share, cells[(3, 1)].share, cells[(2, 3)].share) == ("20", "14", "50")
    assert cells[(1, 2)].sr == ("While Home was off, Office was also off 40% of the time (1h).")
    diagonal = cells[(1, 1)]
    assert (diagonal.diagonal, diagonal.share, diagonal.value) == (True, "", "2h 30m")
    assert [(p.name_a, p.name_b, p.both) for p in view.pairs] == [
        ("Home", "Office", "1h"),
        ("Home", "Shop", "30m"),
        ("Office", "Shop", "30m"),
    ]
    first = view.pairs[0]
    assert (first.coinciding_a, first.coinciding_b) == ("A 1 of 2", "B 1 of 1")
    assert (first.start_gap, first.end_gap) == ("same minute", "same minute")
    assert (view.pairs[1].start_gap, view.pairs[1].end_gap) == ("B 30m later", "B 1h later")
    assert view.never is None
    assert len(view.simultaneous) == 1
    sim = view.simultaneous[0]
    assert (sim.start, sim.end, sim.duration, sim.names) == (
        "Thu 01.10 12:00",
        "Thu 01.10 13:00",
        "1h",
        "Home, Office, Shop",
    )
    assert (sim.all_n, sim.all_text) == (True, "all 3")
    assert view.sim_cap is None


def test_overlap_zero_off_row_and_never_pairs() -> None:
    view = compare_chart.overlap(_result(_home(), _quiet(4, "Quiet"), _quiet(5, "Still")))
    cells = _cells(view)

    # A row location with no off time: every other cell is bucket 0, no share, "—".
    for col in (1, 5):
        assert (cells[(4, col)].bucket, cells[(4, col)].share, cells[(4, col)].value) == (
            0,
            "",
            "—",
        )
    # Home's share with a location never off is a real 0%.
    assert (cells[(1, 4)].share, cells[(1, 4)].value, cells[(1, 4)].bucket) == ("0", "0%", 0)
    assert view.pairs == ()
    assert view.never == "3 pairs never were off at the same time."
    assert view.simultaneous == ()
    one_left = compare_chart.overlap(_result(_home(), _quiet(4, "Quiet")))
    assert one_left.never == "1 pair never was off at the same time."


def test_overlap_single_location() -> None:
    view = compare_chart.overlap(_result(_home()))

    assert view.single
    assert (view.matrix, view.pairs, view.simultaneous) == ((), (), ())


# Task 2b: hours and weeks


def test_cell_number() -> None:
    # Expected: the rounded percent without the sign.
    assert compare_chart._cell_number(42, 42, 100) == "42"
    assert compare_chart._cell_number(100, H, H) == "100"
    # Edge: below 100 % but rounding to 100 is ">99", as its title says ">99%".
    assert compare_chart._cell_number(100, 359 * M, 360 * M) == ">99"
    # Failure: no value prints nothing.
    assert compare_chart._cell_number(None, 0, 0) == ""


def test_hour_fill() -> None:
    assert compare_chart.hour_fill(0, 0) == ("track", "1")
    assert compare_chart.hour_fill(0, H) == ("on", "0.35")
    assert compare_chart.hour_fill(1, 1000 * H) == ("off", "0.20")
    assert compare_chart.hour_fill(H, H) == ("off", "1.00")
    assert compare_chart.hour_fill(H, 2 * H) == ("off", "0.60")


def test_hour_fill_together() -> None:
    # WR-01. Expected: all-off time keeps the off scale.
    assert compare_chart.hour_fill(H, 2 * H, together=True) == ("off", "0.60")
    # Edge: monitored together but never all off is the track colour, never "on".
    assert compare_chart.hour_fill(0, H, together=True) == ("track", "1")
    # Failure: no jointly monitored time is the track colour too.
    assert compare_chart.hour_fill(0, 0, together=True) == ("track", "1")


def test_hour_ink() -> None:
    # Expected: the soft ink on a mid off fill, the strong ink on a full one.
    assert compare_chart.hour_ink(H, 2 * H) == "soft"
    assert compare_chart.hour_ink(H, H) == "strong"
    # Edge: the bucket starts at opacity 0.90 (share 7/8 = 0.90), 0.89 stays soft.
    assert compare_chart.hour_fill(7 * H, 8 * H) == ("off", "0.90")
    assert compare_chart.hour_ink(7 * H, 8 * H) == "strong"
    assert compare_chart.hour_fill(86 * H, 100 * H) == ("off", "0.88")
    assert compare_chart.hour_ink(86 * H, 100 * H) == "soft"
    assert compare_chart.hour_ink(1, 1000 * H) == "soft"
    # Failure: no monitored time, or never off (never labelled), keeps the soft ink.
    assert compare_chart.hour_ink(0, 0) == "soft"
    assert compare_chart.hour_ink(H, 0) == "soft"
    assert compare_chart.hour_ink(0, H) == "soft"


def test_hours_on_the_fixture() -> None:
    view = compare_chart.hours(_result(*FIXTURE))
    lanes = {lane.key: lane for lane in view.lanes}

    assert list(lanes) == ["1", "2", "3", "together"]
    home12 = lanes["1"].cells[12]
    assert (home12.pct, home12.label, home12.fill, home12.ink) == ("14", "14", "off", "soft")
    assert home12.title == "Home · 12:00–13:00 · off 14% of monitored time (1h of 7h)"
    office3 = lanes["2"].cells[3]
    assert (office3.pct, office3.label, office3.fill, office3.opacity) == ("0", "", "on", "0.35")
    # Edge: below 10% the share is not printed in the cell.
    shop12 = lanes["3"].cells[12]
    assert (shop12.pct, shop12.label) == ("7", "")
    assert lanes["1"].peak == "peak 09–10 · 14%"
    assert lanes["together"].name == "Together (all off)"
    assert lanes["together"].peak == "peak 12–13 · 7%"
    assert view.together
    # WR-01: an hour monitored together but never all off is the track colour, no number.
    together3 = lanes["together"].cells[3]
    assert (together3.fill, together3.label, together3.pct) == ("track", "", "0")
    assert together3.title == "Together (all off) · 03:00–04:00 · never all off at once"
    together12 = lanes["together"].cells[12]
    assert together12.fill == "off"
    assert together12.title == (
        "Together (all off) · 12:00–13:00 · all off 7% of jointly monitored time (30m of 7h)"
    )


def test_hours_together_zero_and_unmonitored() -> None:
    # Expected: one location off alone; Together is never all off at once.
    lanes = compare_chart.hours(_result(_home(), _quiet(4, "Quiet"))).lanes
    together = lanes[-1]
    assert together.peak == "never all off" and together.tone == "muted"
    assert {cell.fill for cell in together.cells} == {"track"}
    assert {cell.label for cell in together.cells} == {""}
    assert together.cells[9].title == "Together (all off) · 09:00–10:00 · never all off at once"
    # Home's own lane still shows its off hour.
    assert lanes[0].cells[9].fill == "off"
    # Failure: never monitored together gives the dash and "not monitored together".
    nothing = compare.Series(9, "Nothing", (), ())
    alone = compare_chart.hours(_result(_home(), nothing)).lanes[-1]
    assert alone.peak == "—"
    assert {cell.fill for cell in alone.cells} == {"track"}
    assert alone.cells[9].title == "Together (all off) · 09:00–10:00 · not monitored together"
    # One location: no Together row and no Together legend sentence.
    assert not compare_chart.hours(_result(_home())).together


def test_hours_without_monitored_time() -> None:
    nothing = compare.Series(9, "Nothing", (), ())
    view = compare_chart.hours(_result(nothing))

    lane = view.lanes[0]
    assert lane.peak == "—"
    assert {cell.fill for cell in lane.cells} == {"track"}
    assert {cell.pct for cell in lane.cells} == {""}
    assert lane.cells[0].title == "Nothing · 00:00–01:00 · not monitored"
    # Monitored, never off: "no outages".
    quiet = compare_chart.hours(_result(_quiet(4, "Quiet"))).lanes[0]
    assert quiet.peak == "no outages"


def test_weeks_on_the_fixture() -> None:
    view = compare_chart.weeks(_result(*FIXTURE))

    assert view.columns == ("Home", "Office", "Shop", "All off together")
    assert [(row.monday, row.label, row.partial) for row in view.rows] == [
        ("2026-09-28", "28.09 – 04.10", True),
        ("2026-09-21", "21.09 – 27.09", True),
    ]
    newest = {cell.key: cell for cell in view.rows[0].cells}
    assert (newest["1"].text, newest["3"].text, newest["together"].text) == (
        "2h 30m · 2",
        "3h 30m · 2",
        "30m · 1",
    )
    assert newest["3"].bar == 100
    assert newest["together"].bar == 14
    oldest = {cell.key: cell for cell in view.rows[1].cells}
    assert (oldest["1"].text, oldest["1"].tone, oldest["1"].bar) == ("no outages", "muted", 0)
    # Edge: one location has no Together column.
    assert compare_chart.weeks(_result(_home())).columns == ("Home",)


def test_weeks_together_zero_case() -> None:
    # WR-01: monitored together but never all off at once reads "never all off".
    view = compare_chart.weeks(_result(_home(), _quiet(4, "Quiet")))
    cells = {cell.key: cell for cell in view.rows[0].cells}
    assert (cells["together"].text, cells["together"].tone) == ("never all off", "muted")
    assert cells["4"].text == "no outages"
    # Failure: never monitored together gives the dash.
    alone = compare_chart.weeks(_result(_home(), compare.Series(9, "Nothing", (), ())))
    assert {cell.key: cell.text for cell in alone.rows[0].cells}["together"] == "—"
    # The text helper on its own.
    assert compare_chart.week_text(compare.WeekCell(0, 0, True), together=True) == "never all off"
    assert compare_chart.week_text(compare.WeekCell(0, 0, True)) == "no outages"
    assert compare_chart.week_text(compare.WeekCell(H, 1, True), together=True) == "1h · 1"


def _flicker(key: int, name: str, count: int) -> compare.Series:
    """``count`` half-hour outages, one every hour from 25.09 00:00 UTC, then on."""
    base = _t(9, 25, 0)
    hour, half = timedelta(hours=1), timedelta(minutes=30)
    pieces = [model.Piece("on", START, base, None)]
    outages = []
    for k in range(count):
        s = base + k * hour
        pieces.append(model.Piece("off", s, s + half, s))
        end = None if k == count - 1 else s + hour
        pieces.append(model.Piece("on", s + half, end, None))
        outages.append(compare.OutageRec(s, s + half, 30 * M))
    return compare.Series(key, name, tuple(pieces), tuple(outages))


CUT_NOW = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


def _off_since(key: int, name: str, since: datetime, end: datetime | None) -> compare.Series:
    pieces = [model.Piece("on", datetime(2026, 10, 30, tzinfo=UTC), since, None)]
    pieces.append(model.Piece("off", since, end, since))
    if end is not None:
        pieces.append(model.Piece("on", end, None, None))
    off_us = ((end or CUT_NOW) - since) // model.ONE_US
    return compare.Series(key, name, tuple(pieces), (compare.OutageRec(since, end, off_us),))


def _cut_rows(*series: compare.Series) -> tuple[compare_chart.SimRow, ...]:
    day = date(2026, 11, 1)
    period = compare.period_of(day, day, CUT_NOW, KYIV)
    return compare_chart.overlap(compare.compare(series, period, CUT_NOW, KYIV)).simultaneous


def test_simultaneous_cut_by_the_period() -> None:
    # WR-02, the reviewer's probe: both off since 31.10 22:00 local, still off at now 03.11.
    since = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    (row,) = _cut_rows(_off_since(1, "Home", since, None), _off_since(2, "Office", since, None))
    assert (row.start, row.end, row.duration) == (
        "before Sun 01.11 00:00",
        "after Mon 02.11 00:00",
        "≥ 24h",
    )
    # Edge: off exactly from the period's start to its end is shown as is.
    start = datetime(2026, 10, 31, 22, 0, tzinfo=UTC)
    end = datetime(2026, 11, 1, 22, 0, tzinfo=UTC)
    (exact,) = _cut_rows(_off_since(1, "Home", start, end), _off_since(2, "Office", start, end))
    assert (exact.start, exact.end, exact.duration) == (
        "Sun 01.11 00:00",
        "Mon 02.11 00:00",
        "24h",
    )
    # Failure: only one location beyond the edge does not flag the span.
    (one,) = _cut_rows(_off_since(1, "Home", since, None), _off_since(2, "Office", start, end))
    assert (one.start, one.end, one.duration) == ("Sun 01.11 00:00", "Mon 02.11 00:00", "24h")


def test_overlap_simultaneous_cap() -> None:
    view = compare_chart.overlap(_result(_flicker(1, "A", 105), _flicker(2, "B", 105)))

    assert len(view.simultaneous) == 100
    assert view.sim_cap == "Showing the latest 100 of 105."
    # Newest first: the last outage starts 104 h after 25.09 00:00 UTC.
    assert (view.simultaneous[0].start, view.simultaneous[0].end) == (
        "Tue 29.09 11:00",
        "Tue 29.09 11:30",
    )
    assert view.simultaneous[0].names == "A, B"
    # Edge: exactly 100 has no cap line.
    exact = compare_chart.overlap(_result(_flicker(1, "A", 100), _flicker(2, "B", 100)))
    assert len(exact.simultaneous) == 100 and exact.sim_cap is None
