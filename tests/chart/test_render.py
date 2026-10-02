"""The chart image (D-09, D-15, CHRT-01, CHRT-02, CHRT-07, CHRT-08; chart-spec §2-§9).

Canvas, layout, state colours and totals; today's row, the now marker and the finished
render; previous-week rows, legend, grid and axis; the subtitle's name filter and
truncation; byte-identical output; the per-band memory budget. Scenarios: INV-04 chart
part (server downtime and maintenance are drawn hatched, never in OFF) and K-5 (uk and
ru draw Cyrillic labels).

Pure: no database. Probe positions come from ``render.layout`` (bar x-range, bar tops),
never from hard-coded pixels, except the chart-spec values a test checks. A probe at a
half hour sits about 15 px from the nearest hour edge, inside a solid area.
"""

import ast
import io
import pathlib
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import date, datetime

import pytest
from chart_fixtures import (
    KYIV,
    SAMPLE_NAMES,
    SAMPLE_NOW,
    SAMPLE_TODAY,
    kyiv,
    local_pieces,
    sample_pieces,
)
from PIL import Image

from powermon.chart import render
from powermon.chart.model import HOUR_US, Piece, Week, build_week, next_midnight, wall_us
from powermon.i18n import chart_texts

RGB = tuple[int, int, int]
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
MIN_US = 60_000_000
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART_DIR = REPO_ROOT / "powermon" / "chart"


def _week(
    pieces: list[Piece],
    today: date = SAMPLE_TODAY,
    now: datetime = SAMPLE_NOW,
    *,
    live: bool = True,
) -> Week:
    return build_week(pieces, today=today, now=now, tz=KYIV, live=live)


def _sample(*, live: bool = True) -> Week:
    return _week(sample_pieces(), live=live)


def _open(png: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(png))
    img.load()
    return img


def _render(week: Week, lang: str = "uk") -> Image.Image:
    return _open(render.render_png(week, lang=lang, name=SAMPLE_NAMES.get(lang, "Home")))


def _px(img: Image.Image, x: float, y: float) -> RGB:
    value = img.getpixel((int(x), int(y)))
    assert isinstance(value, tuple)
    return (value[0], value[1], value[2])


def _hours(h: float) -> int:
    return int(h * HOUR_US)


def _region(img: Image.Image, x0: float, y0: float, x1: float, y1: float) -> list[RGB]:
    """Every pixel of the box ``[x0, x1) × [y0, y1)``."""
    return [_px(img, x, y) for y in range(int(y0), int(y1)) for x in range(int(x0), int(x1))]


def _darkest(pixels: list[RGB]) -> RGB:
    return min(pixels, key=sum)


def _near(a: RGB, b: RGB, tol: int) -> bool:
    return all(abs(p - q) <= tol for p, q in zip(a, b, strict=True))


def _total_box(lay: render.Layout, row: int) -> tuple[float, float]:
    """The y span of a row's total text: one font size above its baseline to 8 px below."""
    baseline = lay.bar_ys[row] + render.BAR_H / 2 + render.BASELINE * render.TOTAL_SIZE - 1
    return baseline - render.TOTAL_SIZE, baseline + 8


def test_sample_week_png_is_1280x1000_opaque_rgb() -> None:
    png = render.render_png(_sample(), lang="uk", name=SAMPLE_NAMES["uk"])
    img = _open(png)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert img.format == "PNG"
    assert img.size == (1280, 1000)
    # Opaque: no alpha channel at all (chart-spec §2).
    assert img.mode == "RGB"
    assert _px(img, 0, 0) == render.SURFACE
    assert _px(img, 1279, 999) == render.SURFACE


@pytest.mark.parametrize(
    ("lang", "x0", "x1"), [("uk", 232.0, 954.0), ("en", 261.0, 983.0), ("ru", 231.0, 952.0)]
)
def test_layout_bar_range_per_language(lang: str, x0: float, x1: float) -> None:
    lay = render.layout(_sample(), lang)
    assert lay.bar_x0 == pytest.approx(x0, abs=1.0)
    assert lay.bar_x1 == pytest.approx(x1, abs=1.0)
    # Today is Thursday: +38 above it, the 56 px divider above Friday (chart-spec §3).
    assert lay.bar_ys == (273, 355, 437, 557, 695, 777, 859)
    assert lay.divider_top == 620
    assert lay.grid_top == 254
    assert lay.grid_bottom == 254 + 38 + 7 * 82 + 56 == 922
    assert lay.axis_baseline == 952
    assert lay.hx(0) == lay.bar_x0
    assert lay.hx(24 * HOUR_US) == pytest.approx(lay.bar_x1)


def test_layout_label_column_fits_the_widest_weekday() -> None:
    # Edge: the date starts 14 px after the widest weekday, measured at SemiBold 34.
    label = render.font("semibold", render.LABEL_SIZE)
    for lang in ("uk", "en", "ru"):
        widest = max(label.getlength(day) for day in chart_texts.WEEKDAYS[lang])
        lay = render.layout(_sample(), lang)
        assert lay.date_x == pytest.approx(48 + widest + 14)
        assert lay.bar_x0 == pytest.approx(lay.date_x + label.getlength("00.00") + 24)


def test_INV04_sample_segments_have_their_state_colours() -> None:
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")

    def at(row: int, h: float) -> RGB:
        return _px(img, lay.hx(_hours(h)), lay.bar_ys[row] + 22)

    assert at(MON, 10.5) == render.OFF
    assert at(MON, 14.5) == render.ON
    # After now (14:37) today's track stays empty.
    assert at(THU, 15.5) == render.NO_DATA
    # Previous-week rows are dimmed; their no-data track is no-data itself.
    assert at(FRI, 15.5) == render.dim(render.OFF)
    assert at(FRI, 12.5) == render.dim(render.ON)
    assert at(FRI, 5.5) == render.NO_DATA

    def stripe_colours(row: int, start: float, end: float) -> set[RGB]:
        y = lay.bar_ys[row] + 22
        x0, x1 = lay.hx(_hours(start)) + 2, lay.hx(_hours(end)) - 2
        return set(_region(img, x0, y, x1, y + 1))

    # Wed 03:10-03:52 server downtime and Sat 11:00-12:30 maintenance: hatched, never OFF.
    downtime = stripe_colours(WED, 3 + 10 / 60, 3 + 52 / 60)
    assert {render.NM_BASE, render.NM_INK} <= downtime
    assert render.OFF not in downtime
    maintenance = stripe_colours(SAT, 11, 12.5)
    assert {render.dim(render.NM_BASE), render.dim(render.NM_INK)} <= maintenance
    assert render.dim(render.OFF) not in maintenance


def test_row_totals_are_drawn_right_aligned() -> None:
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")
    top, bottom = _total_box(lay, MON)
    # The total ends at x 1232: the 70 px left of it hold ink, the 40 px right of it none.
    assert any(p != render.SURFACE for p in _region(img, 1232 - 70, top, 1232, bottom))
    assert set(_region(img, 1233, top, 1280, bottom)) == {render.SURFACE}
    # Mon's duration is ink-primary on a current-week row.
    assert _near(_darkest(_region(img, 980, top, 1232, bottom)), render.INK_PRIMARY, 12)

    # The dimmed Sun 27.09 "no outages" is ink-muted, not ink-secondary (chart-spec §4, §7).
    top, bottom = _total_box(lay, SUN)
    sunday = _darkest(_region(img, 960, top, 1232, bottom))
    assert _near(sunday, render.INK_MUTED, 12)
    assert sum(sunday) > sum(render.INK_SECONDARY) + 3 * 12


def test_row_totals_zero_outages_and_no_data_inks() -> None:
    # Edge: a current-week day on all day says "no outages" in ink-secondary; a day that was
    # only not monitored says "—" in ink-muted (chart-spec §8 Daily total).
    pieces = local_pieces(
        [
            ("on", "2026-09-28 00:00", "2026-09-29 00:00"),
            ("not_monitored", "2026-09-29 00:00", "2026-09-30 00:00"),
        ]
    )
    week = _week(pieces)
    img = _render(week)
    lay = render.layout(week, "uk")
    top, bottom = _total_box(lay, MON)
    assert _near(_darkest(_region(img, 960, top, 1232, bottom)), render.INK_SECONDARY, 12)
    top, bottom = _total_box(lay, TUE)
    assert _near(_darkest(_region(img, 960, top, 1232, bottom)), render.INK_MUTED, 12)


def test_unknown_language_renders_the_en_chart() -> None:
    week = _sample()
    name = SAMPLE_NAMES["en"]
    assert render.render_png(week, lang="de", name=name) == render.render_png(
        week, lang="en", name=name
    )


def test_a_week_without_seven_rows_raises() -> None:
    week = _sample()
    with pytest.raises(ValueError, match="7 rows"):
        render.render_png(replace(week, rows=week.rows[:6]), lang="uk", name="Дім")


def test_font_rejects_an_unknown_face() -> None:
    with pytest.raises(ValueError, match="unknown font face"):
        render.font("bold", 30)


def test_font_fails_loudly_when_the_bundled_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Failure: never fall back to a system font of the same name.
    monkeypatch.setattr(render, "FONT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="bundled font file is missing"):
        render.font.__wrapped__("regular", 30)


# --- Today's row, the now marker and the finished render (chart-spec §7, CHRT-02, D-01) ---


def _pill_centre_y(bar_y: float) -> float:
    # The 36 px pill's bottom is 2 px above the line top (bar top - 8).
    return bar_y - 8 - 2 - 36 + 18


def _between(pixel: RGB, low: RGB, high: RGB) -> bool:
    return all(a <= p <= b for a, p, b in zip(low, pixel, high, strict=True))


def test_live_today_row_has_band_line_and_pill() -> None:
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")
    bar_y = lay.bar_ys[THU]
    assert _px(img, 35, bar_y + 22) == render.TODAY_BAND
    now_x = round(lay.hx(wall_us(week.now, KYIV, end=False)))
    # The 3 px now line runs from bar top - 8 to bar bottom + 8, over the bar. Its ends are
    # rounded (rx 1.5), so the first and last pixel rows are ink blended with the band.
    for y in range(int(bar_y) - 6, int(bar_y) + 51):
        assert _px(img, now_x, y) == render.INK_PRIMARY, y
    for y in (int(bar_y) - 8, int(bar_y) - 7, int(bar_y) + 51):
        assert sum(_px(img, now_x, y)) < sum(render.TODAY_BAND) - 150, y
    assert _px(img, now_x, bar_y - 9) == render.TODAY_BAND
    assert _px(img, now_x, bar_y + 52) == render.TODAY_BAND
    # The pill above it is ink-primary with the time in surface ink.
    pill = _region(img, now_x - 20, bar_y - 46, now_x + 21, bar_y - 10)
    assert render.INK_PRIMARY in pill
    assert render.SURFACE in pill


def test_finished_render_has_no_line_and_no_pill() -> None:
    # D-01: the final render of a day has now = its end, no line and no pill; the band stays.
    week = _week(sample_pieces(), now=next_midnight(SAMPLE_TODAY, KYIV), live=False)
    img = _render(week)
    lay = render.layout(week, "uk")
    bar_y = lay.bar_ys[THU]
    pill_zone = _region(img, lay.bar_x0 - 8, bar_y - 46, lay.bar_x1 + 9, bar_y - 10)
    assert render.INK_PRIMARY not in pill_zone
    line_zone = _region(img, lay.bar_x0 - 8, bar_y - 8, lay.bar_x1 + 9, bar_y + 52)
    assert render.INK_PRIMARY not in line_zone
    assert _px(img, 35, bar_y + 22) == render.TODAY_BAND
    # The whole day is drawn to 24:00: the open on interval runs to the day's end.
    for h in range(24):
        assert _px(img, lay.hx(_hours(h + 0.5)), bar_y + 22) in {render.ON, render.OFF}, h
    assert _px(img, lay.hx(_hours(23.5)), bar_y + 22) == render.ON


@pytest.mark.parametrize(("hm", "edge"), [("00:01", "left"), ("23:59", "right")])
def test_pill_is_clamped_at_both_ends(hm: str, edge: str) -> None:
    week = _week(sample_pieces(), now=kyiv(f"2026-10-01 {hm}"))
    img = _render(week)
    lay = render.layout(week, "uk")
    y = _pill_centre_y(lay.bar_ys[THU])
    if edge == "left":
        # The pill's left edge is at bar_x0 - 8, never further left.
        assert _px(img, lay.bar_x0 - 4, y) == render.INK_PRIMARY
        assert _px(img, lay.bar_x0 - 10, y) == render.SURFACE
    else:
        assert _px(img, lay.bar_x1 + 4, y) == render.INK_PRIMARY
        assert _px(img, lay.bar_x1 + 10, y) == render.SURFACE


def test_now_at_midnight_leaves_an_empty_today_bar() -> None:
    # Edge (CHRT-02 empty): now is exactly today's local midnight.
    week = _week(sample_pieces(), now=kyiv("2026-10-01 00:00"))
    assert week.today_row.segments == ()
    img = _render(week)
    lay = render.layout(week, "uk")
    bar_y = lay.bar_ys[THU]
    for quarter in range(1, 96):
        if quarter % 4:
            x = lay.hx(quarter * 15 * MIN_US)
            assert _px(img, x, bar_y + 22) == render.NO_DATA, quarter
    assert _px(img, lay.bar_x0 - 4, _pill_centre_y(bar_y)) == render.INK_PRIMARY
    assert _px(img, lay.bar_x0 - 10, _pill_centre_y(bar_y)) == render.SURFACE


def test_nothing_is_drawn_after_now() -> None:
    # The prohibition: right of now, today's row shows only the empty track (chart-spec §7, §9).
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")
    bar_y = lay.bar_ys[THU]
    for quarter in range(59, 96):  # 14:45 to 23:45, between the hour separators
        if quarter % 4:
            x = lay.hx(quarter * 15 * MIN_US)
            assert _px(img, x, bar_y + 22) == render.NO_DATA, quarter
    # Every pixel right of the line (away from the rounded end) is the empty track or an
    # hour separator over it, never a state colour.
    now_x = lay.hx(wall_us(week.now, KYIV, end=False))
    for pixel in _region(img, now_x + 3, bar_y, lay.bar_x1 - 9, bar_y + render.BAR_H):
        assert _between(pixel, render.NO_DATA, render.SURFACE), pixel


def _off_runs(img: Image.Image, lay: render.Layout, row: int) -> list[tuple[int, int]]:
    """The ``[start, end)`` runs of exact OFF pixels on a bar's centre row."""
    y = lay.bar_ys[row] + 22
    runs: list[list[int]] = []
    for x in range(int(lay.bar_x0) - 2, int(lay.bar_x1) + 3):
        if _px(img, x, y) != render.OFF:
            continue
        if runs and runs[-1][1] == x:
            runs[-1][1] = x + 1
        else:
            runs.append([x, x + 1])
    return [(start, end) for start, end in runs]


def test_min_off_width_at_both_ends() -> None:
    # One-minute outages at 00:00 (Mon), 23:59 (Tue) and half past noon (Wed, between
    # two hour separators): each is drawn 8 px wide and kept inside the bar (chart-spec §6).
    pieces = local_pieces(
        [
            ("off", "2026-09-28 00:00", "2026-09-28 00:01"),
            ("on", "2026-09-28 00:01", "2026-09-29 23:59"),
            ("off", "2026-09-29 23:59", "2026-09-30 00:00"),
            ("on", "2026-09-30 00:00", "2026-09-30 12:30"),
            ("off", "2026-09-30 12:30", "2026-09-30 12:31"),
            ("on", "2026-09-30 12:31", None),
        ]
    )
    week = _week(pieces)
    img = _render(week)
    lay = render.layout(week, "uk")
    ((start, end),) = _off_runs(img, lay, MON)
    assert 7 <= end - start <= 8
    assert abs(start - round(lay.bar_x0)) <= 1
    ((start, end),) = _off_runs(img, lay, TUE)
    assert 7 <= end - start <= 8
    assert abs(end - round(lay.bar_x1)) <= 1
    ((start, end),) = _off_runs(img, lay, WED)
    assert 7 <= end - start <= 8
    assert start <= lay.hx(_hours(12.5) + MIN_US // 2) <= end
    # OFF is drawn over its on neighbours, which continue right next to it.
    y = lay.bar_ys[WED] + 22
    assert _px(img, start - 2, y) == render.ON
    assert _px(img, end + 1, y) == render.ON


def test_off_over_not_monitored_sub_pixel_spans_and_repeated_hatch() -> None:
    # chart-spec §6: OFF is drawn over not monitored; only OFF has a minimum width, so a
    # one-second maintenance blip is not drawn at all; two not-monitored spans in one row
    # are both hatched from the same canvas-anchored pattern.
    pieces = local_pieces(
        [
            ("on", "2026-09-28 00:00", "2026-09-28 02:10"),
            ("not_monitored", "2026-09-28 02:10", "2026-09-28 02:50"),
            ("on", "2026-09-28 02:50", "2026-09-28 05:10"),
            ("not_monitored", "2026-09-28 05:10", "2026-09-28 05:50"),
            ("on", "2026-09-28 05:50", "2026-09-28 10:30:00"),
            ("not_monitored", "2026-09-28 10:30:00", "2026-09-28 10:30:01"),
            ("on", "2026-09-28 10:30:01", "2026-09-28 12:10"),
            ("not_monitored", "2026-09-28 12:10", "2026-09-28 12:30"),
            ("off", "2026-09-28 12:30", "2026-09-28 12:31"),
            ("on", "2026-09-28 12:31", None),
        ]
    )
    week = _week(pieces)
    img = _render(week)
    lay = render.layout(week, "uk")
    y = lay.bar_ys[MON] + 22
    for start, end in ((2 + 10 / 60, 2 + 50 / 60), (5 + 10 / 60, 5 + 50 / 60)):
        span = set(_region(img, lay.hx(_hours(start)) + 2, y, lay.hx(_hours(end)) - 2, y + 1))
        assert {render.NM_BASE, render.NM_INK} <= span, (start, end)
    blip = lay.hx(_hours(10.5))
    assert set(_region(img, blip - 3, y, blip + 4, y + 1)) == {render.ON}
    ((start, end),) = _off_runs(img, lay, MON)
    assert 7 <= end - start <= 8
    # The widened OFF reaches back over the end of the not-monitored span.
    assert start < lay.hx(_hours(12.5)) < end


# --- Previous week, legend, grid and axis (chart-spec §5-§7, CHRT-07, CHRT-08, K-5) ---


def _gridline_columns(lay: render.Layout) -> set[int]:
    return {int(lay.hx(_hours(h))) + d for h in range(0, 25, 3) for d in (-1, 0, 1)}


def test_previous_week_divider_and_dimmed_text() -> None:
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")
    top = lay.divider_top
    assert top == 620
    caption_end = 48 + render.font("medium", render.HEAD_SIZE).getlength(chart_texts.DIVIDER["uk"])
    caption = _region(img, 48, top, caption_end + 1, top + render.DIVIDER_H)
    assert _near(_darkest(caption), render.INK_MUTED, 12)
    # The 1.5 px hairline is centred at zone top + 32 and runs to x 1232.
    hairline_y = top + 32
    for x in range(int(caption_end) + 17, 1232):
        assert _near(_px(img, x, hairline_y), render.GRID, 10), x
    assert _px(img, 1232, hairline_y) == render.SURFACE
    # It starts 16 px after the caption: the gap holds only surface and gridlines.
    gridlines = _gridline_columns(lay)
    for x in range(int(caption_end) + 3, int(caption_end) + 15):
        if x not in gridlines:
            assert _px(img, x, hairline_y) == render.SURFACE, x
    # Dimmed row labels are ink-muted.
    fri_label = _region(img, 48, lay.bar_ys[FRI], lay.bar_x0 - 24, lay.bar_ys[FRI] + 44)
    assert _near(_darkest(fri_label), render.INK_MUTED, 12)

    # Monday: six dimmed rows under the divider (chart-spec §10 row mapping).
    monday = _week(sample_pieces(), today=date(2026, 9, 28), now=kyiv("2026-09-28 14:37"))
    assert sum(row.dimmed for row in monday.rows) == 6
    mon_lay = render.layout(monday, "uk")
    assert mon_lay.divider_top == 254 + 38 + 82
    assert mon_lay.grid_bottom == 922
    # Sunday: no dimmed row, no divider, and the layout ends 56 px higher.
    sunday = _week(sample_pieces(), today=date(2026, 10, 4), now=kyiv("2026-10-04 14:37"))
    sun_lay = render.layout(sunday, "uk")
    assert sun_lay.divider_top is None
    assert sun_lay.grid_bottom == 866
    assert sun_lay.axis_baseline == 896
    sun_img = _render(sunday)
    assert sun_img.size == (1280, 1000)
    assert set(_region(sun_img, 0, 930, 1280, 1000)) == {render.SURFACE}


def test_dim_colours_match_the_spec() -> None:
    assert render.dim(render.ON) == (0xA0, 0xD9, 0xB7)
    assert render.dim(render.OFF) == (0xDF, 0x84, 0x84)
    assert render.dim(render.NM_BASE) == (0xEC, 0xEB, 0xE7)
    assert render.dim(render.NM_INK) == (0xC5, 0xC3, 0xBD)
    # Edge: the surface is its own dimmed colour.
    assert render.dim(render.SURFACE) == render.SURFACE
    # Edge: a dimmed row's empty track is no-data itself (#E5E4DE), never dim(no-data).
    assert render.dim(render.NO_DATA) != render.NO_DATA
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")
    assert _px(img, lay.hx(_hours(5.5)), lay.bar_ys[FRI] + 22) == render.NO_DATA


def test_legend_axis_and_hour_cells() -> None:
    week = _sample()
    img = _render(week)
    lay = render.layout(week, "uk")
    # Four swatches in a fixed order: on, off, not monitored (hatched), no data.
    legend_font = render.font("regular", render.LEGEND_SIZE)
    lefts = []
    x = 48.0
    for label in chart_texts.LEGEND["uk"]:
        lefts.append(x)
        x += 38 + 12 + legend_font.getlength(label) + 34
    on_x, off_x, nm_x, no_data_x = lefts
    assert _px(img, on_x + 19, 165) == render.ON
    assert _px(img, on_x + 19, 188) == render.ON
    assert _px(img, on_x + 19, 164) == render.SURFACE
    assert _px(img, on_x + 19, 189) == render.SURFACE
    assert _px(img, off_x + 19, 177) == render.OFF
    assert {render.NM_BASE, render.NM_INK} <= set(_region(img, nm_x + 3, 168, nm_x + 35, 186))
    assert _px(img, no_data_x + 19, 177) == render.NO_DATA
    # Each label is ink-secondary, 12 px after its swatch.
    label = _region(img, on_x + 50, 160, on_x + 50 + legend_font.getlength("Світло є"), 194)
    assert _near(_darkest(label), render.INK_SECONDARY, 12)
    # The totals header, right-aligned at x 1232 on baseline 236, is ink-muted.
    header = _region(img, 1000, 236 - 27, 1232, 236 + 7)
    assert _near(_darkest(header), render.INK_MUTED, 12)
    assert set(_region(img, 1233, 200, 1280, 250)) == {render.SURFACE}
    # Axis labels 00..24 sit on the axis baseline under their gridlines.
    for h in range(0, 25, 3):
        centre = lay.hx(_hours(h))
        box = _region(img, centre - 20, lay.axis_baseline - 24, centre + 21, lay.axis_baseline + 1)
        assert _near(_darkest(box), render.INK_MUTED, 12), h
    # Gridlines run through every zone (a row box under its bar, the pill zone above the
    # pill, the divider zone above its caption, the last row box) and stop at the plot's
    # top and bottom.
    for h in range(0, 25, 3):
        columns = range(int(lay.hx(_hours(h))) - 1, int(lay.hx(_hours(h))) + 2)
        for y in (330, 505, 630, 915):
            assert _near(_darkest([_px(img, c, y) for c in columns]), render.GRID, 10), (h, y)
        assert {_px(img, c, 250) for c in columns} == {render.SURFACE}
        assert {_px(img, c, 925) for c in columns} == {render.SURFACE}
    # Hour cells: a separator over the segments, 90 % at 03:00 and 55 % at 01:00.
    y = lay.bar_ys[MON] + 22

    def cell(h: int) -> RGB:
        return _px(img, round(lay.hx(_hours(h)) * render.S) // render.S, y)

    strong, weak = cell(3), cell(1)
    assert sum(render.ON) < sum(weak) < sum(strong) <= sum(render.SURFACE)


def test_K5_uk_and_ru_draw_cyrillic_labels() -> None:
    week = _sample()
    title_box = (48, 40, 700, 90)
    legend_box = (48, 160, 1232, 195)
    renders = {lang: _render(week, lang) for lang in ("uk", "en", "ru")}
    for box, ink in ((title_box, render.INK_PRIMARY), (legend_box, render.INK_SECONDARY)):
        crops = {lang: img.crop(box) for lang, img in renders.items()}
        for lang in ("uk", "ru"):
            colours = crops[lang].getcolors(maxcolors=1 << 16) or []
            assert ink in {colour for _, colour in colours}, (lang, box)
            assert crops[lang].tobytes() != crops["en"].tobytes(), (lang, box)
        assert crops["uk"].tobytes() != crops["ru"].tobytes(), box


# --- The subtitle's name (D-15, chart-spec §8), determinism and memory (chart-spec §9, D-09) ---

UK_RANGE = "28 вересня – 4 жовтня 2026"


def test_subtitle_keeps_a_name_that_fits() -> None:
    week = _sample()
    assert render.subtitle_text("Дім, Оболонь", week, "uk") == f"Дім, Оболонь · {UK_RANGE}"
    assert render.subtitle_text("Home, Obolon", week, "en") == "Home, Obolon · 28 Sep – 4 Oct 2026"
    # The filter runs first: the emoji goes, the rest of the name stays.
    assert render.subtitle_text("🏠 Дім", week, "uk") == f"Дім · {UK_RANGE}"


def test_subtitle_drops_the_name_when_nothing_printable_is_left() -> None:
    # Edge (CHRT-08 empty): no "{name} · " prefix when nothing printable is left.
    week = _sample()
    assert render.subtitle_text("🏠", week, "uk") == UK_RANGE
    assert render.subtitle_text("", week, "uk") == UK_RANGE
    assert render.subtitle_text("​\n ", week, "uk") == UK_RANGE
    # The image draws the same subtitle as for a location without a name.
    assert render.render_png(week, lang="uk", name="🏠") == render.render_png(
        week, lang="uk", name=""
    )


def test_subtitle_truncates_long_names_with_an_ellipsis() -> None:
    # Edge (CHRT-08 encoding): measured in rendered pixels, cut at whole code points.
    week = _sample()
    sub_font = render.font("regular", render.SUB_SIZE)
    text = render.subtitle_text("Ш" * 100, week, "uk")
    assert sub_font.getlength(text) <= render.SUBTITLE_MAX == 1184
    suffix = f"… · {UK_RANGE}"
    assert text.endswith(suffix)
    kept = text.removesuffix(suffix)
    assert kept and set(kept) == {"Ш"}
    # The cut keeps as much of the name as fits: one more letter would not.
    assert sub_font.getlength(f"{kept}Ш{suffix}") > render.SUBTITLE_MAX
    # A cut that lands after a space drops the space too, never "Ш …".
    spaced = render.subtitle_text(f"{kept} {'Ш' * 50}", week, "uk")
    assert spaced == f"{kept}{suffix}"


def test_render_is_byte_identical() -> None:
    # chart-spec §9: the same inputs give the same bytes, whatever the font cache holds.
    week = _sample()
    first = render.render_png(week, lang="uk", name=SAMPLE_NAMES["uk"])
    render.font.cache_clear()
    second = render.render_png(week, lang="uk", name=SAMPLE_NAMES["uk"])
    assert first == second
    # Edge: another location name changes the image.
    assert render.render_png(week, lang="uk", name="Офіс") != first


MEMORY_PROBE = textwrap.dedent(
    """
    import sys
    import time
    from datetime import UTC, date, datetime

    from powermon.chart import render
    from powermon.chart.model import Piece, build_week


    def at(text):
        return datetime.fromisoformat(text).replace(tzinfo=UTC)


    def peak_rss_kib():
        # VmHWM is this process image's own peak. ru_maxrss would not do: Linux keeps the
        # forking parent's (pytest's) high-water mark in it across fork and exec.
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
        raise RuntimeError("no VmHWM in /proc/self/status")


    sizes = (
        render.TITLE_SIZE, render.SUB_SIZE, render.LEGEND_SIZE, render.LABEL_SIZE,
        render.TOTAL_SIZE, render.HEAD_SIZE, render.AXIS_SIZE, render.NOW_SIZE,
    )
    for face in ("regular", "medium", "semibold"):
        for size in sizes:
            render.font(face, size)
    # Kyiv is UTC+3: monitoring from Fri 10:42, an outage, maintenance, an open on.
    pieces = [
        Piece("on", at("2026-09-25T07:42"), at("2026-09-28T05:00"), None),
        Piece("off", at("2026-09-28T05:00"), at("2026-09-28T09:05"), at("2026-09-28T05:00")),
        Piece("on", at("2026-09-28T09:05"), at("2026-09-30T00:10"), None),
        Piece("not_monitored", at("2026-09-30T00:10"), at("2026-09-30T00:52"), None),
        Piece("on", at("2026-09-30T00:52"), None, None),
    ]
    week = build_week(
        pieces, today=date(2026, 10, 1), now=at("2026-10-01T11:37"), tz="Europe/Kyiv",
        live=True,
    )
    before = peak_rss_kib()
    start = time.perf_counter()
    png = render.render_png(week, lang="uk", name="Дім, Оболонь")
    seconds = time.perf_counter() - start
    after = peak_rss_kib()
    print(after - before, f"{seconds:.3f}", len(png), "django" in sys.modules)
    """
)


def test_render_peak_memory_stays_within_the_band_budget() -> None:
    # D-09: per-band 4× layers keep a render's peak RSS growth to ~+25-35 MB; a full 4×
    # canvas would need ~+180 MB. Measured in a fresh interpreter, in KiB, after every
    # font is loaded (the container is Linux, so /proc/self/status is there).
    result = subprocess.run(
        [sys.executable, "-c", MEMORY_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    growth_kib, seconds, size, django_loaded = result.stdout.split()
    print(f"render peak RSS growth {growth_kib} KiB, {seconds} s, {size} bytes")
    # Above zero: a render does allocate its bands, so 0 would mean the probe measured
    # nothing.
    assert 0 < int(growth_kib) < 80 * 1024
    assert int(size) > 10_000
    # The renderer pulls in nothing from Django, not even indirectly.
    assert django_loaded == "False"


def _imported_modules(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_chart_modules_stay_pure() -> None:
    render_imports = _imported_modules(CHART_DIR / "render.py")
    glyph_imports = _imported_modules(CHART_DIR / "glyphs.py")
    # The scan saw the real modules.
    assert "PIL" in render_imports
    assert "unicodedata" in glyph_imports
    for names in (render_imports, glyph_imports):
        assert sorted(n for n in names if n.split(".")[0] == "django") == []
    # fontTools is a dev tool: the committed table replaces it at run time.
    assert sorted(n for n in glyph_imports if n.split(".")[0] == "fontTools") == []
