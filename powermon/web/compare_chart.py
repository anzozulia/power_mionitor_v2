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
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from powermon.chart import compare, model
from powermon.i18n import chart_texts
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
