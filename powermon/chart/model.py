"""The weekly chart's data: seven local-day rows from the stored timeline (CHRT-01/03/06/07).

PURE: no Django import and no clock read. Every function takes its inputs, including
``now``, ``today`` and the display time zone name, as arguments, so the renderer, the
captions and the message lifecycle share one definition and the tests need no database.

Rules (docs/chart-spec.md sections 8 and 9; docs/v1-lessons.md INV-03, INV-04, INV-08):
- The input is the location's stored intervals (``Piece``, read from ``power_interval``,
  KD1): ``on``, ``off`` or ``not_monitored`` over ``[start, end)``, open while ``end`` is
  None and then drawn up to ``now``. Time that no interval covers is no data; it is never
  stored and never counted.
- A row is one local day of the display time zone: the instants from its local midnight
  to the next local midnight, start included, end excluded. Today's row stops at ``now``.
- Positions follow the local wall clock and totals follow real elapsed time (INV-08). A
  piece is split at every UTC-offset change inside the day. On a fall-back day the first
  occurrence (fold 0) of the repeated hour is not drawn; on a spring-forward day the
  missing hour stays no data. Totals stay real time, so 23 h and 25 h days add up.
- Off time and the outage count come from ``off`` pieces only, so not-monitored and
  uncovered time is never off time and never an outage (INV-04, KD3). The count is the
  number of distinct outage starts: an outage split by not-monitored time counts once,
  two adjacent off pieces of two outages count twice (INV-11), an outage that crosses
  midnight counts on both days and an ongoing outage counts.
- The rows run Monday to Sunday of today's week. A day after today shows the same weekday
  of the previous week, dimmed (CHRT-07, K-5).

Durations are integer microseconds: nothing here uses a float or true division.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from operator import attrgetter
from zoneinfo import ZoneInfo

ONE_US = timedelta(microseconds=1)
HOUR_US = 3_600_000_000
DAY_US = 86_400_000_000
STATES = ("on", "off", "not_monitored")
# The states that make a day monitored. Not-monitored time never does (chart-spec §8 "—").
MONITORED_STATES = ("on", "off")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_S = timedelta(seconds=1)
# Step of the UTC-offset scan, in seconds. No real zone changes its offset twice within it.
_SCAN_S = 3600
_WEEK = timedelta(days=7)


@dataclass(frozen=True)
class Piece:
    """One stored interval ``[start, end)``; ``end`` is None while it is open.

    ``outage_start`` is the start of the outage an off piece belongs to, None otherwise.
    """

    state: str
    start: datetime
    end: datetime | None
    outage_start: datetime | None


@dataclass(frozen=True)
class Segment:
    """A drawable span of one row, in wall-clock microseconds since local midnight."""

    state: str
    start_us: int
    end_us: int


@dataclass(frozen=True)
class Row:
    """One local day: its drawable segments and its real-time totals.

    ``day`` is the date shown (the previous week's date in a dimmed row). ``on_us``,
    ``off_us`` and ``nm_us`` are real elapsed time; no-data time is in none of them.
    ``count`` is the number of outages that overlap the day. ``monitored`` is False when
    the day has no on and no off time at all (shown as "—").
    """

    day: date
    dimmed: bool
    is_today: bool
    segments: tuple[Segment, ...]
    on_us: int
    off_us: int
    nm_us: int
    count: int
    monitored: bool


@dataclass(frozen=True)
class Week:
    """The seven rows, Monday to Sunday of ``today``'s week, as one chart draws them.

    ``now`` is the render time (aware UTC). ``live`` is False for the finished-day render,
    which has no now marker (chart-spec §7).
    """

    monday: date
    today: date
    now: datetime
    tz: str
    live: bool
    rows: tuple[Row, ...]

    @property
    def sunday(self) -> date:
        return self.monday + timedelta(days=6)

    @property
    def today_row(self) -> Row:
        return self.rows[self.today.weekday()]


def _utc(t: datetime) -> datetime:
    """``t`` as an aware UTC datetime; ValueError for a naive datetime."""
    if t.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return t.astimezone(UTC)


def local_today(now: datetime, tz: str) -> date:
    """The local date of ``now`` in the IANA zone ``tz``."""
    return _utc(now).astimezone(ZoneInfo(tz)).date()


def day_bounds(d: date, tz: str) -> tuple[datetime, datetime]:
    """The UTC instants of the local midnight that starts ``d`` and of the next one.

    Each bound is converted on its own, so a 23 h or 25 h day gets its real length; never
    ``midnight + 24 h`` (the v1 DST bug, INV-08).
    """
    zone = ZoneInfo(tz)
    start = datetime.combine(d, time(0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    return start, end


def next_midnight(d: date, tz: str) -> datetime:
    """The UTC instant of the local midnight that ends day ``d``."""
    return day_bounds(d, tz)[1]


def _offset(ts: int, zone: ZoneInfo) -> timedelta:
    """The UTC offset of ``zone`` at ``ts`` whole seconds after the epoch."""
    return (_EPOCH + timedelta(seconds=ts)).astimezone(zone).utcoffset() or timedelta(0)


def transitions(a: datetime, b: datetime, tz: str) -> list[tuple[datetime, timedelta, timedelta]]:
    """Every UTC-offset change of ``tz`` in ``(a, b]`` as ``(instant, before, after)``.

    Scans whole seconds in one-hour steps, then bisects a step whose two ends have
    different offsets down to the exact second (Kyiv on 2026-10-25: 01:00 UTC, +03:00 to
    +02:00).
    """
    zone = ZoneInfo(tz)
    t = (_utc(a) - _EPOCH) // _ONE_S
    end = (_utc(b) - _EPOCH) // _ONE_S
    found: list[tuple[datetime, timedelta, timedelta]] = []
    before = _offset(t, zone)
    while t < end:
        step = min(t + _SCAN_S, end)
        after = _offset(step, zone)
        if after != before:
            lo, hi = t, step
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if _offset(mid, zone) == before:
                    lo = mid
                else:
                    hi = mid
            found.append((_EPOCH + timedelta(seconds=hi), before, after))
            before = after
        t = step
    return found


def _tod_us(local_t: datetime) -> int:
    """Wall-clock microseconds since midnight of a local datetime."""
    seconds = (local_t.hour * 60 + local_t.minute) * 60 + local_t.second
    return seconds * 1_000_000 + local_t.microsecond


def wall_us(t: datetime, tz: str, *, end: bool) -> int:
    """Wall-clock microseconds since local midnight for the instant ``t`` (0..DAY_US).

    A start boundary reads the local time at ``t``. An end boundary reads it one
    microsecond before ``t`` (the offset in force before the boundary) and adds that
    microsecond back. So the day's end is 24 h exactly (not 0, the v1 "next midnight"
    bug), and a piece that ends at the spring-forward instant ends at 03:00 while the next
    one starts at 04:00.
    """
    zone = ZoneInfo(tz)
    instant = _utc(t)
    if end:
        return _tod_us((instant - ONE_US).astimezone(zone)) + 1
    return _tod_us(instant.astimezone(zone))


def _checked(p: Piece) -> Piece:
    """``p`` with UTC times; ValueError for a bad state, a naive time or ``end <= start``."""
    if p.state not in STATES:
        raise ValueError(f"unknown interval state: {p.state!r}")
    start = _utc(p.start)
    end = None if p.end is None else _utc(p.end)
    if end is not None and end <= start:
        raise ValueError("an interval must end after it starts")
    outage_start = None if p.outage_start is None else _utc(p.outage_start)
    if p.state == "off" and outage_start is None:
        raise ValueError("an off interval needs its outage start")
    return Piece(p.state, start, end, outage_start)


def _row(
    pieces: list[Piece], d: date, tz: str, now: datetime, *, dimmed: bool, is_today: bool
) -> Row:
    """``build_row`` for pieces that are already checked and a ``now`` in UTC."""
    a, b_full = day_bounds(d, tz)
    b = min(b_full, now)
    if b <= a:
        return Row(d, dimmed, is_today, (), 0, 0, 0, 0, False)
    cuts = transitions(a, b_full, tz)
    # Fall-back: the UTC span of the repeated hour's first occurrence (fold 0) is not drawn.
    drops = [(t - (before - after), t) for t, before, after in cuts if after < before]
    points = sorted({t for t, _, _ in cuts} | {lo for lo, _ in drops})
    segments: list[Segment] = []
    totals = dict.fromkeys(STATES, 0)
    outages: set[datetime | None] = set()
    monitored = False
    for p in pieces:
        s = max(p.start, a)
        e = min(p.end if p.end is not None else now, b)
        if e <= s:
            continue
        # Real elapsed time over the UTC clip (23 h and 25 h days, INV-08).
        totals[p.state] += (e - s) // ONE_US
        if p.state in MONITORED_STATES:
            monitored = True
        if p.state == "off":
            outages.add(p.outage_start)
        bounds = [s, *(x for x in points if s < x < e), e]
        for s1, e1 in pairwise(bounds):
            if any(lo <= s1 and e1 <= hi for lo, hi in drops):
                continue
            segments.append(Segment(p.state, wall_us(s1, tz, end=False), wall_us(e1, tz, end=True)))
    segments.sort(key=attrgetter("start_us", "end_us"))
    return Row(
        day=d,
        dimmed=dimmed,
        is_today=is_today,
        segments=tuple(segments),
        on_us=totals["on"],
        off_us=totals["off"],
        nm_us=totals["not_monitored"],
        count=len(outages),
        monitored=monitored,
    )


def build_row(
    pieces: Iterable[Piece], d: date, tz: str, now: datetime, *, dimmed: bool, is_today: bool
) -> Row:
    """The row of local day ``d`` up to ``now``: wall-clock segments and real-time totals.

    The segments are sorted by ``(start_us, end_us)``. A day that starts at or after
    ``now`` gives an empty, unmonitored row. ValueError for a naive ``now`` or a bad piece.
    """
    checked = [_checked(p) for p in pieces]
    return _row(checked, d, tz, _utc(now), dimmed=dimmed, is_today=is_today)


def week_window(today: date, now: datetime, tz: str) -> tuple[datetime, datetime]:
    """The UTC span a chart of ``today``'s week reads.

    It runs from the local midnight of ``today - 6`` to ``min(now, next local midnight)``:
    the seven rows always show exactly the dates ``today - 6`` to ``today``, so one overlap
    query over this span covers them. The end is never before the start.
    """
    start = day_bounds(today - timedelta(days=6), tz)[0]
    return start, max(start, min(_utc(now), next_midnight(today, tz)))


def build_week(pieces: Iterable[Piece], *, today: date, now: datetime, tz: str, live: bool) -> Week:
    """The seven rows Monday..Sunday of ``today``'s week (CHRT-07, K-5).

    A day after ``today`` is shown as the same weekday of the previous week (its date
    minus 7 days) and is dimmed. ValueError for a naive ``now`` or a bad piece.
    """
    checked = [_checked(p) for p in pieces]
    now = _utc(now)
    monday = today - timedelta(days=today.weekday())
    rows = []
    for i in range(7):
        cur = monday + timedelta(days=i)
        future = cur > today
        shown = cur - _WEEK if future else cur
        rows.append(_row(checked, shown, tz, now, dimmed=future, is_today=cur == today))
    return Week(monday=monday, today=today, now=now, tz=tz, live=live, rows=tuple(rows))
