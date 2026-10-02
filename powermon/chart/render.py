"""The weekly chart image (CHRT-01, CHRT-02, CHRT-07, CHRT-08; docs/chart-spec.md §2-§7).

``render_png(week, lang=..., name=...)`` draws one ``model.Week`` as the 1280×1000 opaque
RGB PNG of chart-spec §2. It is a pure function of the week (built from the stored
timeline, ``now`` and the display time zone), the language and the location name: the
same inputs, font files and Pillow version give the same bytes (chart-spec §9, D-09). It
draws exactly the model's wall-clock segments and real-time totals and recomputes no data.

How it draws (D-09, STACK Gotcha 1, 03-RESEARCH Pattern 2):
- Pillow with the bundled Inter 4.1 TTFs, loaded by file path from ``FONT_DIR`` with
  ``ImageFont.Layout.BASIC`` only, so no system font, fontconfig or RAQM shaping can
  change a render.
- Each horizontal band (a row box or a zone of the plot) is cut from the 1× canvas,
  scaled up 4× with NEAREST, drawn on with integer 4× coordinates, reduced with
  ``Image.reduce(4)`` (a box filter, so shapes come out anti-aliased) and pasted back.
  Untouched pixels round-trip exactly, and no full 4× canvas is ever allocated.
- All text is drawn at 1× after every band is pasted, so no band can cover text.

Pure: imports nothing from Django. The web process must never import this module (it
pulls in Pillow); only the worker renders.
"""

import io
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from powermon.chart.model import DAY_US, Row, Week
from powermon.i18n import chart_texts
from powermon.i18n.strings import resolve_language

RGB = tuple[int, int, int]

# Canvas (chart-spec §2) and the supersampling factor of every band.
W = 1280
H = 1000
S = 4

# Colour tokens (chart-spec §5).
SURFACE: RGB = (252, 252, 251)
INK_PRIMARY: RGB = (26, 26, 25)
INK_SECONDARY: RGB = (82, 81, 78)
INK_MUTED: RGB = (119, 117, 112)
GRID: RGB = (227, 226, 220)
TODAY_BAND: RGB = (244, 243, 239)
ON: RGB = (98, 194, 138)
OFF: RGB = (204, 52, 52)
NO_DATA: RGB = (229, 228, 222)
NM_BASE: RGB = (226, 224, 218)
NM_INK: RGB = (160, 157, 148)

# Layout grid (chart-spec §3), in canvas px.
PAD_X = 48
Y_TITLE = 82
Y_SUB = 128
Y_LEGEND = 186
Y_COLHEAD = 236
Y_PLOT = 254
ROW_PITCH = 82
BAR_H = 44
BAR_R = 8
BAR_TOP = 19  # the bar starts this far below its row's top
NOW_EXTRA = 38  # room above today's row for the now pill
DIVIDER_H = 56  # the "last week" zone above the first dimmed row
GAP = 24  # between the label column and the bar, and between the bar and the totals
DATE_GAP = 14  # between the widest weekday and the date
AXIS_GAP = 30  # from the grid bottom to the hour-axis baseline
MIN_OFF_PX = 8
SUBTITLE_MAX = W - 2 * PAD_X  # 1184

# Font sizes (chart-spec §4).
TITLE_SIZE = 48
SUB_SIZE = 30
LEGEND_SIZE = 30
LABEL_SIZE = 34
TOTAL_SIZE = 32
HEAD_SIZE = 27
AXIS_SIZE = 30
NOW_SIZE = 28
# Text sits on its bar: baseline = bar centre + 0.36 × font size (chart-spec §4).
BASELINE = 0.36

# Not-monitored hatch (chart-spec §5): 45° stripes every 14 px, each 6 px wide, measured
# across the stripes. Along x + y the period and the width are √2 times longer.
HATCH_PERIOD = 14 * math.sqrt(2)
HATCH_STRIPE = 6 * math.sqrt(2)

# Draw order (chart-spec §6): on, then not monitored, then off, so OFF is always on top.
_ORDER = {"on": 0, "not_monitored": 1, "off": 2}

FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONT_FILES = {
    "regular": "Inter-Regular.ttf",
    "medium": "Inter-Medium.ttf",
    "semibold": "Inter-SemiBold.ttf",
}


def dim(rgb: RGB) -> RGB:
    """A previous-week colour: 60 % of ``rgb`` mixed with 40 % surface, in integers.

    chart-spec §5 asks for a computed mix, not opacity: ``dim(ON)`` is #A0D9B7,
    ``dim(OFF)`` #DF8484, ``dim(NM_BASE)`` #ECEBE7 and ``dim(NM_INK)`` #C5C3BD.
    """
    r, g, b = rgb
    sr, sg, sb = SURFACE
    return (
        (6 * r + 4 * sr + 5) // 10,
        (6 * g + 4 * sg + 5) // 10,
        (6 * b + 4 * sb + 5) // 10,
    )


@dataclass(frozen=True)
class _Palette:
    on: RGB
    off: RGB
    nm_base: RGB
    nm_ink: RGB


_CURRENT = _Palette(ON, OFF, NM_BASE, NM_INK)
_DIMMED = _Palette(dim(ON), dim(OFF), dim(NM_BASE), dim(NM_INK))


@lru_cache(maxsize=32)
def font(face: str, size: int) -> ImageFont.FreeTypeFont:
    """The bundled Inter ``face`` ("regular", "medium" or "semibold") at ``size`` px.

    Loaded once per (face, size) by explicit path with the BASIC layout. ValueError for
    an unknown face; FileNotFoundError when the bundled file is missing (Pillow would
    otherwise go on to search the system font folders for a file of that name).
    """
    try:
        file = _FONT_FILES[face]
    except KeyError:
        raise ValueError(f"unknown font face: {face!r}") from None
    path = FONT_DIR / file
    if not path.is_file():
        raise FileNotFoundError(f"bundled font file is missing: {path}")
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)


@dataclass(frozen=True)
class Layout:
    """Where the week's parts go on the canvas (chart-spec §3), in canvas px.

    ``bar_ys`` holds the top of each of the seven bars, Monday first. ``divider_top`` is
    the top of the "last week" zone, None when no row is dimmed (Sunday).
    """

    bar_x0: float
    bar_x1: float
    date_x: float
    bar_ys: tuple[float, ...]
    divider_top: float | None
    grid_top: float
    grid_bottom: float
    axis_baseline: float

    def hx(self, us: int) -> float:
        """The x of wall-clock ``us`` microseconds after local midnight (00:00 → 24:00)."""
        return self.bar_x0 + us / DAY_US * (self.bar_x1 - self.bar_x0)


def layout(week: Week, lang: str) -> Layout:
    """The chart-spec §3 layout of ``week`` in ``lang``; widths measured with the real fonts.

    The label column fits the widest weekday plus "00.00" at SemiBold 34; the totals
    column fits the widest of the zero-outages text, the worst-case total and the column
    header. Rows sit on an 82 px pitch from y 254, with 38 px more above today's row and
    a 56 px divider zone above the first dimmed row; the hour axis is 30 px below them.
    """
    lang = resolve_language(lang)
    label = font("semibold", LABEL_SIZE)
    weekday_w = max(label.getlength(day) for day in chart_texts.WEEKDAYS[lang])
    date_x = PAD_X + weekday_w + DATE_GAP
    bar_x0 = date_x + label.getlength("00.00") + GAP
    totals_w = max(
        font("medium", TOTAL_SIZE).getlength(chart_texts.NO_OUTAGES[lang]),
        font("semibold", TOTAL_SIZE).getlength(chart_texts.worst_total(lang)),
        font("medium", HEAD_SIZE).getlength(chart_texts.TOTALS_HEADER[lang]),
    )
    bar_x1 = W - PAD_X - totals_w - GAP
    y = Y_PLOT
    bar_ys: list[float] = []
    divider_top: float | None = None
    for row in week.rows:
        if row.is_today:
            y += NOW_EXTRA
        if row.dimmed and divider_top is None:
            divider_top = y
            y += DIVIDER_H
        bar_ys.append(y + BAR_TOP)
        y += ROW_PITCH
    return Layout(
        bar_x0=bar_x0,
        bar_x1=bar_x1,
        date_x=date_x,
        bar_ys=tuple(bar_ys),
        divider_top=divider_top,
        grid_top=Y_PLOT,
        grid_bottom=y,
        axis_baseline=y + AXIS_GAP,
    )


def _x(v: float) -> int:
    """A canvas coordinate in S× pixels."""
    return round(v * S)


class _Band:
    """A full-width strip ``[top, bottom)`` of the canvas, drawn on at S× and pasted back."""

    def __init__(self, canvas: Image.Image, top: int, bottom: int) -> None:
        self.canvas = canvas
        self.top = top
        strip = canvas.crop((0, top, W, bottom))
        self.img = strip.resize((W * S, (bottom - top) * S), Image.Resampling.NEAREST)
        self.draw = ImageDraw.Draw(self.img)

    def y(self, v: float) -> int:
        """Canvas y ``v`` in this band's S× pixels."""
        return _x(v - self.top)

    def paste(self) -> None:
        self.canvas.paste(self.img.reduce(S), (0, self.top))


def _hatch(w: int, h: int, x: float, y: float, base: RGB, ink: RGB) -> Image.Image:
    """A ``w``×``h`` (S× px) tile of 45° stripes whose top-left corner is canvas (x, y).

    A canvas point (cx, cy) is on a stripe when ``(cx + cy) mod 14√2 < 6√2``, so the
    pattern is anchored to the canvas and lines up across rows and the legend swatch.
    """
    tile = Image.new("RGB", (w, h), base)
    draw = ImageDraw.Draw(tile)
    origin = x + y

    def tile_x(s: float, ty: int) -> float:
        # Where the line cx + cy = s crosses tile row ty, in S× px.
        return (s - origin) * S - ty

    k = math.floor(origin / HATCH_PERIOD) - 1
    last = origin + (w + h) / S
    while k * HATCH_PERIOD <= last:
        s0 = k * HATCH_PERIOD
        s1 = s0 + HATCH_STRIPE
        draw.polygon(
            [(tile_x(s0, 0), 0), (tile_x(s1, 0), 0), (tile_x(s1, h), h), (tile_x(s0, h), h)],
            fill=ink,
        )
        k += 1
    return tile


def _draw_bar(band: _Band, lay: Layout, row: Row, bar_y: float) -> None:
    """The row's track and segments, clipped to the rounded bar (chart-spec §6)."""
    pal = _DIMMED if row.dimmed else _CURRENT
    left = _x(lay.bar_x0)
    lw = _x(lay.bar_x1) - left
    lh = BAR_H * S
    # The empty track is no-data in every row: a dimmed row's no-data is no-data itself.
    layer = Image.new("RGB", (lw, lh), NO_DATA)
    draw = ImageDraw.Draw(layer)
    hatch: Image.Image | None = None
    # Stable sort: segments of one state keep their start order.
    for seg in sorted(row.segments, key=lambda s: _ORDER[s.state]):
        a = _x(lay.hx(seg.start_us)) - left
        b = _x(lay.hx(seg.end_us)) - left
        if b <= a:
            continue
        if seg.state == "not_monitored":
            if hatch is None:
                hatch = _hatch(lw, lh, left / S, bar_y, pal.nm_base, pal.nm_ink)
            layer.paste(hatch.crop((a, 0, b, lh)), (a, 0))
        else:
            fill = pal.off if seg.state == "off" else pal.on
            draw.rectangle((a, 0, b - 1, lh - 1), fill=fill)
    mask = Image.new("L", (lw, lh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, lw - 1, lh - 1), radius=BAR_R * S, fill=255)
    band.img.paste(layer, (left, band.y(bar_y)), mask)


def _row_text(draw: ImageDraw.ImageDraw, lay: Layout, row: Row, bar_y: float, lang: str) -> None:
    """The weekday, date and daily total of one row (chart-spec §4, §7, §8)."""
    centre = bar_y + BAR_H / 2
    if row.dimmed:
        day_ink = date_ink = INK_MUTED
    elif row.is_today:
        day_ink = date_ink = INK_PRIMARY
    else:
        day_ink, date_ink = INK_PRIMARY, INK_SECONDARY
    label_y = centre + BASELINE * LABEL_SIZE
    day_font = font("semibold" if row.is_today else "medium", LABEL_SIZE)
    date_font = font("semibold" if row.is_today else "regular", LABEL_SIZE)
    draw.text(
        (PAD_X, label_y),
        chart_texts.weekday(row.day, lang),
        font=day_font,
        fill=day_ink,
        anchor="ls",
    )
    draw.text(
        (lay.date_x, label_y),
        chart_texts.row_date(row.day),
        font=date_font,
        fill=date_ink,
        anchor="ls",
    )
    main, suffix = chart_texts.row_total(row.off_us, row.count, row.monitored, lang)
    # Dimmed rows draw every part in ink-muted; "—" is ink-muted, the zero-outages text
    # ink-secondary, a duration ink-primary with its " · N" ink-muted.
    if row.dimmed or not row.monitored:
        main_ink = INK_MUTED
    elif row.count == 0:
        main_ink = INK_SECONDARY
    else:
        main_ink = INK_PRIMARY
    total_font = font("semibold" if row.is_today else "medium", TOTAL_SIZE)
    total_y = centre + BASELINE * TOTAL_SIZE - 1
    right: float = W - PAD_X
    if suffix:
        draw.text((right, total_y), suffix, font=total_font, fill=INK_MUTED, anchor="rs")
        right -= total_font.getlength(suffix)
    draw.text((right, total_y), main, font=total_font, fill=main_ink, anchor="rs")


def render_png(week: Week, *, lang: str, name: str) -> bytes:
    """The chart of ``week`` in ``lang`` for the location ``name``, as PNG bytes.

    An unknown language falls back to en. ValueError when ``week`` does not have exactly
    seven rows.
    """
    lang = resolve_language(lang)
    if len(week.rows) != 7:
        raise ValueError(f"a week has 7 rows, not {len(week.rows)}")
    lay = layout(week, lang)
    canvas = Image.new("RGB", (W, H), SURFACE)
    for row, bar_y in zip(week.rows, lay.bar_ys, strict=True):
        top = int(bar_y) - BAR_TOP
        band = _Band(canvas, top, top + ROW_PITCH)
        _draw_bar(band, lay, row, bar_y)
        band.paste()

    draw = ImageDraw.Draw(canvas)
    draw.text(
        (PAD_X, Y_TITLE),
        chart_texts.TITLE[lang],
        font=font("semibold", TITLE_SIZE),
        fill=INK_PRIMARY,
        anchor="ls",
    )
    subtitle = f"{name}{chart_texts.DOT}{chart_texts.week_range(week.monday, week.sunday, lang)}"
    draw.text(
        (PAD_X, Y_SUB), subtitle, font=font("regular", SUB_SIZE), fill=INK_SECONDARY, anchor="ls"
    )
    for row, bar_y in zip(week.rows, lay.bar_ys, strict=True):
        _row_text(draw, lay, row, bar_y, lang)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
