"""The admin's location comparison: totals, overlaps, day lanes, hours and weeks (quick task
261103-lfm, D-3, D-4, D-6, D-8, D-9).

PURE: stdlib and ``powermon.chart.model`` only. No Django, no clock read, no float and no
true division: every duration is integer microseconds. Every input is explicit (the pieces
and whole outages of each location, the period, ``now`` and the display time zone), so the
web page and the tests share one definition and the tests need no database.

Rules:
- The period P runs from the local midnight of its first date to the earlier of ``now``
  and the local midnight after its last date (``model.day_bounds``, DST-safe, never
  ``+ 24 h``). Its days are the local dates first..last.
- Per location (D-3): on, off and not-monitored time clipped to P in real elapsed time (an
  open piece runs to now); no data = |P| minus the three. Not-monitored time is never off
  and never an outage (INV-04, KD3). The outages in P are the distinct outage starts of
  the off pieces overlapping P, so an outage split by not-monitored time counts once
  (INV-11). Average, median and longest use each outage's WHOLE off time (``OutageRec``,
  the same number as ``history.group_outages``), also outside P.
- Together (D-4): at each instant a location is on, off or unknown (not monitored or no
  data). The state is ``all_off`` (every location off), ``some_off`` (at least one off,
  not all), ``all_on`` (every location on) or ``partial`` (none off, at least one unknown).
- Pairs (D-4): both-off time, asymmetric shares, coinciding outages and the signed median
  start and end gaps over every overlapping outage pair.
- Simultaneous outages (D-4): the maximal spans of P in which at least two locations are
  off at once, newest first, at most ``SIM_CAP``. A span cut by P's start or end is flagged
  (``starts_before``, ``ends_after``) so the page does not show P's edge as its real start
  or end.
- Day lanes (D-6): exactly the Telegram chart's day slicing (``model._row``'s algorithm on
  ``model.day_bounds``, ``model.transitions`` and ``model.wall_us``): positions follow the
  local wall clock, totals follow real time (INV-08), a day is local midnight to the next
  (INV-03), the fall-back day's first repeated hour is not drawn and the spring-forward
  hour stays empty. Each day's frame is computed once and shared by every lane. The
  timeline shows up to 31 days at a time (``timeline_window``); Earlier/Later days page
  through the period without changing any other number.
- Hours (D-8): wall-clock off and monitored time per local hour, summed over the drawn day
  segments of every day of P. Weeks (D-9): Monday-to-Sunday weeks overlapping P, newest
  first, in real time.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from operator import attrgetter

from powermon.chart import model

TIMELINE_DAYS = 31
SIM_CAP = 100
PARTIAL_COVERAGE_PCT = 90
EARLIEST = date(2026, 1, 1)
MAX_SPAN_DAYS = 366
PRESETS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90}
TOGETHER_STATES = ("all_off", "some_off", "all_on", "partial")

HOURS = 24
_ONE_US = model.ONE_US
_HOUR_US = model.HOUR_US


def _utc(t: datetime) -> datetime:
    if t.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return t.astimezone(UTC)


def _us(a: datetime, b: datetime) -> int:
    return (b - a) // _ONE_US


# Inputs


@dataclass(frozen=True)
class Period:
    """The compared period: the local dates ``first..last`` and its UTC span ``[start, end)``."""

    first: date
    last: date
    start: datetime
    end: datetime

    @property
    def days(self) -> tuple[date, ...]:
        n = (self.last - self.first).days + 1
        return tuple(self.first + timedelta(days=i) for i in range(n))

    @property
    def length_us(self) -> int:
        return _us(self.start, self.end)


def period_of(first: date, last: date, now: datetime, tz: str) -> Period:
    """P from the local midnight of ``first`` to ``min(now, local midnight after last)``.

    The end is never before the start. ValueError for ``last < first`` or a naive ``now``.
    """
    now = _utc(now)
    if last < first:
        raise ValueError("the period must not end before it starts")
    start = model.day_bounds(first, tz)[0]
    end = max(start, min(now, model.next_midnight(last, tz)))
    return Period(first=first, last=last, start=start, end=end)


def preset_dates(key: str, today: date) -> tuple[date, date]:
    """The dates of a preset range: ``n`` days ending ``today``. ValueError for an unknown key."""
    if key not in PRESETS:
        raise ValueError(f"unknown range preset: {key!r}")
    return today - timedelta(days=PRESETS[key] - 1), today


@dataclass(frozen=True)
class TimelineWindow:
    """The dates the timeline shows, ``first..last``, and the ``tl`` of its neighbours.

    ``earlier`` is the last date of the window before (None when the window starts at the
    period's first date); ``later`` is the last date of the window after (None when the
    window ends at the period's last date).
    """

    first: date
    last: date
    earlier: date | None
    later: date | None


def timeline_window(
    first: date, last: date, end: date | None = None, days: int = TIMELINE_DAYS
) -> TimelineWindow:
    """The timeline's window of the period ``first..last``: up to ``days`` dates ending at
    ``end`` (default ``last``), never before ``first``.

    ``end`` is navigation, not a filter: a date outside the period is clamped into it.
    "Earlier" ends the day before the window (its start is clamped at ``first`` in turn);
    "later" is the window's next ``days`` dates, clamped at ``last``. ValueError for
    ``last < first`` or ``days < 1``.
    """
    if last < first:
        raise ValueError("the period must not end before it starts")
    if days < 1:
        raise ValueError(f"a timeline window needs at least one day, not {days}")
    shown_last = min(max(end if end is not None else last, first), last)
    shown_first = max(first, shown_last - timedelta(days=days - 1))
    earlier = shown_first - timedelta(days=1) if shown_first > first else None
    later = min(last, shown_last + timedelta(days=days)) if shown_last < last else None
    return TimelineWindow(shown_first, shown_last, earlier, later)


def percent(num: int, den: int) -> int | None:
    """``num / den`` as an integer percent rounded half up; None when ``den`` is 0."""
    if den == 0:
        return None
    return (200 * num + den) // (2 * den)


@dataclass(frozen=True)
class OutageRec:
    """One whole outage: its start, its end (None while in progress) and its off time."""

    start: datetime
    end: datetime | None
    off_us: int


@dataclass(frozen=True)
class Series:
    """One location's input: its pieces overlapping P (start order) and its whole outages.

    ``outages`` holds every outage that has an off piece overlapping P, keyed by ``start``.
    An off piece in P whose outage is missing is a ValueError in ``compare``.
    """

    key: int
    name: str
    pieces: tuple[model.Piece, ...]
    outages: tuple[OutageRec, ...]


# Outputs


@dataclass(frozen=True)
class LocationStats:
    key: int
    name: str
    on_us: int
    off_us: int
    nm_us: int
    nodata_us: int
    period_us: int
    outage_count: int
    avg_outage_us: int | None
    median_outage_us: int | None
    longest: OutageRec | None
    days_with_outages: int
    days_total: int
    days_monitored: int
    off_per_day_us: int | None

    @property
    def monitored_us(self) -> int:
        return self.on_us + self.off_us

    @property
    def off_share(self) -> int | None:
        return percent(self.off_us, self.monitored_us)

    @property
    def coverage(self) -> int | None:
        return percent(self.monitored_us, self.period_us)

    @property
    def partial_data(self) -> bool:
        return self.monitored_us * 100 < PARTIAL_COVERAGE_PCT * self.period_us


@dataclass(frozen=True)
class TogetherSpan:
    """A maximal UTC span of P with one Together state; ``joint``: every location known."""

    state: str
    start: datetime
    end: datetime
    joint: bool


@dataclass(frozen=True)
class TogetherStats:
    all_off_us: int
    any_on_us: int
    jointly_monitored_us: int
    known_us: int

    @property
    def all_off_share(self) -> int | None:
        return percent(self.all_off_us, self.jointly_monitored_us)

    @property
    def any_on_share(self) -> int | None:
        return percent(self.any_on_us, self.known_us)


@dataclass(frozen=True)
class PairStats:
    a: int
    b: int
    both_off_us: int
    share_a: int | None
    share_b: int | None
    coinciding_a: int
    outages_a: int
    coinciding_b: int
    outages_b: int
    start_gap_us: int | None
    end_gap_us: int | None


@dataclass(frozen=True)
class SimOutage:
    """A maximal span of P with at least two locations off at once; ``end`` None at now.

    The span is clipped to P. ``starts_before``: it begins at P's start and at least two of
    its locations were already off before P (their off pieces start before P's start).
    ``ends_after``: it reaches P's end, P ends before now, and at least two of its
    locations stay off past P's end (their off pieces end after it or are still open).
    """

    start: datetime
    end: datetime | None
    duration_us: int
    keys: tuple[int, ...]
    peak: int
    all_n: bool
    starts_before: bool = False
    ends_after: bool = False


@dataclass(frozen=True)
class TogetherSegment:
    state: str
    start_us: int
    end_us: int
    joint: bool


@dataclass(frozen=True)
class TogetherLane:
    segments: tuple[TogetherSegment, ...]
    all_off_us: int
    joint_us: int


@dataclass(frozen=True)
class DayLanes:
    day: date
    is_today: bool
    lanes: tuple[model.Row, ...]
    together: TogetherLane | None


@dataclass(frozen=True)
class HourCell:
    off_us: int
    monitored_us: int

    @property
    def value(self) -> int | None:
        return percent(self.off_us, self.monitored_us)


@dataclass(frozen=True)
class HourLane:
    """24 local-hour cells of one location, or of Together (``key`` None)."""

    key: int | None
    cells: tuple[HourCell, ...]


@dataclass(frozen=True)
class WeekCell:
    off_us: int
    count: int
    monitored: bool


@dataclass(frozen=True)
class WeekRow:
    monday: date
    sunday: date
    days_in_period: int
    partial: bool
    cells: tuple[WeekCell, ...]
    together: WeekCell | None


@dataclass(frozen=True)
class Comparison:
    period: Period
    now: datetime
    tz: str
    locations: tuple[LocationStats, ...]
    together: TogetherStats | None
    spans: tuple[TogetherSpan, ...]
    pairs: tuple[PairStats, ...]
    simultaneous: tuple[SimOutage, ...]
    simultaneous_total: int
    window: TimelineWindow
    days: tuple[DayLanes, ...]
    hours: tuple[HourLane, ...]
    weeks: tuple[WeekRow, ...]


# Day frames: one per day of P, shared by every lane (D-10)


@dataclass(frozen=True)
class _Frame:
    day: date
    a: datetime
    b: datetime
    points: tuple[datetime, ...]
    drops: tuple[tuple[datetime, datetime], ...]


def _frame(d: date, now: datetime, tz: str) -> _Frame:
    """The day's clip ``[a, b)`` and its offset-change cut points, exactly as ``model._row``."""
    a, b_full = model.day_bounds(d, tz)
    b = min(b_full, now)
    if b <= a:
        return _Frame(d, a, b, (), ())
    cuts = model.transitions(a, b_full, tz)
    # Fall-back: the UTC span of the repeated hour's first occurrence (fold 0) is not drawn.
    drops = tuple((t - (before - after), t) for t, before, after in cuts if after < before)
    points = tuple(sorted({t for t, _, _ in cuts} | {lo for lo, _ in drops}))
    return _Frame(d, a, b, points, drops)


def _walls(s: datetime, e: datetime, frame: _Frame, tz: str) -> Iterator[tuple[int, int]]:
    """The drawable wall-clock spans of the UTC span ``[s, e)`` inside the frame's day."""
    bounds = [s, *(x for x in frame.points if s < x < e), e]
    for s1, e1 in pairwise(bounds):
        if any(lo <= s1 and e1 <= hi for lo, hi in frame.drops):
            continue
        yield model.wall_us(s1, tz, end=False), model.wall_us(e1, tz, end=True)


def _day_index(starts: list[datetime], s: datetime, e: datetime) -> range:
    """The indexes of the days (by their start instants) that ``[s, e)`` touches."""
    lo = max(0, bisect_right(starts, s) - 1)
    hi = bisect_left(starts, e)
    return range(lo, hi)


def _lane(
    pieces: list[model.Piece], frame: _Frame, now: datetime, tz: str, *, is_today: bool
) -> tuple[model.Row, set[datetime]]:
    """``model.build_row`` of one day over the frame, plus the day's outage starts."""
    d = frame.day
    if frame.b <= frame.a:
        return model.Row(d, False, is_today, (), 0, 0, 0, 0, False), set()
    segments: list[model.Segment] = []
    totals = dict.fromkeys(model.STATES, 0)
    outages: set[datetime] = set()
    monitored = False
    for p in pieces:
        s = max(p.start, frame.a)
        e = min(p.end if p.end is not None else now, frame.b)
        if e <= s:
            continue
        totals[p.state] += _us(s, e)
        if p.state in model.MONITORED_STATES:
            monitored = True
        if p.state == "off" and p.outage_start is not None:
            outages.add(p.outage_start)
        segments.extend(model.Segment(p.state, ws, we) for ws, we in _walls(s, e, frame, tz))
    segments.sort(key=attrgetter("start_us", "end_us"))
    row = model.Row(
        day=d,
        dimmed=False,
        is_today=is_today,
        segments=tuple(segments),
        on_us=totals["on"],
        off_us=totals["off"],
        nm_us=totals["not_monitored"],
        count=len(outages),
        monitored=monitored,
    )
    return row, outages


# Per-location helpers


_Clip = tuple[datetime, datetime, model.Piece]


def _clipped(pieces: Sequence[model.Piece], period: Period, now: datetime) -> list[_Clip]:
    """The pieces' parts inside P as ``(start, end, piece)``, an open end run to now."""
    out = []
    for p in pieces:
        s = max(p.start, period.start)
        e = min(p.end if p.end is not None else now, period.end)
        if e > s:
            out.append((s, e, p))
    return out


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _location_stats(
    s: Series,
    clips: list[_Clip],
    rows: list[model.Row],
    outages: dict[datetime, OutageRec],
    period: Period,
) -> LocationStats:
    on = sum(r.on_us for r in rows)
    off = sum(r.off_us for r in rows)
    nm = sum(r.nm_us for r in rows)
    starts = {p.outage_start for _s, _e, p in clips if p.state == "off"}
    recs = [outages[t] for t in sorted(t for t in starts if t is not None)]
    offs = [r.off_us for r in recs]
    longest = None
    for rec in recs:  # start order, so a tie keeps the earlier start
        if longest is None or rec.off_us > longest.off_us:
            longest = rec
    days_monitored = sum(1 for r in rows if r.monitored)
    return LocationStats(
        key=s.key,
        name=s.name,
        on_us=on,
        off_us=off,
        nm_us=nm,
        nodata_us=period.length_us - on - off - nm,
        period_us=period.length_us,
        outage_count=len(recs),
        avg_outage_us=sum(offs) // len(offs) if offs else None,
        median_outage_us=_median(offs),
        longest=longest,
        days_with_outages=sum(1 for r in rows if r.off_us > 0),
        days_total=len(rows),
        days_monitored=days_monitored,
        off_per_day_us=off // days_monitored if days_monitored else None,
    )


# Together: one sweep over every location's clipped pieces


@dataclass(frozen=True)
class _Elem:
    """An elementary span of P: how many locations are on and off, and which are off.

    With at least two off: ``off_before`` counts the off locations whose off piece starts
    before P (only on the span at P's start), ``off_after`` those whose off piece ends after
    P or is open (only on the span at P's end).
    """

    start: datetime
    end: datetime
    on: int
    off: int
    off_keys: frozenset[int]
    off_before: int = 0
    off_after: int = 0


def _sweep(all_clips: list[list[_Clip]], period: Period) -> list[_Elem]:
    """The elementary spans of P between consecutive piece boundaries of any location."""
    # (instant, 0 = end before 1 = start, location index, the raw piece)
    events: list[tuple[datetime, int, int, model.Piece]] = []
    for i, clips in enumerate(all_clips):
        for s, e, p in clips:
            events.append((s, 1, i, p))
            events.append((e, 0, i, p))
    events.sort(key=lambda ev: (ev[0], ev[1]))
    state: list[str | None] = [None] * len(all_clips)
    piece: list[model.Piece | None] = [None] * len(all_clips)
    on = off = 0
    off_set: set[int] = set()
    elems: list[_Elem] = []
    t = period.start
    k = 0
    while t < period.end:
        while k < len(events) and events[k][0] <= t:
            _, kind, i, p = events[k]
            st = p.state
            if kind == 0:
                if state[i] == "on":
                    on -= 1
                elif state[i] == "off":
                    off -= 1
                    off_set.discard(i)
                state[i] = None
                piece[i] = None
            else:
                state[i] = st
                piece[i] = p
                if st == "on":
                    on += 1
                elif st == "off":
                    off += 1
                    off_set.add(i)
            k += 1
        nxt = events[k][0] if k < len(events) else period.end
        nxt = min(nxt, period.end)
        if off >= 2:
            raw = [q for i in off_set if (q := piece[i]) is not None]
            before = sum(1 for p in raw if p.start < period.start) if t == period.start else 0
            after = (
                sum(1 for p in raw if p.end is None or p.end > period.end)
                if nxt == period.end
                else 0
            )
            elems.append(_Elem(t, nxt, on, off, frozenset(off_set), before, after))
        else:
            elems.append(_Elem(t, nxt, on, off, frozenset()))
        t = nxt
    return elems


def _state_of(n: int, on: int, off: int) -> str:
    if off == n:
        return "all_off"
    if off:
        return "some_off"
    if on == n:
        return "all_on"
    return "partial"


def _together(elems: list[_Elem], n: int) -> tuple[TogetherStats, list[TogetherSpan]]:
    all_off = any_on = joint = known = 0
    spans: list[TogetherSpan] = []
    for el in elems:
        length = _us(el.start, el.end)
        unknown = n - el.on - el.off
        state = _state_of(n, el.on, el.off)
        is_joint = unknown == 0
        if state == "all_off":
            all_off += length
        if el.on:
            any_on += length
        if is_joint:
            joint += length
        if unknown < n:
            known += length
        last = spans[-1] if spans else None
        if last is not None and last.state == state and last.joint == is_joint:
            spans[-1] = TogetherSpan(state, last.start, el.end, is_joint)
        else:
            spans.append(TogetherSpan(state, el.start, el.end, is_joint))
    return TogetherStats(all_off, any_on, joint, known), spans


def _simultaneous(
    elems: list[_Elem], keys: list[int], period: Period, now: datetime
) -> list[SimOutage]:
    n = len(keys)
    found: list[SimOutage] = []
    cur: list[_Elem] = []

    def close() -> None:
        if not cur:
            return
        start, end = cur[0].start, cur[-1].end
        members: set[int] = set()
        for el in cur:
            members |= el.off_keys
        peak = max(el.off for el in cur)
        ongoing = end >= now
        found.append(
            SimOutage(
                start=start,
                end=None if ongoing else end,
                duration_us=_us(start, end),
                keys=tuple(keys[i] for i in sorted(members)),
                peak=peak,
                all_n=peak == n,
                starts_before=start == period.start and cur[0].off_before >= 2,
                ends_after=not ongoing and end == period.end and cur[-1].off_after >= 2,
            )
        )
        cur.clear()

    # The elementary spans tile P, so consecutive ones are always adjacent.
    for el in elems:
        if el.off >= 2:
            cur.append(el)
        else:
            close()
    close()
    found.reverse()
    return found


# Pairs


_Off = tuple[datetime, datetime, datetime]  # (start, end, outage start), clipped to P


def _offs(clips: list[_Clip]) -> list[_Off]:
    return [
        (s, e, p.outage_start)
        for s, e, p in clips
        if p.state == "off" and p.outage_start is not None
    ]


def _overlaps(xs: list[_Off], ys: list[_Off]) -> tuple[int, set[tuple[datetime, datetime]]]:
    """Both-off time of two sorted off lists, and the overlapping outage-start pairs."""
    both = 0
    pairs: set[tuple[datetime, datetime]] = set()
    i = j = 0
    while i < len(xs) and j < len(ys):
        s = max(xs[i][0], ys[j][0])
        e = min(xs[i][1], ys[j][1])
        if e > s:
            both += _us(s, e)
            pairs.add((xs[i][2], ys[j][2]))
        if xs[i][1] <= ys[j][1]:
            i += 1
        else:
            j += 1
    return both, pairs


def _pair(
    a: LocationStats,
    b: LocationStats,
    offs_a: list[_Off],
    offs_b: list[_Off],
    recs_a: dict[datetime, OutageRec],
    recs_b: dict[datetime, OutageRec],
) -> PairStats:
    both, matched = _overlaps(offs_a, offs_b)
    starts: list[int] = []
    ends: list[int] = []
    for sa, sb in sorted(matched):
        ra, rb = recs_a[sa], recs_b[sb]
        starts.append(_us(ra.start, rb.start))
        if ra.end is not None and rb.end is not None:
            ends.append(_us(ra.end, rb.end))
    return PairStats(
        a=a.key,
        b=b.key,
        both_off_us=both,
        share_a=percent(both, a.off_us),
        share_b=percent(both, b.off_us),
        coinciding_a=len({sa for sa, _ in matched}),
        outages_a=a.outage_count,
        coinciding_b=len({sb for _, sb in matched}),
        outages_b=b.outage_count,
        start_gap_us=_median(starts),
        end_gap_us=_median(ends),
    )


# Hours and weeks


def _add_hours(cells: list[list[int]], start_us: int, end_us: int, *, off: bool) -> None:
    """Add the wall-clock span to each local hour it overlaps (``[off, monitored]`` cells)."""
    for h in range(start_us // _HOUR_US, (end_us - 1) // _HOUR_US + 1):
        part = min(end_us, (h + 1) * _HOUR_US) - max(start_us, h * _HOUR_US)
        cells[h][1] += part
        if off:
            cells[h][0] += part


def _hour_lane(key: int | None, cells: list[list[int]]) -> HourLane:
    return HourLane(key, tuple(HourCell(off_us=c[0], monitored_us=c[1]) for c in cells))


def _weeks(period: Period) -> list[tuple[date, date, list[int]]]:
    """Every Monday-to-Sunday week overlapping P, newest first, with its day indexes in P."""
    days = period.days
    weeks: dict[date, list[int]] = {}
    for i, d in enumerate(days):
        weeks.setdefault(d - timedelta(days=d.weekday()), []).append(i)
    return [(m, m + timedelta(days=6), idx) for m, idx in sorted(weeks.items(), reverse=True)]


# The comparison


def _checked_series(s: Series) -> tuple[list[model.Piece], dict[datetime, OutageRec]]:
    # model._checked is the chart's own piece validation, so both read the same pieces.
    pieces = sorted((model._checked(p) for p in s.pieces), key=attrgetter("start"))
    recs: dict[datetime, OutageRec] = {}
    for o in s.outages:
        start = _utc(o.start)
        recs[start] = OutageRec(start, None if o.end is None else _utc(o.end), o.off_us)
    return pieces, recs


def compare(
    series: Sequence[Series],
    period: Period,
    now: datetime,
    tz: str,
    *,
    timeline_last: date | None = None,
) -> Comparison:
    """Every number of the Compare page for ``series`` (name order, kept everywhere) over P.

    ``timeline_last`` picks the timeline's window (``timeline_window``); it changes only
    ``window`` and ``days``, never another number. ValueError for a naive ``now``, a bad
    piece, or an off piece in P whose outage is missing from its series' ``outages``.
    """
    now = _utc(now)
    today = model.local_today(now, tz)
    days = period.days
    frames = [_frame(d, now, tz) for d in days]
    starts = [f.a for f in frames]
    n = len(series)
    keys = [s.key for s in series]

    stats: list[LocationStats] = []
    all_clips: list[list[_Clip]] = []
    all_offs: list[list[_Off]] = []
    all_recs: list[dict[datetime, OutageRec]] = []
    rows_by_series: list[list[model.Row]] = []
    day_outages: list[list[set[datetime]]] = []
    for s in series:
        pieces, recs = _checked_series(s)
        clips = _clipped(pieces, period, now)
        for _cs, _ce, p in clips:
            if p.state == "off" and p.outage_start not in recs:
                raise ValueError(f"series {s.key}: an off piece has no outage record")
        buckets: list[list[model.Piece]] = [[] for _ in days]
        for cs, ce, p in clips:
            for i in _day_index(starts, cs, ce):
                buckets[i].append(p)
        rows: list[model.Row] = []
        sets: list[set[datetime]] = []
        for frame, bucket in zip(frames, buckets, strict=True):
            row, outs = _lane(bucket, frame, now, tz, is_today=frame.day == today)
            rows.append(row)
            sets.append(outs)
        stats.append(_location_stats(s, clips, rows, recs, period))
        all_clips.append(clips)
        all_offs.append(_offs(clips))
        all_recs.append(recs)
        rows_by_series.append(rows)
        day_outages.append(sets)

    together: TogetherStats | None = None
    spans: list[TogetherSpan] = []
    sims: list[SimOutage] = []
    pairs: list[PairStats] = []
    t_lanes: list[TogetherLane] = []
    if n >= 2:
        elems = _sweep(all_clips, period)
        together, spans = _together(elems, n)
        sims = _simultaneous(elems, keys, period, now)
        for i in range(n):
            for j in range(i + 1, n):
                pairs.append(
                    _pair(stats[i], stats[j], all_offs[i], all_offs[j], all_recs[i], all_recs[j])
                )
        t_lanes = _together_lanes(spans, frames, tz)

    window = timeline_window(period.first, period.last, timeline_last)
    day_lanes = tuple(
        DayLanes(
            day=frame.day,
            is_today=frame.day == today,
            lanes=tuple(rows[k] for rows in rows_by_series),
            together=t_lanes[k] if t_lanes else None,
        )
        for k, frame in enumerate(frames)
        if window.first <= frame.day <= window.last
    )

    hours: list[HourLane] = []
    for s, rows in zip(series, rows_by_series, strict=True):
        cells = [[0, 0] for _ in range(HOURS)]
        for row in rows:
            for seg in row.segments:
                if seg.state in model.MONITORED_STATES:
                    _add_hours(cells, seg.start_us, seg.end_us, off=seg.state == "off")
        hours.append(_hour_lane(s.key, cells))
    if t_lanes:
        cells = [[0, 0] for _ in range(HOURS)]
        for lane in t_lanes:
            for tseg in lane.segments:
                if tseg.joint:
                    _add_hours(cells, tseg.start_us, tseg.end_us, off=tseg.state == "all_off")
        hours.append(_hour_lane(None, cells))

    episodes = [sp for sp in spans if sp.state == "all_off"]
    weeks: list[WeekRow] = []
    for monday, sunday, idx in _weeks(period):
        cells_w = []
        for rows, sets in zip(rows_by_series, day_outages, strict=True):
            starts_w: set[datetime] = set()
            for i in idx:
                starts_w |= sets[i]
            cells_w.append(
                WeekCell(
                    off_us=sum(rows[i].off_us for i in idx),
                    count=len(starts_w),
                    monitored=any(rows[i].monitored for i in idx),
                )
            )
        t_cell = None
        if t_lanes:
            wa = frames[idx[0]].a
            wb = max(wa, frames[idx[-1]].b)
            t_cell = WeekCell(
                off_us=sum(t_lanes[i].all_off_us for i in idx),
                count=sum(1 for sp in episodes if sp.start < wb and sp.end > wa),
                monitored=sum(t_lanes[i].joint_us for i in idx) > 0,
            )
        days_in = len(idx)
        weeks.append(WeekRow(monday, sunday, days_in, days_in < 7, tuple(cells_w), t_cell))

    return Comparison(
        period=period,
        now=now,
        tz=tz,
        locations=tuple(stats),
        together=together,
        spans=tuple(spans),
        pairs=tuple(pairs),
        simultaneous=tuple(sims[:SIM_CAP]),
        simultaneous_total=len(sims),
        window=window,
        days=day_lanes,
        hours=tuple(hours),
        weeks=tuple(weeks),
    )


def _together_lanes(spans: list[TogetherSpan], frames: list[_Frame], tz: str) -> list[TogetherLane]:
    """The Together lane of every day: the spans sliced with the day's frame (D-6)."""
    starts = [f.a for f in frames]
    buckets: list[list[TogetherSpan]] = [[] for _ in frames]
    for sp in spans:
        for i in _day_index(starts, sp.start, sp.end):
            buckets[i].append(sp)
    lanes = []
    for frame, bucket in zip(frames, buckets, strict=True):
        segs: list[TogetherSegment] = []
        all_off = joint = 0
        for sp in bucket:
            s = max(sp.start, frame.a)
            e = min(sp.end, frame.b)
            if e <= s:
                continue
            if sp.state == "all_off":
                all_off += _us(s, e)
            if sp.joint:
                joint += _us(s, e)
            segs.extend(
                TogetherSegment(sp.state, ws, we, sp.joint) for ws, we in _walls(s, e, frame, tz)
            )
        segs.sort(key=attrgetter("start_us", "end_us"))
        lanes.append(TogetherLane(tuple(segs), all_off, joint))
    return lanes
