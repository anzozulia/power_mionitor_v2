"""The Compare page's presentation (quick task 261103-lfm, D-3 texts, D-5, R-7).

Pure: no Django, no database and no clock. Every function takes the numbers of
``powermon.chart.compare`` and returns frozen dataclasses holding numbers and finished,
plain-text strings. Nothing here builds markup or a class name: the templates write every
element and every class, and Django escapes every string (location names are user-typed).

Task 2a's part:

- the text helpers shared by every card: ``percent_text`` ("42%", "<1%", "—"),
  ``duration_text`` (the chart's total format in English: ``3h 20m``, ``45m``, ``<1m``),
  ``total_text`` (the chart's daily total: ``3h 20m · 2``, "no outages", "—"),
  ``gap_text`` (a signed start or end gap: "Office 4m later", "same minute") and
  ``truncate_name`` (a deterministic width estimate at 12 px, cut with "…");
- ``summary_text``: the filter card's one line, "3 locations · 25 Sep – 1 Oct 2026 (7 days)";
- ``kpi_tiles``: the KPI card (D-5 item 2). Ties go to name order (the comparison's input
  order); the longest outage ties to the earlier start. A tile that cannot be computed
  (no monitored time) shows "—"; a fleet without an outage shows "no outages";
- ``scoreboard``: the outage scoreboard (D-5 item 3), sorted by off time in P, most first,
  ties in name order, with the rank and a bar width in percent of the largest off time (a
  non-zero off time is at least 1).

Task 2b's part (D-6 to D-9, the plan's "Design: page geometry, texts and hooks"):

- the shared x-geometry of the timeline and the hours chart (``bar_x``, ``seg_span``), so
  their hour columns line up: a viewBox 960 wide (plus a 12-unit margin each side for the
  today band), the label column at x 0..64, the name column at x 72..192, the bar from
  ``BAR_X0`` 204 to ``BAR_X1`` 828 (26 units per hour) and the totals right-aligned at
  960. Coordinates are integers in tenths of a unit, written with one decimal;
- ``timeline``: the Telegram chart rebuilt for the browser (legend, top and bottom hour
  axes, 3-hour gridlines, one day group per local day with one lane per location and the
  Together lane, the today band, the now marker and pill, week dividers, segment titles,
  the totals, the 31-day cap and the "Earlier days" query, the daily-totals table);
- ``together_total``: "all off 1h 5m", "no overlap" (R-8) or "—";
- ``overlap``: the pair matrix (buckets 0..5, the zero-off row rule), the pairs and the
  simultaneous outages;
- ``hours``: the hour-of-day lanes with the cell fill rule (track, always on, off opacity);
- ``weeks``: the by-week table with its proportional bars.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlencode

from powermon.chart import compare, model
from powermon.i18n import chart_texts, times
from powermon.i18n.duration import format_total_duration

LANG = "en"
NONE_TEXT = "—"
NO_OUTAGES = chart_texts.NO_OUTAGES[LANG]
SAME_MINUTE = "same minute"
LESS_THAN_ONE = "<1%"
PARTIAL_TAG = "partial data"
# A start or end gap below one minute reads "same minute".
_MINUTE_US = 60_000_000

# truncate_name's width estimate, in tenths of a pixel at 12 px (Inter).
_NARROW = frozenset("ilj.,:;'|!")
_WIDE = frozenset("MWmwШЩЖЮФЫмшщжюфы")
_NARROW_W = 35
_WIDE_W = 100
_UPPER_W = 80
_OTHER_W = 65
ELLIPSIS = "…"

# The KPI tiles' data-metric values and labels, in the card's order.
TILE_LABELS = {
    "off-most": "Off time, most",
    "outages-most": "Outages, most",
    "longest": "Longest outage",
    "all-off": "All off together",
    "power-somewhere": "Power somewhere",
}


# Text helpers


def percent_text(p: int | None, nonzero: bool) -> str:
    """A percent: "—" for None, "<1%" for a non-zero value rounding to 0, else "N%"."""
    if p is None:
        return NONE_TEXT
    if p == 0 and nonzero:
        return LESS_THAN_ONE
    return f"{p}%"


def duration_text(us: int) -> str:
    """The chart's total duration in English: ``3h 20m``, ``45m``, ``<1m`` (never days)."""
    return format_total_duration(us, LANG)


def total_text(off_us: int, count: int, monitored: bool) -> str:
    """The chart's daily total: ``3h 20m · 2``, "no outages" or "—" (chart-spec §8)."""
    main, suffix = chart_texts.row_total(off_us, count, monitored, LANG)
    return f"{main}{suffix}"


def gap_text(gap_us: int | None, b_name: str) -> str:
    """A signed gap of B against A: "{B} 4m later", "{B} 12m earlier", "same minute", "—"."""
    if gap_us is None:
        return NONE_TEXT
    if abs(gap_us) < _MINUTE_US:
        return SAME_MINUTE
    word = "later" if gap_us > 0 else "earlier"
    return f"{b_name} {duration_text(abs(gap_us))} {word}"


def _char_width(ch: str) -> int:
    if ch in _WIDE:
        return _WIDE_W
    if ch in _NARROW:
        return _NARROW_W
    if ch.isupper() or ch.isdigit():
        return _UPPER_W
    return _OTHER_W


def truncate_name(name: str, max_units: int) -> str:
    """``name`` cut with "…" so its estimated width at 12 px fits ``max_units`` pixels.

    Deterministic (a fixed width table, no font read). A name that fits is unchanged; the
    caller keeps the full name for the ``<title>``. ValueError for a width below the
    ellipsis' own.
    """
    limit = max_units * 10
    if limit < _OTHER_W:
        raise ValueError(f"no room for a name in {max_units} units")
    if sum(_char_width(ch) for ch in name) <= limit:
        return name
    room = limit - _OTHER_W
    used = 0
    kept: list[str] = []
    for ch in name:
        width = _char_width(ch)
        if used + width > room:
            break
        used += width
        kept.append(ch)
    return "".join(kept).rstrip() + ELLIPSIS


def summary_text(count: int, first: date, last: date) -> str:
    """The filter card's line: "3 locations · 25 Sep – 1 Oct 2026 (7 days)"."""
    noun = "location" if count == 1 else "locations"
    days = (last - first).days + 1
    unit = "day" if days == 1 else "days"
    if first == last:
        span = f"{first.day} {chart_texts.MONTHS[LANG][first.month - 1]} {first.year}"
    else:
        span = chart_texts.week_range(first, last, LANG)
    return f"{count} {noun}{chart_texts.DOT}{span} ({days} {unit})"


# KPI tiles


@dataclass(frozen=True)
class Tile:
    """One KPI tile: its metric, label, value, the location it names and a short note."""

    metric: str
    label: str
    value: str
    name: str | None = None
    key: int | None = None
    note: str | None = None


def _first_max[T](items: Sequence[T], score: Mapping[int, int]) -> T | None:
    """The first item with the highest score (``score`` by index); None when empty."""
    best: T | None = None
    best_score = 0
    for i, item in enumerate(items):
        if best is None or score[i] > best_score:
            best, best_score = item, score[i]
    return best


def _local_date(result: compare.Comparison, rec: compare.OutageRec) -> date:
    return model.local_today(rec.start, result.tz)


def kpi_tiles(result: compare.Comparison) -> tuple[Tile, ...]:
    """The KPI tiles of D-5 item 2; the all-off and power-somewhere tiles for 2+ locations."""
    stats = result.locations
    monitored = any(s.monitored_us > 0 for s in stats)
    tiles: list[Tile] = []

    off = _first_max(stats, {i: s.off_us for i, s in enumerate(stats)})
    if not monitored or off is None:
        tiles.append(Tile("off-most", TILE_LABELS["off-most"], NONE_TEXT))
    elif off.off_us == 0:
        tiles.append(Tile("off-most", TILE_LABELS["off-most"], NO_OUTAGES))
    else:
        tiles.append(
            Tile("off-most", TILE_LABELS["off-most"], duration_text(off.off_us), off.name, off.key)
        )

    most = _first_max(stats, {i: s.outage_count for i, s in enumerate(stats)})
    if not monitored or most is None:
        tiles.append(Tile("outages-most", TILE_LABELS["outages-most"], NONE_TEXT))
    elif most.outage_count == 0:
        tiles.append(Tile("outages-most", TILE_LABELS["outages-most"], NO_OUTAGES))
    else:
        tiles.append(
            Tile(
                "outages-most",
                TILE_LABELS["outages-most"],
                str(most.outage_count),
                most.name,
                most.key,
            )
        )

    longest: tuple[compare.LocationStats, compare.OutageRec] | None = None
    for s in stats:
        rec = s.longest
        if rec is None:
            continue
        if (
            longest is None
            or rec.off_us > longest[1].off_us
            or (rec.off_us == longest[1].off_us and rec.start < longest[1].start)
        ):
            longest = (s, rec)
    if longest is not None:
        s, rec = longest
        tiles.append(
            Tile(
                "longest",
                TILE_LABELS["longest"],
                duration_text(rec.off_us),
                s.name,
                s.key,
                chart_texts.row_date(_local_date(result, rec)),
            )
        )
    else:
        value = NO_OUTAGES if monitored else NONE_TEXT
        tiles.append(Tile("longest", TILE_LABELS["longest"], value))

    together = result.together
    if together is not None:
        if together.jointly_monitored_us == 0:
            tiles.append(Tile("all-off", TILE_LABELS["all-off"], NONE_TEXT))
        else:
            share = percent_text(together.all_off_share, together.all_off_us > 0)
            tiles.append(
                Tile(
                    "all-off",
                    TILE_LABELS["all-off"],
                    duration_text(together.all_off_us) if together.all_off_us else NO_OUTAGES,
                    note=f"{share} of jointly monitored time",
                )
            )
        tiles.append(
            Tile(
                "power-somewhere",
                TILE_LABELS["power-somewhere"],
                percent_text(together.any_on_share, together.any_on_us > 0),
                note="of the time any location was monitored",
            )
        )
    return tuple(tiles)


# Scoreboard


@dataclass(frozen=True)
class ScoreRow:
    """One scoreboard row: the location, its rank and its pre-formatted cells."""

    rank: int
    key: int
    name: str
    status: str
    off_us: int
    off_text: str
    bar_width: int
    off_share_text: str
    outages_text: str
    avg_text: str
    longest_text: str
    days_text: str
    monitored_text: str
    partial: bool


def bar_width(off_us: int, max_off_us: int) -> int:
    """The off bar's width in percent of the largest off time; a non-zero one is >= 1."""
    if max_off_us <= 0 or off_us <= 0:
        return 0
    return max(1, 100 * off_us // max_off_us)


def _off_text(s: compare.LocationStats) -> str:
    if s.monitored_us == 0:
        return NONE_TEXT
    if s.off_us == 0:
        return NO_OUTAGES
    return duration_text(s.off_us)


def scoreboard(result: compare.Comparison, statuses: Mapping[int, str]) -> tuple[ScoreRow, ...]:
    """One row per location, most off time first, ties in name order; ranks from 1.

    ``statuses`` maps each location id to its live status key (the LED); a missing one
    reads "waiting".
    """
    ordered = sorted(result.locations, key=lambda s: -s.off_us)
    max_off = max((s.off_us for s in ordered), default=0)
    rows: list[ScoreRow] = []
    for rank, s in enumerate(ordered, start=1):
        longest = (
            NONE_TEXT
            if s.longest is None
            else f"{duration_text(s.longest.off_us)}{chart_texts.DOT}"
            f"{chart_texts.row_date(_local_date(result, s.longest))}"
        )
        rows.append(
            ScoreRow(
                rank=rank,
                key=s.key,
                name=s.name,
                status=statuses.get(s.key, "waiting"),
                off_us=s.off_us,
                off_text=_off_text(s),
                bar_width=bar_width(s.off_us, max_off),
                off_share_text=percent_text(s.off_share, s.off_us > 0),
                outages_text=str(s.outage_count),
                avg_text=NONE_TEXT if s.avg_outage_us is None else duration_text(s.avg_outage_us),
                longest_text=longest,
                days_text=f"{s.days_with_outages} of {s.days_total}",
                monitored_text=percent_text(s.coverage, s.monitored_us > 0),
                partial=s.partial_data,
            )
        )
    return tuple(rows)


# Shared x-geometry (Task 2b): integers in tenths of a viewBox unit

VIEW_W = 960
VIEW_PAD = 12
LABEL_X = 0
NAME_X = 72
NAME_W = 120
BAR_X0 = 204
BAR_X1 = 828
BAR_W = BAR_X1 - BAR_X0
TOTAL_X = VIEW_W
LANE_H = 12
LANE_PITCH = 18
LANE_RADIUS = 4
GROUP_PAD = 8
TODAY_EXTRA = 24
WEEK_ZONE = 22
LEGEND_H = 28
AXIS_H = 20
HOUR_LANE_H = 20
HOUR_PITCH = 26
HOUR_NAME_W = NAME_X + NAME_W
# The chart's minimum OFF width, 8 px of ~30 px per hour (16 of 1440 minutes), in tenths.
MIN_OFF_TENTHS = BAR_W * 10 * 16 // 1440
_DAY_US = 24 * model.HOUR_US
_BAR_T = BAR_W * 10
_HOUR_T = _BAR_T // 24
# A lane's text baseline below the lane top (12 px text centred on the 12-unit lane).
_TEXT_DY = 10
# The now pill (chart-spec §7 scaled): 18 high, fully rounded, 2 above the line's top.
_PILL_H = 18
_PILL_W = 46
_NOW_OVERHANG = 6
TOGETHER = "together"
TOGETHER_NAME = "Together"
TOGETHER_HOURS_NAME = "Together (all off)"
EARLIER_LABEL = "Earlier days"
TIMELINE_CAP = f"The timeline shows the last {compare.TIMELINE_DAYS} days of the period."
TOTALS_HEADER = chart_texts.TOTALS_HEADER[LANG]
STATE_WORDS = {
    "on": "Power on",
    "off": "Power off",
    "not_monitored": "Not monitored",
    "all_off": "All off",
    "some_off": "Some off",
    "all_on": "All on",
}
# Draw order (chart-spec §6): on, then not monitored, then off, so OFF is always on top.
_DRAW_ORDER = {"on": 0, "all_on": 0, "not_monitored": 1, "some_off": 1, "off": 2, "all_off": 2}
_MINIMUM_STATES = frozenset({"off", "all_off"})


def _half_up(num: int, den: int) -> int:
    return (2 * num + den) // (2 * den)


def tenths(value: int) -> str:
    """A coordinate in tenths written with one decimal: 2040 -> "204.0", -5 -> "-0.5"."""
    sign = "-" if value < 0 else ""
    whole, part = divmod(abs(value), 10)
    return f"{sign}{whole}.{part}"


def _bar_t(wall_us: int) -> int:
    """A wall-clock time of day (0..24 h in µs) as tenths from the bar's start."""
    return _half_up(min(max(wall_us, 0), _DAY_US) * _BAR_T, _DAY_US)


def bar_x(wall_us: int) -> str:
    """The absolute x of a wall-clock time: 00:00 is BAR_X0, 24:00 is BAR_X1."""
    return tenths(BAR_X0 * 10 + _bar_t(wall_us))


def seg_span(start_us: int, end_us: int, *, minimum: bool) -> tuple[str, str]:
    """A segment's (x, width) relative to the bar start, one decimal each.

    With ``minimum`` (OFF), a segment narrower than the chart's minimum width is widened
    around its midpoint and shifted inside the bar (chart-spec §6). ValueError for an
    empty or reversed span.
    """
    if end_us <= start_us:
        raise ValueError(f"an empty segment: {start_us}..{end_us}")
    x0, x1 = _bar_t(start_us), _bar_t(end_us)
    if minimum and x1 - x0 < MIN_OFF_TENTHS:
        x0 = min(max((x0 + x1 - MIN_OFF_TENTHS) // 2, 0), _BAR_T - MIN_OFF_TENTHS)
        x1 = x0 + MIN_OFF_TENTHS
    return tenths(x0), tenths(x1 - x0)


def text_width(value: str) -> int:
    """The estimated width of 12 px text in tenths (truncate_name's table)."""
    return sum(_char_width(ch) for ch in value)


def wall_hm(wall_us: int) -> str:
    """A wall-clock time of day as ``HH:MM``; the day's end is "24:00"."""
    hours, rest = divmod(wall_us, model.HOUR_US)
    return f"{hours:02d}:{rest // _MINUTE_US:02d}"


def segment_title(name: str, state: str, start_us: int, end_us: int) -> str:
    """ "Home · Power off · 12:00–13:00 · 1h" (wall-clock times, the duration of the span)."""
    return (
        f"{name}{chart_texts.DOT}{STATE_WORDS[state]}{chart_texts.DOT}"
        f"{wall_hm(start_us)}–{wall_hm(end_us)}{chart_texts.DOT}{duration_text(end_us - start_us)}"
    )


def together_total(all_off_us: int, joint_us: int) -> str:
    """The Together lane's total: "all off 1h 5m", "no overlap" (R-8) or "—"."""
    if all_off_us > 0:
        return f"all off {duration_text(all_off_us)}"
    if joint_us > 0:
        return "no overlap"
    return NONE_TEXT


def earlier_query(keys: Sequence[int], earlier: tuple[date, date]) -> str:
    """The "Earlier days" query: the same locations, the custom period ``earlier``."""
    pairs = [("loc", str(key)) for key in keys]
    pairs += [
        ("range", "custom"),
        ("from", earlier[0].isoformat()),
        ("to", earlier[1].isoformat()),
    ]
    return urlencode(pairs)


# Timeline (D-6)


@dataclass(frozen=True)
class Seg:
    """One drawn segment: its state, its x and width inside the lane and its title."""

    state: str
    x: str
    width: str
    start_us: int
    end_us: int
    title: str


@dataclass(frozen=True)
class Lane:
    """One lane of a day group: a location, or Together (``key`` "together")."""

    key: str
    name: str
    short: str
    together: bool
    y: str
    text_y: str
    transform: str
    segments: tuple[Seg, ...]
    total: str
    total_main: str
    total_suffix: str
    tone: str


@dataclass(frozen=True)
class Divider:
    """A week divider above a Monday group: a caption and a hairline."""

    caption: str
    text_y: str
    line_y: str
    line_x0: str


@dataclass(frozen=True)
class NowMarker:
    """The now line and pill of today's group (chart-spec §7)."""

    text: str
    x: str
    y1: str
    y2: str
    pill_x: str
    pill_y: str
    pill_cx: str
    text_y: str


@dataclass(frozen=True)
class DayGroup:
    """One local day of the shown window: its label, band, lanes and markers."""

    day: str
    weekday: str
    date_text: str
    label_y: str
    is_today: bool
    top: str
    height: str
    lanes: tuple[Lane, ...]
    divider: Divider | None
    separator_y: str | None
    now: NowMarker | None


@dataclass(frozen=True)
class LegendItem:
    kind: str
    label: str
    x: str
    text_x: str


@dataclass(frozen=True)
class AxisLabel:
    x: str
    text: str


@dataclass(frozen=True)
class Separator:
    """An hour-cell separator inside a lane: x (relative) and its opacity."""

    x: str
    opacity: str


@dataclass(frozen=True)
class DailyRow:
    day: str
    label: str
    cells: tuple[str, ...]


@dataclass(frozen=True)
class Timeline:
    """Everything the timeline card draws (D-6)."""

    view_box: str
    width: int
    height: int
    title: str
    desc: str
    legend: tuple[LegendItem, ...]
    axis: tuple[AxisLabel, ...]
    grid: tuple[str, ...]
    grid_y1: str
    grid_y2: str
    top_axis_y: str
    bottom_axis_y: str
    separators: tuple[Separator, ...]
    groups: tuple[DayGroup, ...]
    capped: bool
    cap_text: str
    earlier_query: str | None
    columns: tuple[str, ...]
    daily: tuple[DailyRow, ...]


def _separators() -> tuple[Separator, ...]:
    """The 23 hour-cell separators, stronger every 3 hours (chart-spec §6)."""
    return tuple(
        Separator(tenths(h * _HOUR_T - 5), "0.90" if h % 3 == 0 else "0.55") for h in range(1, 24)
    )


def axis_labels() -> tuple[AxisLabel, ...]:
    """The hour axis, "00 03 … 24", one label on each 3-hour gridline."""
    return tuple(AxisLabel(bar_x(h * model.HOUR_US), f"{h:02d}") for h in range(0, 25, 3))


def _gridlines() -> tuple[str, ...]:
    return tuple(bar_x(h * model.HOUR_US) for h in range(0, 25, 3))


def _legend(several: bool) -> tuple[LegendItem, ...]:
    kinds = ("on", "off", "not_monitored", "no_data")
    labels = list(zip(kinds, chart_texts.LEGEND[LANG], strict=True))
    if several:
        labels.append(("some_off", "Some off (Together)"))
    items: list[LegendItem] = []
    x = 0
    for kind, label in labels:
        items.append(LegendItem(kind, label, tenths(x), tenths(x + 260)))
        x += 260 + text_width(label) + 200
    return tuple(items)


def _segments(name: str, segments: Sequence[tuple[str, int, int]]) -> tuple[Seg, ...]:
    """Drawn segments in the chart's order (on, not monitored, off); ``partial`` never."""
    ordered = sorted(
        (s for s in segments if s[0] in _DRAW_ORDER and s[2] > s[1]),
        key=lambda s: (_DRAW_ORDER[s[0]], s[1], s[2]),
    )
    out: list[Seg] = []
    for state, start, end in ordered:
        x, width = seg_span(start, end, minimum=state in _MINIMUM_STATES)
        out.append(Seg(state, x, width, start, end, segment_title(name, state, start, end)))
    return tuple(out)


def _lane_total(row: model.Row) -> tuple[str, str, str]:
    main, suffix = chart_texts.row_total(row.off_us, row.count, row.monitored, LANG)
    if not row.monitored:
        return main, suffix, "subtle"
    if row.count == 0:
        return main, suffix, "muted"
    return main, suffix, "strong"


def _together_total_parts(lane: compare.TogetherLane) -> tuple[str, str]:
    text = together_total(lane.all_off_us, lane.joint_us)
    if lane.all_off_us > 0:
        return text, "strong"
    return text, "muted" if lane.joint_us > 0 else "subtle"


def _lane(
    key: str,
    name: str,
    *,
    together: bool,
    top: int,
    segments: Sequence[tuple[str, int, int]],
    total: tuple[str, str, str],
) -> Lane:
    main, suffix, tone = total
    return Lane(
        key=key,
        name=name,
        short=truncate_name(name, NAME_W),
        together=together,
        y=str(top),
        text_y=str(top + _TEXT_DY),
        transform=f"translate({BAR_X0} {top})",
        segments=_segments(TOGETHER_NAME if together else name, segments),
        total=f"{main}{suffix}",
        total_main=main,
        total_suffix=suffix,
        tone=tone,
    )


def _now_marker(result: compare.Comparison, first_top: int, last_bottom: int) -> NowMarker:
    wall = model.wall_us(result.now, result.tz, end=False)
    x_t = BAR_X0 * 10 + _bar_t(wall)
    y1 = first_top - _NOW_OVERHANG
    half = _PILL_W * 10 // 2
    cx = min(max(x_t, (BAR_X0 - 8) * 10 + half), (BAR_X1 + 8) * 10 - half)
    pill_y = y1 - 2 - _PILL_H
    return NowMarker(
        text=times.hm(result.now, result.tz),
        x=tenths(x_t),
        y1=str(y1),
        y2=str(last_bottom + _NOW_OVERHANG),
        pill_x=tenths(cx - half),
        pill_y=str(pill_y),
        pill_cx=tenths(cx),
        text_y=str(pill_y + 13),
    )


def timeline(result: compare.Comparison) -> Timeline:
    """The timeline card's view model: the shown window's day groups, oldest at the top."""
    stats = result.locations
    several = len(stats) > 1
    names = [s.name for s in stats]
    plot_top = LEGEND_H + AXIS_H
    y = plot_top
    groups: list[DayGroup] = []
    daily: list[DailyRow] = []
    for k, day in enumerate(result.days):
        divider = None
        separator = None
        if k > 0 and day.day.weekday() == 0:
            sunday = day.day.fromordinal(day.day.toordinal() + 6)
            caption = f"{chart_texts.row_date(day.day)}{chart_texts.DASH}"
            caption += chart_texts.row_date(sunday)
            divider = Divider(
                caption=caption,
                text_y=str(y + 15),
                line_y=str(y + 11),
                line_x0=tenths(text_width(caption) + 80),
            )
            y += WEEK_ZONE
        elif k > 0:
            separator = str(y)
        if day.is_today:
            y += TODAY_EXTRA
        top = y
        lanes: list[Lane] = []
        for i, (s, row) in enumerate(zip(stats, day.lanes, strict=True)):
            lane_top = top + GROUP_PAD + i * LANE_PITCH + (LANE_PITCH - LANE_H) // 2
            segs = [(seg.state, seg.start_us, seg.end_us) for seg in row.segments]
            lanes.append(
                _lane(
                    str(s.key),
                    s.name,
                    together=False,
                    top=lane_top,
                    segments=segs,
                    total=_lane_total(row),
                )
            )
        if day.together is not None:
            lane_top = top + GROUP_PAD + len(stats) * LANE_PITCH + (LANE_PITCH - LANE_H) // 2
            text, tone = _together_total_parts(day.together)
            segs = [(seg.state, seg.start_us, seg.end_us) for seg in day.together.segments]
            lanes.append(
                _lane(
                    TOGETHER,
                    TOGETHER_NAME,
                    together=True,
                    top=lane_top,
                    segments=segs,
                    total=(text, "", tone),
                )
            )
        height = 2 * GROUP_PAD + len(lanes) * LANE_PITCH
        now = None
        if day.is_today and result.period.end == result.now:
            first_top = int(lanes[0].y)
            last_bottom = int(lanes[-1].y) + LANE_H
            now = _now_marker(result, first_top, last_bottom)
        groups.append(
            DayGroup(
                day=day.day.isoformat(),
                weekday=chart_texts.weekday(day.day, LANG),
                date_text=chart_texts.row_date(day.day),
                label_y=lanes[0].text_y,
                is_today=day.is_today,
                top=str(top),
                height=str(height),
                lanes=tuple(lanes),
                divider=divider,
                separator_y=separator,
                now=now,
            )
        )
        daily.append(
            DailyRow(
                day.day.isoformat(),
                chart_texts.weekday_date(day.day, LANG),
                tuple(lane.total for lane in lanes),
            )
        )
        y = top + height
    plot_bottom = y
    height = plot_bottom + AXIS_H + 4
    shown = (result.days[0].day, result.days[-1].day) if result.days else None
    span = (
        f"{chart_texts.row_date(shown[0])}{chart_texts.DASH}{chart_texts.row_date(shown[1])}"
        if shown
        else NONE_TEXT
    )
    earlier = (
        earlier_query([s.key for s in stats], result.earlier)
        if result.earlier is not None
        else None
    )
    desc = (
        f"One group of lanes per day from {span}, one lane per location"
        f"{' and one for all of them together' if several else ''}, from 00:00 to 24:00 "
        "local time: green is power on, red power off, hatched not monitored, grey no data."
    )
    return Timeline(
        view_box=f"-{VIEW_PAD} 0 {VIEW_W + 2 * VIEW_PAD} {height}",
        width=VIEW_W + 2 * VIEW_PAD,
        height=height,
        title=f"Timeline of power on and off, {span}",
        desc=desc,
        legend=_legend(several),
        axis=axis_labels(),
        grid=_gridlines(),
        grid_y1=str(plot_top),
        grid_y2=str(plot_bottom),
        top_axis_y=str(LEGEND_H + 14),
        bottom_axis_y=str(plot_bottom + 16),
        separators=_separators(),
        groups=tuple(groups),
        capped=result.earlier is not None,
        cap_text=TIMELINE_CAP,
        earlier_query=earlier,
        columns=(*names, *([TOGETHER_NAME] if several else [])),
        daily=tuple(daily),
    )


# Overlap (D-7)


def matrix_bucket(num: int, den: int) -> int:
    """The matrix cell's tone bucket: 0 for no share, else <10, 10-25, 25-50, 50-75, >=75."""
    if den <= 0 or num <= 0:
        return 0
    for bucket, edge in enumerate((10, 25, 50, 75), start=1):
        if 100 * num < edge * den:
            return bucket
    return 5


@dataclass(frozen=True)
class MatrixCell:
    col: int
    diagonal: bool
    share: str
    bucket: int
    value: str
    detail: str
    sr: str


@dataclass(frozen=True)
class MatrixRow:
    key: int
    name: str
    short: str
    cells: tuple[MatrixCell, ...]


@dataclass(frozen=True)
class MatrixHead:
    key: int
    name: str
    short: str


@dataclass(frozen=True)
class PairRow:
    a: int
    b: int
    label: str
    both: str
    share_a: str
    share_b: str
    coinciding: str
    start_gap: str
    end_gap: str


@dataclass(frozen=True)
class SimRow:
    start: str
    end: str
    duration: str
    names: str
    all_n: bool
    all_text: str


@dataclass(frozen=True)
class Overlap:
    """The overlap card (D-7); ``single`` with one location (nothing else is shown)."""

    single: bool
    heads: tuple[MatrixHead, ...]
    matrix: tuple[MatrixRow, ...]
    pairs: tuple[PairRow, ...]
    never: str | None
    simultaneous: tuple[SimRow, ...]
    sim_cap: str | None


def _pair_of(
    pairs: Mapping[tuple[int, int], compare.PairStats], a: int, b: int
) -> tuple[compare.PairStats, bool]:
    """The pair of ``a`` and ``b`` and whether ``a`` is its first location."""
    if (a, b) in pairs:
        return pairs[(a, b)], True
    return pairs[(b, a)], False


def _matrix_cell(
    a: compare.LocationStats,
    b: compare.LocationStats,
    pairs: Mapping[tuple[int, int], compare.PairStats],
) -> MatrixCell:
    if a.key == b.key:
        value = _off_text(a)
        sr = (
            f"{a.name} was off {duration_text(a.off_us)} in total."
            if a.off_us
            else f"{a.name} had no off time."
        )
        return MatrixCell(a.key, True, "", 0, value, "", sr)
    pair, first = _pair_of(pairs, a.key, b.key)
    both = pair.both_off_us
    if a.off_us == 0:
        sr = f"{a.name} had no off time, so there is no share with {b.name}."
        return MatrixCell(b.key, False, "", 0, NONE_TEXT, "", sr)
    share = pair.share_a if first else pair.share_b
    value = percent_text(share, both > 0)
    detail = duration_text(both) if both else NO_OUTAGES
    sr = (
        f"While {a.name} was off, {b.name} was also off {value} of the time"
        f" ({duration_text(both) if both else 'never'})."
    )
    return MatrixCell(
        b.key,
        False,
        "" if share is None else str(share),
        matrix_bucket(both, a.off_us),
        value,
        detail,
        sr,
    )


def _local_stamp(result: compare.Comparison, at: datetime) -> str:
    day = chart_texts.weekday_date(model.local_today(at, result.tz), LANG)
    return f"{day} {times.hm(at, result.tz)}"


def overlap(result: compare.Comparison) -> Overlap:
    """The overlap card's view model (D-7)."""
    stats = result.locations
    if len(stats) < 2:
        return Overlap(True, (), (), (), None, (), None)
    by_key = {s.key: s for s in stats}
    pairs = {(p.a, p.b): p for p in result.pairs}
    heads = tuple(MatrixHead(s.key, s.name, truncate_name(s.name, NAME_W)) for s in stats)
    matrix = tuple(
        MatrixRow(
            a.key,
            a.name,
            truncate_name(a.name, NAME_W),
            tuple(_matrix_cell(a, b, pairs) for b in stats),
        )
        for a in stats
    )
    shown = sorted((p for p in result.pairs if p.both_off_us > 0), key=lambda p: -p.both_off_us)
    rows = []
    for p in shown:
        a, b = by_key[p.a], by_key[p.b]
        rows.append(
            PairRow(
                a=p.a,
                b=p.b,
                label=f"{a.name} ↔ {b.name}",
                both=duration_text(p.both_off_us),
                share_a=percent_text(p.share_a, True),
                share_b=percent_text(p.share_b, True),
                coinciding=(
                    f"{p.coinciding_a} of {a.name}'s {p.outages_a}{chart_texts.DOT}"
                    f"{p.coinciding_b} of {b.name}'s {p.outages_b}"
                ),
                start_gap=gap_text(p.start_gap_us, b.name),
                end_gap=gap_text(p.end_gap_us, b.name),
            )
        )
    left = len(result.pairs) - len(rows)
    never = None
    if left == 1:
        never = "1 pair never was off at the same time."
    elif left > 1:
        never = f"{left} pairs never were off at the same time."
    n = len(stats)
    sims = tuple(
        SimRow(
            start=_local_stamp(result, sim.start),
            end="ongoing" if sim.end is None else _local_stamp(result, sim.end),
            duration=duration_text(sim.duration_us),
            names=", ".join(by_key[key].name for key in sim.keys),
            all_n=sim.all_n,
            all_text=f"all {n}",
        )
        for sim in result.simultaneous
    )
    cap = None
    if result.simultaneous_total > len(sims):
        cap = f"Showing the latest {len(sims)} of {result.simultaneous_total}."
    return Overlap(False, heads, matrix, tuple(rows), never, sims, cap)


# Hours (D-8)


@dataclass(frozen=True)
class HourCellView:
    hour: int
    x: str
    text_x: str
    fill: str
    opacity: str
    pct: str
    label: str
    title: str


@dataclass(frozen=True)
class HourLaneView:
    key: str
    name: str
    short: str
    together: bool
    y: str
    text_y: str
    transform: str
    cells: tuple[HourCellView, ...]
    peak: str
    tone: str


@dataclass(frozen=True)
class Hours:
    view_box: str
    width: int
    height: int
    title: str
    axis: tuple[AxisLabel, ...]
    axis_y: str
    grid: tuple[str, ...]
    grid_y1: str
    grid_y2: str
    separators: tuple[Separator, ...]
    lanes: tuple[HourLaneView, ...]


def hour_fill(off_us: int, monitored_us: int) -> tuple[str, str]:
    """A cell's (fill, opacity): "track" with no monitored time, "on" at 0.35 when the power
    was always on, else "off" at (20 + 80 × share) %, two decimals (INFO-7)."""
    if monitored_us <= 0:
        return "track", "1"
    if off_us <= 0:
        return "on", "0.35"
    level = 20 + 80 * min(off_us, monitored_us) // monitored_us
    return "off", f"{level // 100}.{level % 100:02d}"


def _hour_label(h: int) -> str:
    return f"{h:02d}:00–{h + 1:02d}:00"


def _hour_title(name: str, h: int, cell: compare.HourCell) -> str:
    head = f"{name}{chart_texts.DOT}{_hour_label(h)}{chart_texts.DOT}"
    if cell.monitored_us == 0:
        return f"{head}not monitored"
    pct = percent_text(cell.value, cell.off_us > 0)
    return (
        f"{head}off {pct} of monitored time "
        f"({duration_text(cell.off_us) if cell.off_us else '0m'} of "
        f"{duration_text(cell.monitored_us)})"
    )


def _peak(cells: Sequence[compare.HourCell]) -> tuple[str, str]:
    if not any(c.monitored_us for c in cells):
        return NONE_TEXT, "subtle"
    best: int | None = None
    for h, c in enumerate(cells):
        if c.monitored_us == 0 or c.off_us == 0:
            continue
        if (
            best is None
            or c.off_us * cells[best].monitored_us > cells[best].off_us * c.monitored_us
        ):
            best = h
    if best is None:
        return NO_OUTAGES, "muted"
    cell = cells[best]
    return (
        f"peak {best:02d}–{best + 1:02d}{chart_texts.DOT}{percent_text(cell.value, True)}",
        "strong",
    )


def hours(result: compare.Comparison) -> Hours:
    """The hour-of-day chart's view model: one lane per location, then Together (D-8)."""
    names = {s.key: s.name for s in result.locations}
    top = AXIS_H + 4
    lanes: list[HourLaneView] = []
    for i, lane in enumerate(result.hours):
        together = lane.key is None
        name = TOGETHER_HOURS_NAME if lane.key is None else names[lane.key]
        lane_top = top + i * HOUR_PITCH
        cells = []
        for h, cell in enumerate(lane.cells):
            fill, opacity = hour_fill(cell.off_us, cell.monitored_us)
            value = cell.value
            show = value is not None and value >= 10
            cells.append(
                HourCellView(
                    hour=h,
                    x=tenths(h * _HOUR_T),
                    text_x=tenths(h * _HOUR_T + _HOUR_T // 2),
                    fill=fill,
                    opacity=opacity,
                    pct="" if value is None else str(value),
                    label=str(value) if show else "",
                    title=_hour_title(name, h, cell),
                )
            )
        peak, tone = _peak(lane.cells)
        lanes.append(
            HourLaneView(
                key=TOGETHER if together else str(lane.key),
                name=name,
                short=truncate_name(name, HOUR_NAME_W),
                together=together,
                y=str(lane_top),
                text_y=str(lane_top + 14),
                transform=f"translate({BAR_X0} {lane_top})",
                cells=tuple(cells),
                peak=peak,
                tone=tone,
            )
        )
    bottom = top + len(lanes) * HOUR_PITCH - (HOUR_PITCH - HOUR_LANE_H)
    height = bottom + 6
    return Hours(
        view_box=f"-{VIEW_PAD} 0 {VIEW_W + 2 * VIEW_PAD} {height}",
        width=VIEW_W + 2 * VIEW_PAD,
        height=height,
        title="Share of monitored time with the power off, by local hour of day",
        axis=axis_labels(),
        axis_y="14",
        grid=_gridlines(),
        grid_y1=str(AXIS_H),
        grid_y2=str(bottom + 2),
        separators=_separators(),
        lanes=tuple(lanes),
    )


# Weeks (D-9)


@dataclass(frozen=True)
class WeekCellView:
    key: str
    off_us: int
    text: str
    tone: str
    bar: int


@dataclass(frozen=True)
class WeekRowView:
    monday: str
    label: str
    partial: bool
    cells: tuple[WeekCellView, ...]


@dataclass(frozen=True)
class Weeks:
    columns: tuple[str, ...]
    rows: tuple[WeekRowView, ...]


def _week_tone(cell: compare.WeekCell) -> str:
    if not cell.monitored:
        return "subtle"
    return "muted" if cell.count == 0 else "strong"


def weeks(result: compare.Comparison) -> Weeks:
    """The by-week table (D-9): newest week first, bars scaled to the table's largest cell."""
    keys = [str(s.key) for s in result.locations]
    several = len(keys) > 1
    largest = 0
    for row in result.weeks:
        for cell in (*row.cells, *([row.together] if row.together else [])):
            largest = max(largest, cell.off_us)
    rows = []
    for row in result.weeks:
        pairs = list(zip(keys, row.cells, strict=True))
        if row.together is not None:
            pairs.append((TOGETHER, row.together))
        rows.append(
            WeekRowView(
                monday=row.monday.isoformat(),
                label=(
                    f"{chart_texts.row_date(row.monday)}{chart_texts.DASH}"
                    f"{chart_texts.row_date(row.sunday)}"
                ),
                partial=row.partial,
                cells=tuple(
                    WeekCellView(
                        key=key,
                        off_us=cell.off_us,
                        text=total_text(cell.off_us, cell.count, cell.monitored),
                        tone=_week_tone(cell),
                        bar=bar_width(cell.off_us, largest),
                    )
                    for key, cell in pairs
                ),
            )
        )
    columns = (*[s.name for s in result.locations], *(["All off together"] if several else []))
    return Weeks(columns, tuple(rows))
