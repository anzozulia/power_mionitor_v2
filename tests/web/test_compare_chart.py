"""The Compare page's pure presentation (quick task 261103-lfm, R-7): ``compare_chart``.

Task 2a's part: the text helpers (percent, duration, daily total, signed gap, name
truncation, the summary line), the KPI tiles and the scoreboard rows, built from a real
``compare.compare`` result over the plan's page fixture (Home, Office, Shop over the 7 days
ending Thu 2026-10-01, now 16:00 UTC = 19:00 Kyiv). No database, no Django settings.

The modules stay pure: compare_chart.py and compare_views.py hold no SVG or HTML tag literal
and import nothing from django.utils.safestring or django.utils.html (D-6: markup is the
templates' job, escaped by Django).
"""

import ast
from datetime import UTC, date, datetime
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
    assert gap_text(4 * M, "Office") == "Office 4m later"
    assert gap_text(-12 * M, "Office") == "Office 12m earlier"
    # Edge: below one minute either way is the same minute.
    assert gap_text(59_000_000, "Office") == "same minute"
    assert gap_text(-59_000_000, "Office") == "same minute"
    assert gap_text(60_000_000, "Office") == "Office 1m later"
    # Failure: no overlapping outage pair.
    assert gap_text(None, "Office") == "—"


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
    assert tiles["power-somewhere"].value == "100%"
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
    # Edge: monitored but never off gives "no outages"; the all-off tile says so too.
    quiet = _tiles(_result(_quiet(5, "Barn"), _quiet(6, "Cellar")))
    assert quiet["off-most"].value == "no outages" and quiet["off-most"].name is None
    assert quiet["outages-most"].value == "no outages"
    assert quiet["longest"].value == "no outages"
    assert quiet["all-off"].value == "no outages"
    assert quiet["all-off"].note == "0% of jointly monitored time"
    assert quiet["power-somewhere"].value == "100%"


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
    assert [r.monitored_text for r in rows] == ["100%", "100%", "100%"]
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
