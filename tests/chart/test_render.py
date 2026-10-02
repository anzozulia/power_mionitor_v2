"""The chart image: canvas, layout, state colours and totals (D-09, CHRT-01, CHRT-07).

docs/chart-spec.md sections 2-6. Scenarios: INV-04 chart part (server downtime and
maintenance are drawn hatched, never in OFF).

Pure: no database. Probe positions come from ``render.layout`` (bar x-range, bar tops),
never from hard-coded pixels, except the chart-spec values a test checks. A probe at a
half hour sits about 15 px from the nearest hour edge, inside a solid area.
"""

import io
import pathlib
from dataclasses import replace
from datetime import date, datetime

import pytest
from chart_fixtures import (
    KYIV,
    SAMPLE_NAMES,
    SAMPLE_NOW,
    SAMPLE_TODAY,
    local_pieces,
    sample_pieces,
)
from PIL import Image

from powermon.chart import render
from powermon.chart.model import HOUR_US, Piece, Week, build_week
from powermon.i18n import chart_texts

RGB = tuple[int, int, int]
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
MIN_US = 60_000_000


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
    """The baseline of a row's total text and the y above which its glyphs start."""
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
