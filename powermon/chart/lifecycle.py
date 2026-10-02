"""The chart message lifecycle in the worker's Telegram I/O loop (D-01 to D-07, CHRT-05).

Worker only. ``run_step`` is the last step of an I/O pass (``io_loop.run_iteration`` with
``charts=True``): it makes at most one chart call per pass, after every due alert and the
ops head, so chart work delays an alert by at most one call (D-05, INV-14).

Scheduling is condition-based, with all state in the database (INV-18, KD4): each pass
reads a snapshot of the monitored locations and their chart records and asks the pure
``plan`` for the one step that is due now. There is no timer: a step missed while the
worker was down is simply due on the next pass.

- Post: a monitored location (status on or off, not deleted; ``alerts_enabled`` and
  ``maintenance`` never matter, INV-05) with no active record for today's local date gets
  its chart posted silently (D-01, D-03). The record (location, date, the chat it was sent
  to, the bot that sent it, the message id) is written right after Telegram accepts the
  photo and before any pin (INV-17). The bot is named by ``io_loop.bot_key`` of the token
  that posted, never the token (D-08), so a later chat or token change shows on the record.
  The write is ``INSERT ... ON CONFLICT DO NOTHING`` on the partial unique index
  ``chart_message_one_active_per_day``, so two workers can never record two charts for a
  day (INV-17 #2).
  If that write raises a database error, the post Telegram accepted is kept in
  ``RelayState.chart_posted``, with the key of the bot that posted it, and written first by
  the next chart step, before any step is chosen, so it is never posted a second time
  (WR-04 analogue) and names its bot even if the token changed meanwhile. A record that can
  never be written (its location is gone) leaves the photo untracked, with one WARNING.
- Pin: a later pass pins the recorded message silently, in the chat stored with the
  record, never the location's current chat (D-01, D-04). A permanent pin failure (the
  bot may post but not pin, INV-17 #1) is stored as ``pin_failed_at``; the pin is tried
  again only after the next successful render (D-07). The first such failure opens the
  location's ``chart_pin_failed`` incident and queues one ``ops_pin_failed`` notice; the
  pin that works again closes it and queues one ``ops_pin_restored`` notice. Each goes
  in the same transaction as the record's UPDATE, and only the opener (or closer) whose
  write changed the database notifies, so a pin retried every 15 min never repeats a
  notice (INV-20 shape).
- Today's chart deleted in the channel ("message to edit / pin not found" on a refresh
  or a pin): the record is retired (``retired_at``, unpinned) and never called again, and
  the next pass posts exactly one replacement, records it and pins it (INV-17 #3, D-06).
- Finalize: every older record (``local_date`` before today) that has no final edit yet
  gets its finished-day render (chart-spec §7: now = the local midnight that ends its
  day, no now line, no pill, the caption's line 1 only, D-01, D-13), edited in the chat
  and message stored with it (D-04); oldest first. A finalized chart is never rendered
  again (D-14), so the final edit waits until the day's timeline has settled (INV-03):
  OFF is recorded after the fact, backdated to the last heartbeat, so an outage that
  started in the day's last minutes is in the timeline only a timeout after midnight.
  The record's day has settled once the detection cursor
  (``system_state.last_cycle_completed_at``) is the location's ``settle`` past the
  midnight that ends it (``settle_time``, ``settled_records``). The cursor, not the wall
  clock, so a detection stall or a lapse across midnight holds the final edit too. This
  covers every OFF unless detection's decisions fail in every cycle of the short margin
  (one lapse threshold, a few cycles) between midnight + the location's timeout and the
  settle point: no cycle before that margin can record an OFF whose last heartbeat came
  just before midnight. Detection moves the cursor before a cycle's decisions, and a
  failed decision (a database error for one location, or for the whole cycle on a
  connection that still works) does not hold it back. That rare residual is accepted
  (code review WR-01): each failure is logged, and the finished chart is not redrawn
  (D-14).
- Unpin: every older record gets exactly one unpin by its own message id in its own chat,
  oldest first, and nothing else is ever unpinned (D-04, INV-19). It is made whatever
  ``pinned`` says: a pin whose answer was ambiguous, whose outcome could not be written,
  or that a crash cut off may have taken effect while the record says "not pinned", and
  the record must not leave the lifecycle with its pin in place (T-03-27). Unpinning a
  message that is not pinned is "not modified" (ok) or "not found", so it costs at most
  one call per location and day. ``unpinned_at`` records that it was made. It does not
  wait for the final edit: today's chart is pinned and the older one unpinned within
  seconds (D-02), and the final edit follows once the day has settled. An older record
  leaves the lifecycle once it is finalized and unpinned (or retired).
- Cleanup of older records never blocks and never loops (INV-19): a permanent error on a
  final edit or an unpin marks the record finalized or unpinned anyway (best effort, one
  WARNING); an older chart deleted in the channel ("not found") is retired with no
  repost, or simply marked unpinned. Every record transition is a conditional UPDATE
  decided by row count, so a repeated or concurrent outcome changes nothing twice.
- Refresh: today's chart is due ``REFRESH_EVERY`` (15 min) after its last successful
  render (``last_rendered_at``, the answer time), and is edited in place in its recorded
  chat; "message is not modified" counts as rendered (D-05, CHRT-02). The state is in the
  database, so after downtime exactly one catch-up refresh is made, not one per missed
  slot (INV-18). Midnight-class steps (post, pin, finalize, unpin) beat any refresh; among
  due refreshes the oldest render goes first, ties to the lower location id.

Each step is its own condition, checked on every pass (D-02): after downtime across one
or more midnights the passes post and pin one chart for today and finalize and unpin
every older chart, and a day the worker missed gets no chart (D-03, INV-18, INV-19). When
both are due, an older record's final edit goes before its unpin (D-02 order); a final
edit not due yet (its day has not settled) never holds the unpin.

Rendering runs inline in the I/O thread right before its call (D-05), with the location's
current name and language and the display time zone read at render time (D-14). The
render module (Pillow) is imported inside ``chart_content`` only, so the web process,
which imports the app's models, never loads Pillow.

Before each call the worker checks that its lease session (``RelayState.lease_pid``) still
holds the worker lock, with the same ``pg_locks`` predicate as the outbox claim; a stale
worker makes no chart call (C1).

Backoff (D-02, D-06, INV-14). Chart calls share the alert relay's per-bot and per-chat
backoff, read only: a bot whose ``bot_wide_key`` or a channel whose ``chat_key`` is in
the future gets no chart call. A chart outcome writes its own step key
(``chart_key``: ``chart:<location>:<step>[:<record>]``) and never ``chat_key``, so a
chart's per-chat, permanent or render failure never holds the channel's alerts. Only a
bot-wide outcome (429, 5xx, refused connection) also sets ``bot_wide_key``, for exactly the
hold the relay gives an alert's outcome of that kind. On top of that hold, the step waits
``step_delay(n)`` after its n-th consecutive failure (``RelayState.chart_failures``): 30 s,
doubling, at most 15 min. So a failed step's key always outlives its bot's hold, and the
location's other due steps go first (D-02); and an ambiguous post, which is posted again
(D-06), slows down to at most 4 untracked photos an hour. Permanent errors and render
errors wait a fixed 15 min. A step whose call was made but whose record UPDATE raised a
database error (refresh, pin, finalize, unpin: all idempotent) waits ``step_delay(n)``
too, so a write that keeps failing never turns into a call per pass. Any other
unexpected error once a step is chosen, except a lost connection, also makes that step
alone wait 15 min, with one ERROR line and its traceback, so the next pass chooses
another step and one location's error never stops the other charts (INV-13). Nothing
here sleeps.

The step keys stay bounded (``RelayState`` lives as long as the worker): each chart step
first drops every ``chart:`` key that no longer guards a step, i.e. one of a location
that is not monitored, a post key once today's record exists, a refresh key while it
does not, a pin key of a record that is pinned or no longer today's (an older record's
pin is never retried: its one unpin follows instead), a finalize or unpin key of a record
that is done or retired. The alert relay's keys are never touched.

Each call logs one INFO line, ``chart <step> for location <id>: <kind> (<code>)
render_ms=<n> call_ms=<n>``, with no token and no Telegram description (OPS-08).
"""

import logging
import threading
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from django.conf import settings
from django.db import (
    Error,
    IntegrityError,
    InterfaceError,
    OperationalError,
    connection,
    transaction,
)

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident
from powermon.chart import model, source
from powermon.chart.models import ChartMessage
from powermon.clock import Clock
from powermon.engine import lapse, rules
from powermon.engine.models import SystemState
from powermon.i18n import chart_texts, times
from powermon.telegram.client import DEFAULT_RETRY_AFTER_S, SendResult, TelegramClient
from powermon.worker import io_loop
from powermon.worker.lease import LOCK_KEY

log = logging.getLogger(__name__)

Step = Literal["post", "pin", "finalize", "unpin", "refresh"]

# The ops_incident kind of a location whose bot may post but not pin (D-07, INV-17 #1).
KIND_CHART_PIN_FAILED = "chart_pin_failed"
# Every chart step key starts with this (``chart_key``); no other key does.
_KEY_PREFIX = "chart:"
# The HTTP status a pin-failure notice names when the result code carries none.
_DEFAULT_PIN_STATUS = 400

# Today's chart is due for a refresh this long after its last successful render (D-05).
REFRESH_EVERY = timedelta(minutes=15)
# A failed step's own wait: STEP_RETRY after its first consecutive failure, doubling with
# each further one, at most STEP_RETRY_MAX (``step_delay``).
STEP_RETRY = timedelta(seconds=io_loop.BACKOFF_CAP_S)
STEP_RETRY_MAX = REFRESH_EVERY
# Doublings after which STEP_RETRY is past STEP_RETRY_MAX (30 s * 2**5 = 16 min).
_MAX_DOUBLINGS = 5
# The steps that render a chart right before their call.
_RENDERED: tuple[Step, ...] = ("post", "refresh", "finalize")
# Outcomes no retry will change: the message is refused or gone.
_PERMANENT_KINDS = ("permanent", "edit_target_missing")
# The cleanup of older records (INV-19): a permanent outcome ends the step for good.
_CLEANUP: tuple[Step, ...] = ("finalize", "unpin")
# The steps on today's record: its message gone means today's chart is posted again.
_TODAYS: tuple[Step, ...] = ("pin", "refresh")

LOCATIONS_SQL = """
SELECT l.id, l.name, l.language, l.bot_token, l.chat_id, l.period_s, l.grace_s, l.router_grace
  FROM location l
  JOIN location_state s ON s.location_id = l.id
 WHERE s.status IN ('on', 'off') AND l.deleted_at IS NULL
 ORDER BY l.id
"""
# Today's active records, and older ones that still need their final edit or their unpin
# (whether or not they are known pinned, INV-19).
ROWS_SQL = """
SELECT id, location_id, local_date, chat_id, message_id, pinned, pin_failed_at,
       last_rendered_at, finalized_at, unpinned_at
  FROM chart_message
 WHERE retired_at IS NULL
   AND (local_date = %(today)s
        OR (local_date < %(today)s AND (finalized_at IS NULL OR unpinned_at IS NULL)))
 ORDER BY local_date, id
"""
# The record of a posted chart, written right after the send succeeded (INV-17). A second
# active record for the same day is refused by chart_message_one_active_per_day.
INSERT_SQL = """
INSERT INTO chart_message (location_id, local_date, chat_id, bot_key, message_id, pinned,
                           pin_failed_at, last_rendered_at, finalized_at, unpinned_at,
                           retired_at, created_at)
VALUES (%(location_id)s, %(local_date)s, %(chat_id)s, %(bot_key)s, %(message_id)s, false,
        NULL, %(answered)s, NULL, NULL, NULL, %(answered)s)
ON CONFLICT DO NOTHING RETURNING id
"""
# The C1 fence: the lease session holds the worker lock right now (outbox.CLAIM_HELD_SQL).
LEASE_HELD_SQL = """
SELECT 1 FROM pg_locks
 WHERE locktype = 'advisory' AND granted AND pid = %(pid)s
   AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
   AND classid = %(classid)s::oid AND objid = %(objid)s::oid AND objsubid = 1
"""
_LOCK_CLASSID = LOCK_KEY >> 32
_LOCK_OBJID = LOCK_KEY & 0xFFFFFFFF


@dataclass(frozen=True)
class ChartLocation:
    """A monitored location as the chart needs it, read at the start of the step (D-14)."""

    location_id: int
    name: str
    language: str
    bot_token: str = field(repr=False)
    chat_id: int
    # How long after a day ends detection may still record an OFF that started in it
    # (``settle_time``): the day's final edit waits until the detection cursor passed it.
    settle: timedelta


@dataclass(frozen=True)
class ChartRow:
    """One active ``chart_message`` record."""

    id: int
    location_id: int
    local_date: date
    chat_id: int
    message_id: int
    pinned: bool
    pin_failed_at: datetime | None
    last_rendered_at: datetime
    finalized_at: datetime | None
    # The older record's one unpin was made (INV-19); None until then.
    unpinned_at: datetime | None


@dataclass(frozen=True)
class Action:
    """The one chart step a pass makes: the step, its location and its record (if any)."""

    step: Step
    location: ChartLocation
    row: ChartRow | None = None


def chart_key(location_id: int, step: Step, row_id: int | None = None) -> str:
    """A chart step's own backoff key in ``RelayState.not_before``: by location, never a token."""
    key = f"{_KEY_PREFIX}{location_id}:{step}"
    return key if row_id is None else f"{key}:{row_id}"


def step_delay(failures: int) -> timedelta:
    """A step's own wait after its ``failures``-th consecutive failure (D-02, D-06).

    30 s after the first, doubling with each further one, at most 15 min: 30 s, 60 s, 2,
    4, 8 min, then 15 min. ValueError for ``failures`` below 1.
    """
    if failures < 1:
        raise ValueError(f"step_delay() needs at least 1 failure, not {failures}")
    return min(STEP_RETRY * 2 ** min(failures - 1, _MAX_DOUBLINGS), STEP_RETRY_MAX)


def settle_time(period_s: int, grace_s: int, router_grace: bool) -> timedelta:
    """How long after an instant detection may still record an OFF that started before it.

    OFF is recorded after the fact (INV-03): the first detection cycle more than the
    effective timeout after the last heartbeat records it, backdated to that heartbeat. So
    an OFF that started before ``t`` is recorded by the first cycle at or after ``t`` + the
    location's longest timeout (``rules.longest_timeout``). Detection moves its cursor
    before it makes that cycle's decisions, so one lapse threshold is added on top: a
    cursor that far past ``t`` + the timeout means that an earlier cycle past it has
    run its decisions, or that the gap before the cursor's cycle was longer than the
    threshold and was carved as not monitored, which starts any outage found after it at
    the carve. A decision that failed in that cycle (a logged error) does not hold the
    cursor back, so the final chart misses an OFF whose decisions failed in every cycle
    of that last lapse-threshold margin after ``t`` + the timeout (a few cycles; an
    accepted residual, code review WR-01).
    """
    return rules.longest_timeout(period_s, grace_s, router_grace) + lapse.LAPSE_THRESHOLD


def detection_cursor() -> datetime | None:
    """``system_state.last_cycle_completed_at``: None before detection's first cycle."""
    return (
        SystemState.objects.filter(pk=1).values_list("last_cycle_completed_at", flat=True).first()
    )


def settled_records(
    locations: list[ChartLocation],
    rows: list[ChartRow],
    *,
    today: date,
    detected_until: datetime | None,
    tz: str,
) -> frozenset[int]:
    """The ids of the older records whose day has settled: their final edit may be made.

    An older record's day has settled once the detection cursor (``detected_until``) is at
    least its location's ``settle`` past the local midnight that ends the day: every OFF
    that started in the day is then in the stored timeline (INV-03), including an outage
    that crosses midnight, which counts on both days (chart-spec §8). The exception is an
    OFF whose decisions failed in every cycle of the short margin (one lapse threshold, a
    few cycles) just before the settle point: each failure is logged, and the finished
    chart is not redrawn (D-14), an accepted residual (code review WR-01).
    The cursor, not the wall clock: while detection stalls, or across a lapse, the final
    edit waits too (a lapse carve commits before the cursor moves). With no cursor
    (detection has not run yet) no day has settled. Only records not finalized yet, of
    monitored locations.
    """
    if detected_until is None:
        return frozenset()
    settle = {location.location_id: location.settle for location in locations}
    return frozenset(
        row.id
        for row in rows
        if row.local_date < today
        and row.finalized_at is None
        and row.location_id in settle
        and detected_until >= model.next_midnight(row.local_date, tz) + settle[row.location_id]
    )


def read_snapshot(today: date) -> tuple[list[ChartLocation], list[ChartRow]]:
    """The monitored locations by id, and the records ``plan`` may act on, oldest first."""
    with connection.cursor() as cur:
        cur.execute(LOCATIONS_SQL)
        locations = [
            ChartLocation(
                location_id=r[0],
                name=r[1],
                language=r[2],
                bot_token=r[3],
                chat_id=r[4],
                settle=settle_time(r[5], r[6], r[7]),
            )
            for r in cur.fetchall()
        ]
        cur.execute(ROWS_SQL, {"today": today})
        rows = [
            ChartRow(
                id=r[0],
                location_id=r[1],
                local_date=r[2],
                chat_id=r[3],
                message_id=r[4],
                pinned=r[5],
                pin_failed_at=r[6],
                last_rendered_at=r[7],
                finalized_at=r[8],
                unpinned_at=r[9],
            )
            for r in cur.fetchall()
        ]
    return locations, rows


def plan(
    locations: list[ChartLocation],
    rows: list[ChartRow],
    *,
    today: date,
    now: datetime,
    not_before: dict[str, datetime],
    settled: AbstractSet[int] = frozenset(),
) -> Action | None:
    """The one chart step due now, or None. Pure: reads nothing but its arguments.

    Midnight-class steps go first: locations by ascending id, and the first one with a due
    step wins, so one location's midnight work is done before the next one's starts.
    Within a location the steps go in D-02 order: post today's chart, pin it, give the
    oldest older record without one its final edit, unpin the oldest older record not
    unpinned yet, whatever ``pinned`` says (its pin may have taken effect unrecorded,
    INV-19). A final edit is due only for a record in ``settled`` (``settled_records``: its
    day's timeline is complete, INV-03); one not due yet never holds the unpin, so two
    charts are pinned for seconds only. Only when none is due anywhere, the refresh that
    has waited longest goes: today's record with the oldest ``last_rendered_at`` at least
    ``REFRESH_EVERY`` ago, ties to the lower location id (CHRT-02). A step whose own key
    in ``not_before`` is in the future is skipped, so the next due step goes instead: a
    failing post never blocks the older charts' cleanup (D-02, INV-19). The alert relay's
    backoff is respected, read only: a location whose bot waits (``bot_wide_key``) makes
    no step, and a step whose channel waits (``chat_key`` of the chat it would call) is
    skipped (D-06).
    """

    def waiting(key: str) -> bool:
        return not_before.get(key, now) > now

    due_refreshes: list[tuple[datetime, int, Action]] = []
    for location in locations:
        if waiting(io_loop.bot_wide_key(location.bot_token)):
            continue
        today_row = _today_row(rows, location.location_id, today)
        action = _midnight_step(location, rows, today_row, today, waiting, settled)
        if action is not None:
            return action
        if (
            today_row is not None
            and now - today_row.last_rendered_at >= REFRESH_EVERY
            and not waiting(chart_key(location.location_id, "refresh"))
            and not waiting(io_loop.chat_key(location.bot_token, today_row.chat_id))
        ):
            refresh = Action("refresh", location, today_row)
            due_refreshes.append((today_row.last_rendered_at, location.location_id, refresh))
    if not due_refreshes:
        return None
    return min(due_refreshes, key=lambda due: (due[0], due[1]))[2]


def _today_row(rows: list[ChartRow], location_id: int, today: date) -> ChartRow | None:
    """The location's active record for today (the unique index allows at most one)."""
    for row in rows:
        if row.location_id == location_id and row.local_date == today:
            return row
    return None


def _midnight_step(
    location: ChartLocation,
    rows: list[ChartRow],
    today_row: ChartRow | None,
    today: date,
    waiting: Callable[[str], bool],
    settled: AbstractSet[int],
) -> Action | None:
    """The location's first due step of D-02 (post, pin, finalize, unpin), or None.

    A final edit is due only once its record's day has settled (``settled``). Every older
    record gets its one unpin, pinned or not as far as the record knows (INV-19).
    """
    location_id = location.location_id
    if today_row is None:
        if not waiting(chart_key(location_id, "post")) and not waiting(
            io_loop.chat_key(location.bot_token, location.chat_id)
        ):
            return Action("post", location)
    elif _pin_due(today_row) and _free(location, "pin", today_row, waiting):
        return Action("pin", location, today_row)
    older = sorted(
        (row for row in rows if row.location_id == location_id and row.local_date < today),
        key=lambda row: (row.local_date, row.id),
    )
    for row in older:
        if (
            row.finalized_at is None
            and row.id in settled
            and _free(location, "finalize", row, waiting)
        ):
            return Action("finalize", location, row)
    for row in older:
        if row.unpinned_at is None and _free(location, "unpin", row, waiting):
            return Action("unpin", location, row)
    return None


def _free(
    location: ChartLocation, step: Step, row: ChartRow, waiting: Callable[[str], bool]
) -> bool:
    """Neither the step's own key nor the record's channel (the relay's ``chat_key``) waits.

    The channel is the chat stored with the record, which the call goes to (D-04).
    """
    return not waiting(chart_key(location.location_id, step, row.id)) and not waiting(
        io_loop.chat_key(location.bot_token, row.chat_id)
    )


def _pin_due(row: ChartRow) -> bool:
    """Not pinned, and no permanent pin failure since the last render (D-07)."""
    if row.pinned:
        return False
    return row.pin_failed_at is None or row.pin_failed_at < row.last_rendered_at


def chart_content(
    location: ChartLocation, day: date, now: datetime, *, live: bool, tz: str
) -> tuple[bytes, str]:
    """The chart PNG and its caption for ``day`` as of ``now``, in the location's language.

    A live chart's caption carries today's off time, outage count and the update time,
    all from the same ``now`` as the image's now pill (CHRT-04); a finished one has line 1
    only (D-13). The render module is imported here, in the worker, and called as a module
    attribute.
    """
    from powermon.chart import render  # Pillow: worker only, never at import time

    week = source.load_week(location.location_id, today=day, now=now, tz=tz, live=live)
    png = render.render_png(week, lang=location.language, name=location.name)
    row = week.today_row
    if live:
        caption = chart_texts.live_caption(
            row.off_us, row.count, times.hm(now, tz), location.language
        )
    else:
        caption = chart_texts.finished_caption(row.off_us, row.count, day, location.language)
    return png, caption


def run_step(clock: Clock, state: io_loop.RelayState, stop: threading.Event | None = None) -> bool:
    """Make the one chart call due now, if any; True if a Telegram call was made.

    First the posts kept after a database error are written (``RelayState.chart_posted``);
    a database error there propagates before any step is chosen, so no call is made while
    a kept post is unwritten and no second photo is ever posted for it (WR-04 analogue).
    Then the snapshot and the detection cursor are read (the cursor decides which older
    days have settled for their final edit) and the step keys no longer needed are dropped.

    ``now`` is read once: today's date, the render, its now pill and the caption's update
    time all come from it (CHRT-04, Pitfall 8); the outcome counts from the answer time.
    ``stop`` is checked before the render and again before the call. A render error backs
    off that step only, for 15 min, and makes no call (INV-13 pattern). A database error
    writing the outcome of a call that was made is handled: a post's is kept, and any
    other step waits ``step_delay``, so a write that keeps failing never turns into a call
    per pass.

    Once the step is chosen, a lost connection (OperationalError, InterfaceError)
    propagates: the pass ends and the step is retried on the next one (MON-06). Any other
    error backs off that step only (``_step_crashed``), so the next pass chooses another
    step and one location's error never stops the other locations' charts (INV-13).
    """
    _flush_posted(state)
    now = clock.now()
    tz = settings.CFG.display_tz
    today = model.local_today(now, tz)
    locations, rows = read_snapshot(today)
    settled = settled_records(
        locations, rows, today=today, detected_until=detection_cursor(), tz=tz
    )
    _prune(state, locations, rows, today)
    action = plan(
        locations, rows, today=today, now=now, not_before=state.not_before, settled=settled
    )
    if action is None or _stopped(stop):
        return False
    location = action.location
    key = _key(action)
    # Telegram's answer and its time, once the call returned: an accepted post must then
    # be recorded, never posted again.
    result: SendResult | None = None
    answered: datetime | None = None
    try:
        content: tuple[bytes, str] | None = None
        started = clock.monotonic()
        if action.step in _RENDERED:
            try:
                content = _content(action, today, now, tz)
            except Error:
                raise  # the database, not the chart: handled below
            except Exception as exc:
                # The class only: an exception's text could carry anything.
                state.not_before[key] = now + io_loop.PERMANENT_BACKOFF
                log.error(
                    "chart %s for location %s: render failed (%s); the step waits 15 min",
                    action.step,
                    location.location_id,
                    type(exc).__name__,
                )
                return False
        render_ms = _ms(clock.monotonic() - started)
        if _stopped(stop) or not _lease_holds(state.lease_pid):
            return False
        client = TelegramClient(location.bot_token)
        called = clock.monotonic()
        result = _call(client, action, content)
        call_ms = _ms(clock.monotonic() - called)
        answered = clock.now()
        log.info(
            "chart %s for location %s: %s (%s) render_ms=%d call_ms=%d",
            action.step,
            location.location_id,
            result.kind,
            result.code,
            render_ms,
            call_ms,
        )
        try:
            _apply(action, key, result, today, answered, state)
        except Error as exc:
            _unwritten(action, key, result, answered, state, exc)
        return True
    except OperationalError, InterfaceError:
        # The connection, not the chart: the pass ends and is retried (MON-06). Never
        # after the call: ``_apply``'s database errors are handled above.
        raise
    except Exception:
        at = clock.now() if answered is None else answered
        _step_crashed(action, key, result, today, at, state)
        return result is not None


def _content(action: Action, today: date, now: datetime, tz: str) -> tuple[bytes, str]:
    """The step's render: today's live chart, or an older record's finished day (D-01, D-13).

    A finished day is drawn as of the local midnight that ends it (chart-spec §7), never as
    of the time the final edit happens to run, so a catch-up after downtime draws the same
    day as a final edit made at midnight.
    """
    row = action.row
    if action.step == "finalize" and row is not None:
        end_of_day = model.next_midnight(row.local_date, tz)
        return chart_content(action.location, row.local_date, end_of_day, live=False, tz=tz)
    return chart_content(action.location, today, now, live=True, tz=tz)


def _call(client: TelegramClient, action: Action, content: tuple[bytes, str] | None) -> SendResult:
    """The step's one Telegram call; a record's own chat is the one called (D-04)."""
    row = action.row
    if action.step == "post" and content is not None:
        png, caption = content
        return client.send_photo(action.location.chat_id, png, caption)
    if action.step == "pin" and row is not None:
        return client.pin_chat_message(row.chat_id, row.message_id)
    if action.step in ("refresh", "finalize") and row is not None and content is not None:
        png, caption = content
        return client.edit_message_media(row.chat_id, row.message_id, png, caption)
    if action.step == "unpin" and row is not None:
        # By its message id, in its own chat: never the admin's own pins (D-04, INV-19).
        return client.unpin_chat_message(row.chat_id, row.message_id)
    raise ValueError(f"no call for the chart step {action.step!r}")


def _apply(
    action: Action,
    key: str,
    result: SendResult,
    today: date,
    answered: datetime,
    state: io_loop.RelayState,
) -> None:
    """Write the step's outcome: its record on success, else its own backoff (``_fail``)."""
    row = action.row
    if result.kind == "ok" and action.step == "post" and result.message_id is None:
        # Cannot happen with the client (it answers no_message_id), and must not record.
        result = SendResult("maybe_delivered", code="no_message_id")
    if result.kind == "ok":
        if action.step == "post" and result.message_id is not None:
            _record_or_keep(action.location, today, result.message_id, answered, state)
        elif action.step == "pin" and row is not None:
            _pinned(action.location, row, answered)
        elif action.step == "refresh" and row is not None:
            # "message is not modified" is ok too: the chart shows this render (D-05).
            ChartMessage.objects.filter(pk=row.id, retired_at__isnull=True).update(
                last_rendered_at=answered
            )
        elif action.step == "finalize" and row is not None:
            # Conditional: a repeated or concurrent final edit changes nothing twice.
            ChartMessage.objects.filter(pk=row.id, finalized_at__isnull=True).update(
                finalized_at=answered
            )
        elif action.step == "unpin" and row is not None:
            # Also "not modified": the message was not pinned (its pin never took effect).
            _unpinned(row, answered)
        _step_done(key, state)
        return
    if action.step in _CLEANUP and row is not None and result.kind in _PERMANENT_KINDS:
        _end_cleanup(action, row, result, answered)
        _step_done(key, state)
        return
    if action.step in _TODAYS and row is not None and result.kind == "edit_target_missing":
        # Today's chart was deleted in the channel: the next pass posts one replacement.
        _retire(row, answered)
        log.warning(
            "chart %s for location %s: record %s is gone from the chat; it is retired and "
            "today's chart is posted again",
            action.step,
            action.location.location_id,
            row.id,
        )
        _step_done(key, state)
        return
    if result.kind == "permanent" and action.step == "pin" and row is not None:
        # The bot may not pin here: tried again after the next render, not before (D-07).
        _pin_refused(action.location, row, result, answered)
    _fail(action, key, result, answered, state)


def _pinned(location: ChartLocation, row: ChartRow, answered: datetime) -> None:
    """Today's chart is pinned; a pin failure of the location ends with one notice (D-07).

    One transaction: the record's UPDATE, the incident's close and the recovery notice.
    Only the closer whose UPDATE closed the open incident notifies (INV-20 shape). A
    record pinned again after its unpin (the wall clock stepped back across midnight)
    owes a new unpin, so ``unpinned_at`` is cleared (INV-19).
    """
    location_id = location.location_id
    with transaction.atomic():
        ChartMessage.objects.filter(pk=row.id, pinned=False).update(
            pinned=True, pin_failed_at=None, unpinned_at=None
        )
        incident = (
            OpsIncident.objects.filter(
                kind=KIND_CHART_PIN_FAILED, location_id=location_id, ended_at__isnull=True
            )
            .values_list("id", flat=True)
            .first()
        )
        if incident is not None and ops.close_incident(incident, answered):
            ops.notify(
                outbox.KIND_OPS_PIN_RESTORED,
                payload={},
                recorded_at=answered,
                location_id=location_id,
            )


def _pin_refused(
    location: ChartLocation, row: ChartRow, result: SendResult, answered: datetime
) -> None:
    """The bot may post but not pin (INV-17 #1): one notice when pinning starts failing.

    One transaction: ``pin_failed_at``, the incident's open and the start notice. Only
    the opener that got an incident id back notifies, so a pin refused again after every
    refresh never repeats the notice (D-07). The channel's alerts are never held.
    """
    location_id = location.location_id
    with transaction.atomic():
        ChartMessage.objects.filter(pk=row.id, pinned=False).update(pin_failed_at=answered)
        if ops.open_incident(KIND_CHART_PIN_FAILED, answered, location_id=location_id) is not None:
            ops.notify(
                outbox.KIND_OPS_PIN_FAILED,
                payload={"http_status": _http_status(result.code)},
                recorded_at=answered,
                location_id=location_id,
            )


def _http_status(code: str) -> int:
    """The HTTP status in a client code such as ``http_400``; 400 when it holds none."""
    digits = code.removeprefix("http_")
    if digits.isascii() and digits.isdecimal() and 100 <= int(digits) <= 599:
        return int(digits)
    return _DEFAULT_PIN_STATUS


def _step_done(key: str, state: io_loop.RelayState) -> None:
    """The step is over: its failure count and its spent key are forgotten."""
    state.chart_failures.pop(key, None)
    state.not_before.pop(key, None)


def _end_cleanup(action: Action, row: ChartRow, result: SendResult, answered: datetime) -> None:
    """An older record's final edit or unpin that cannot succeed ends here (INV-19, D-06).

    The message is gone ("message to edit / unpin not found"): a final edit retires the
    record, with no repost, as it is not today's; an unpin marks it unpinned (its one
    unpin is done; a final edit still due finds the message gone and retires it). Any
    other permanent error is best effort: the record is marked finalized or unpinned, with
    one WARNING, so cleanup never loops and never blocks the next step.
    """
    location_id = action.location.location_id
    if action.step == "finalize" and result.kind == "edit_target_missing":
        _retire(row, answered)
        log.warning(
            "chart finalize for location %s: record %s is gone from the chat; it is retired",
            location_id,
            row.id,
        )
    elif action.step == "finalize":
        ChartMessage.objects.filter(pk=row.id, finalized_at__isnull=True).update(
            finalized_at=answered
        )
        log.warning(
            "chart finalize for location %s: permanent error %s for record %s; "
            "it is marked finalized (best effort)",
            location_id,
            result.code,
            row.id,
        )
    else:
        _unpinned(row, answered)
        if result.kind == "permanent":
            log.warning(
                "chart unpin for location %s: permanent error %s for record %s; "
                "it is marked unpinned (best effort)",
                location_id,
                result.code,
                row.id,
            )


def _unpinned(row: ChartRow, answered: datetime) -> None:
    """The older record's one unpin is done (INV-19): not pinned, never unpinned again.

    Conditional: a repeated or concurrent outcome changes nothing twice.
    """
    ChartMessage.objects.filter(pk=row.id, unpinned_at__isnull=True).update(
        pinned=False, unpinned_at=answered
    )


def _retire(row: ChartRow, answered: datetime) -> None:
    """The record's message is gone: retired and unpinned, never called again (D-06)."""
    ChartMessage.objects.filter(pk=row.id, retired_at__isnull=True).update(
        retired_at=answered, pinned=False
    )


def _key(action: Action) -> str:
    """The step's backoff key: by record for a step on one record, else by location."""
    row_id = action.row.id if action.row is not None and action.step != "refresh" else None
    return chart_key(action.location.location_id, action.step, row_id)


def _stopped(stop: threading.Event | None) -> bool:
    return stop is not None and stop.is_set()


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _lease_holds(pid: int | None) -> bool:
    """True when ``pid`` (the lease session) holds the worker lock; no pid is unfenced."""
    if pid is None:
        return True
    with connection.cursor() as cur:
        cur.execute(LEASE_HELD_SQL, {"pid": pid, "classid": _LOCK_CLASSID, "objid": _LOCK_OBJID})
        return cur.fetchone() is not None


def _record_post(
    location_id: int,
    day: date,
    chat_id: int,
    message_id: int,
    answered: datetime,
    bot_key: str,
) -> None:
    """Record the posted chart before any pin (INV-17); a second record for the day is refused.

    ``bot_key`` names the bot that posted it (``io_loop.bot_key`` of its token, D-08).
    A record that can never be written (an IntegrityError: the location row is gone)
    leaves the photo untracked, as a refused second record does, with one WARNING. Any
    other database error propagates: the caller keeps the post and writes it later.
    """
    try:
        with connection.cursor() as cur:
            cur.execute(
                INSERT_SQL,
                {
                    "location_id": location_id,
                    "local_date": day,
                    "chat_id": chat_id,
                    "bot_key": bot_key,
                    "message_id": message_id,
                    "answered": answered,
                },
            )
            inserted = cur.fetchone()
    except IntegrityError:
        inserted = None
    if inserted is None:
        log.warning(
            "chart post for location %s on %s: a record exists or the location is gone; "
            "message %s stays untracked",
            location_id,
            day,
            message_id,
        )


def _record_or_keep(
    location: ChartLocation,
    day: date,
    message_id: int,
    answered: datetime,
    state: io_loop.RelayState,
) -> None:
    """Record an accepted post now, or keep it for the next chart step (WR-04 analogue).

    Telegram answered with the message id, so the photo exists: it must be recorded, never
    posted again. ``location`` is the step's snapshot, so its token is the one that posted
    the photo (D-08). A database error keeps (location, date) -> (chat, message, answer
    time, bot key) in ``state.chart_posted``; ``_flush_posted`` writes it before the next
    step is chosen, with that bot key even if the location's token changed meanwhile.
    """
    posted_by = io_loop.bot_key(location.bot_token)
    try:
        _record_post(location.location_id, day, location.chat_id, message_id, answered, posted_by)
    except Error as exc:
        state.chart_posted[(location.location_id, day)] = (
            location.chat_id,
            message_id,
            answered,
            posted_by,
        )
        log.warning(
            "chart post for location %s: message %s was posted, but its record was not "
            "written (%s); it is written before the next chart step",
            location.location_id,
            message_id,
            type(exc).__name__,
        )


def _flush_posted(state: io_loop.RelayState) -> None:
    """Write every kept post, oldest first; a database error stops and propagates.

    Each entry is removed once written, so a flush cut short resumes where it stopped.
    The kept date is the one the photo was posted for, even after midnight: a post kept
    at 23:59 becomes that day's record, which the next steps finalize.
    """
    for (location_id, day), posted in list(state.chart_posted.items()):
        chat_id, message_id, answered, posted_by = posted
        _record_post(location_id, day, chat_id, message_id, answered, posted_by)
        del state.chart_posted[(location_id, day)]


def _prune(
    state: io_loop.RelayState,
    locations: list[ChartLocation],
    rows: list[ChartRow],
    today: date,
) -> None:
    """Drop the ``chart:`` keys no step needs any more, so both maps stay bounded.

    ``RelayState`` lives as long as the worker. Without this, every record whose step
    failed and was then left behind (a pin refused all day, INV-17 #1) would leave a key
    in ``not_before`` and ``chart_failures`` for good. Only keys that guard no step are
    dropped (``_live_keys``), so the plan never changes; other keys are never touched.
    """
    live = _live_keys(locations, rows, today)
    for keys in (state.not_before, state.chart_failures):
        for key in [k for k in keys if k.startswith(_KEY_PREFIX) and k not in live]:
            del keys[key]


def _live_keys(locations: list[ChartLocation], rows: list[ChartRow], today: date) -> set[str]:
    """Every key that can still guard a step: the steps the snapshot may still make."""
    monitored = {location.location_id for location in locations}
    live: set[str] = set()
    for location_id in monitored:
        today_row = _today_row(rows, location_id, today)
        if today_row is None:
            live.add(chart_key(location_id, "post"))
            continue
        live.add(chart_key(location_id, "refresh"))
        if not today_row.pinned:
            live.add(chart_key(location_id, "pin", today_row.id))
    for row in rows:
        if row.location_id not in monitored or row.local_date >= today:
            continue
        if row.finalized_at is None:
            live.add(chart_key(row.location_id, "finalize", row.id))
        if row.unpinned_at is None:
            live.add(chart_key(row.location_id, "unpin", row.id))
    return live


def _fail(
    action: Action, key: str, result: SendResult, answered: datetime, state: io_loop.RelayState
) -> None:
    """A failed step waits under its own key, counted from the answer (D-02, D-06).

    With n = the step's consecutive failures, this one included:

    - permanent: the fixed PERMANENT_BACKOFF (a refused pin waits for the next render
      anyway, D-07; a refused post or refresh for its next try);
    - rate_limited, transient, not_sent (``BOT_WIDE_KINDS``): the bot is held as an
      alert's outcome would hold it (retry_after capped at MAX_RETRY_AFTER_S, or
      min(2**n, BACKOFF_CAP_S) s), and the step waits ``step_delay(n)`` longer, so the
      location's other steps go first once the hold ends;
    - maybe_delivered: ``step_delay(n)``. A post may exist but is never recorded, pinned
      or edited, and is posted again after the wait (D-06); an edit or pin is idempotent.

    It never writes ``chat_key``: a chart failure must not hold the channel's alerts.
    """
    failures = state.chart_failures.get(key, 0) + 1
    state.chart_failures[key] = failures
    location_id = action.location.location_id
    if result.kind in _PERMANENT_KINDS:
        state.not_before[key] = answered + io_loop.PERMANENT_BACKOFF
        log.warning(
            "chart %s for location %s: permanent error %s; the step waits 15 min",
            action.step,
            location_id,
            result.code,
        )
        return
    hold = _bot_hold(result, failures)
    if hold is not None:
        state.not_before[io_loop.bot_wide_key(action.location.bot_token)] = answered + hold
        state.not_before[key] = answered + hold + step_delay(failures)
        return
    delay = step_delay(failures)
    state.not_before[key] = answered + delay
    if action.step == "post":
        log.warning(
            "chart post for location %s may have been delivered (%s); it is not recorded "
            "and is posted again in %d s (attempt %d)",
            location_id,
            result.code,
            delay // timedelta(seconds=1),
            failures,
        )


def _bot_hold(result: SendResult, failures: int) -> timedelta | None:
    """How long a bot-wide outcome holds the whole bot (``io_loop._apply``), else None."""
    if result.kind not in io_loop.BOT_WIDE_KINDS:
        return None
    if result.kind == "rate_limited":
        wait = min(result.retry_after or DEFAULT_RETRY_AFTER_S, io_loop.MAX_RETRY_AFTER_S)
        return timedelta(seconds=wait)
    return timedelta(seconds=min(2**failures, io_loop.BACKOFF_CAP_S))


def _step_crashed(
    action: Action,
    key: str,
    result: SendResult | None,
    today: date,
    at: datetime,
    state: io_loop.RelayState,
) -> None:
    """An unexpected error once the step was chosen: that step alone waits 15 min (INV-13).

    Without its key the pure ``plan`` would choose the same failing step on every pass,
    and no other location's chart would move. Only the step's own key is set, never
    ``chat_key`` or ``bot_wide_key``: neither the channel's alerts nor the bot's other
    steps wait for it. A post Telegram accepted (``result``) is kept in ``chart_posted``,
    as after a database error, with the key of the bot that posted it (D-08), so the next
    chart step records it and it is never posted again. One ERROR line with the traceback;
    the worker's redacting formatter scrubs any token in it (OPS-08). Called from an
    ``except`` block only.
    """
    location = action.location
    if (
        action.step == "post"
        and result is not None
        and result.kind == "ok"
        and result.message_id is not None
    ):
        posted = (location.chat_id, result.message_id, at, io_loop.bot_key(location.bot_token))
        state.chart_posted.setdefault((location.location_id, today), posted)
    state.not_before[key] = at + io_loop.PERMANENT_BACKOFF
    log.exception(
        "chart %s for location %s failed; the step waits 15 min",
        action.step,
        location.location_id,
    )


def _unwritten(
    action: Action,
    key: str,
    result: SendResult,
    answered: datetime,
    state: io_loop.RelayState,
    exc: Error,
) -> None:
    """The call was made but its record UPDATE could not be written: the step waits.

    For a refresh, pin, final edit or unpin, all idempotent: the call is simply made again
    after ``step_delay``, so an UPDATE that keeps failing never turns into a call per pass.
    A post never gets here: its record is kept and written first (``_record_or_keep``).
    """
    failures = state.chart_failures.get(key, 0) + 1
    state.chart_failures[key] = failures
    delay = step_delay(failures)
    state.not_before[key] = answered + delay
    message_id = result.message_id if action.row is None else action.row.message_id
    log.warning(
        "chart %s for location %s: %s for message %s, but the outcome was not written (%s); "
        "the step waits %d s",
        action.step,
        action.location.location_id,
        result.kind,
        message_id,
        type(exc).__name__,
        delay // timedelta(seconds=1),
    )
