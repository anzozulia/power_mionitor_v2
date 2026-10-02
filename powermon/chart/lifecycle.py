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
  to, the message id) is written right after Telegram accepts the photo and before any pin
  (INV-17), with ``INSERT ... ON CONFLICT DO NOTHING`` on the partial unique index
  ``chart_message_one_active_per_day``, so two workers can never record two charts for a
  day (INV-17 #2).
- Pin: a later pass pins the recorded message silently, in the chat stored with the
  record, never the location's current chat (D-01, D-04). A permanent pin failure (the
  bot may post but not pin, INV-17 #1) is stored as ``pin_failed_at``; the pin is tried
  again only after the next successful render (D-07).
- Finalize: every older record (``local_date`` before today) that has no final edit yet
  gets its finished-day render (chart-spec §7: now = the local midnight that ends its
  day, no now line, no pill, the caption's line 1 only, D-01, D-13), edited in the chat
  and message stored with it (D-04); oldest first. A finalized chart is never rendered
  again (D-14).
- Unpin: every older record still pinned is unpinned by its own message id in its own
  chat, oldest first, and nothing else is ever unpinned (D-04, INV-19).
- Refresh: today's chart is due ``REFRESH_EVERY`` (15 min) after its last successful
  render (``last_rendered_at``, the answer time), and is edited in place in its recorded
  chat; "message is not modified" counts as rendered (D-05, CHRT-02). The state is in the
  database, so after downtime exactly one catch-up refresh is made, not one per missed
  slot (INV-18). Midnight-class steps (post, pin, finalize, unpin) beat any refresh; among
  due refreshes the oldest render goes first, ties to the lower location id.

Each step is its own condition, checked on every pass (D-02): after downtime across one
or more midnights the passes post and pin one chart for today and finalize and unpin
every older chart, and a day the worker missed gets no chart (D-03, INV-18, INV-19).

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
errors wait a fixed 15 min. Nothing here sleeps.

Each call logs one INFO line, ``chart <step> for location <id>: <kind> (<code>)
render_ms=<n> call_ms=<n>``, with no token and no Telegram description (OPS-08).
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from django.conf import settings
from django.db import Error, connection

from powermon.chart import model, source
from powermon.chart.models import ChartMessage
from powermon.clock import Clock
from powermon.i18n import chart_texts, times
from powermon.telegram.client import DEFAULT_RETRY_AFTER_S, SendResult, TelegramClient
from powermon.worker import io_loop
from powermon.worker.lease import LOCK_KEY

log = logging.getLogger(__name__)

Step = Literal["post", "pin", "finalize", "unpin", "refresh"]

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
# Outcomes that end a step's tries for now with the fixed 15-min wait. "message to edit
# not found" waits too until 03-09 retires the record.
_PERMANENT_KINDS = ("permanent", "edit_target_missing")

LOCATIONS_SQL = """
SELECT l.id, l.name, l.language, l.bot_token, l.chat_id
  FROM location l
  JOIN location_state s ON s.location_id = l.id
 WHERE s.status IN ('on', 'off') AND l.deleted_at IS NULL
 ORDER BY l.id
"""
# Today's active records, and older ones that still need their final edit or an unpin.
ROWS_SQL = """
SELECT id, location_id, local_date, chat_id, message_id, pinned, pin_failed_at,
       last_rendered_at, finalized_at
  FROM chart_message
 WHERE retired_at IS NULL
   AND (local_date = %(today)s OR (local_date < %(today)s AND (finalized_at IS NULL OR pinned)))
 ORDER BY local_date, id
"""
# The record of a posted chart, written right after the send succeeded (INV-17). A second
# active record for the same day is refused by chart_message_one_active_per_day.
INSERT_SQL = """
INSERT INTO chart_message (location_id, local_date, chat_id, message_id, pinned,
                           pin_failed_at, last_rendered_at, finalized_at, retired_at,
                           created_at)
VALUES (%(location_id)s, %(local_date)s, %(chat_id)s, %(message_id)s, false,
        NULL, %(answered)s, NULL, NULL, %(answered)s)
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


@dataclass(frozen=True)
class Action:
    """The one chart step a pass makes: the step, its location and its record (if any)."""

    step: Step
    location: ChartLocation
    row: ChartRow | None = None


def chart_key(location_id: int, step: Step, row_id: int | None = None) -> str:
    """A chart step's own backoff key in ``RelayState.not_before``: by location, never a token."""
    key = f"chart:{location_id}:{step}"
    return key if row_id is None else f"{key}:{row_id}"


def step_delay(failures: int) -> timedelta:
    """A step's own wait after its ``failures``-th consecutive failure (D-02, D-06).

    30 s after the first, doubling with each further one, at most 15 min: 30 s, 60 s, 2,
    4, 8 min, then 15 min. ValueError for ``failures`` below 1.
    """
    if failures < 1:
        raise ValueError(f"step_delay() needs at least 1 failure, not {failures}")
    return min(STEP_RETRY * 2 ** min(failures - 1, _MAX_DOUBLINGS), STEP_RETRY_MAX)


def read_snapshot(today: date) -> tuple[list[ChartLocation], list[ChartRow]]:
    """The monitored locations by id, and the records ``plan`` may act on, oldest first."""
    with connection.cursor() as cur:
        cur.execute(LOCATIONS_SQL)
        locations = [
            ChartLocation(location_id=r[0], name=r[1], language=r[2], bot_token=r[3], chat_id=r[4])
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
) -> Action | None:
    """The one chart step due now, or None. Pure: reads nothing but its arguments.

    Midnight-class steps go first: locations by ascending id, and the first one with a due
    step wins, so one location's midnight work is done before the next one's starts.
    Within a location the steps go in D-02 order: post today's chart, pin it, give the
    oldest older record without one its final edit, unpin the oldest older record still
    pinned. Only when none is due anywhere, the refresh that has waited longest goes:
    today's record with the oldest ``last_rendered_at`` at least ``REFRESH_EVERY`` ago,
    ties to the lower location id (CHRT-02). A step whose own key in ``not_before`` is in
    the future is skipped, so the next due step goes instead: a failing post never blocks
    the older charts' cleanup (D-02, INV-19). The alert relay's backoff is respected, read
    only: a location whose bot waits (``bot_wide_key``) makes no step, and a step whose
    channel waits (``chat_key`` of the chat it would call) is skipped (D-06).
    """

    def waiting(key: str) -> bool:
        return not_before.get(key, now) > now

    due_refreshes: list[tuple[datetime, int, Action]] = []
    for location in locations:
        if waiting(io_loop.bot_wide_key(location.bot_token)):
            continue
        today_row = _today_row(rows, location.location_id, today)
        action = _midnight_step(location, rows, today_row, today, waiting)
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
) -> Action | None:
    """The location's first due step of D-02 (post, pin, finalize, unpin), or None."""
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
        if row.finalized_at is None and _free(location, "finalize", row, waiting):
            return Action("finalize", location, row)
    for row in older:
        if row.pinned and _free(location, "unpin", row, waiting):
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

    ``now`` is read once: today's date, the render, its now pill and the caption's update
    time all come from it (CHRT-04, Pitfall 8); the outcome counts from the answer time.
    ``stop`` is checked before the render and again before the call. A render error backs
    off that step only, for 15 min, and makes no call (INV-13 pattern). A database error
    propagates, except one writing the outcome of a call that was made: that step then
    waits ``step_delay``, so a write that keeps failing never turns into a call per pass.
    """
    now = clock.now()
    tz = settings.CFG.display_tz
    today = model.local_today(now, tz)
    locations, rows = read_snapshot(today)
    action = plan(locations, rows, today=today, now=now, not_before=state.not_before)
    if action is None or _stopped(stop):
        return False
    location = action.location
    key = _key(action)
    content: tuple[bytes, str] | None = None
    started = clock.monotonic()
    if action.step in _RENDERED:
        try:
            content = _content(action, today, now, tz)
        except Error:
            raise  # the database, not the chart: the pass ends and is retried (MON-06)
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
            _record_post(action.location, today, result.message_id, answered)
        elif action.step == "pin" and row is not None:
            ChartMessage.objects.filter(pk=row.id, pinned=False).update(
                pinned=True, pin_failed_at=None
            )
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
            ChartMessage.objects.filter(pk=row.id, pinned=True).update(pinned=False)
        # The step is done: its failures and its spent key are forgotten.
        state.chart_failures.pop(key, None)
        state.not_before.pop(key, None)
        return
    if result.kind == "permanent" and action.step == "pin" and row is not None:
        # The bot may not pin here: tried again after the next render, not before (D-07).
        ChartMessage.objects.filter(pk=row.id, pinned=False).update(pin_failed_at=answered)
    _fail(action, key, result, answered, state)


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


def _record_post(location: ChartLocation, day: date, message_id: int, answered: datetime) -> None:
    """Record the posted chart before any pin (INV-17); a second record for the day is refused."""
    with connection.cursor() as cur:
        cur.execute(
            INSERT_SQL,
            {
                "location_id": location.location_id,
                "local_date": day,
                "chat_id": location.chat_id,
                "message_id": message_id,
                "answered": answered,
            },
        )
        inserted = cur.fetchone()
    if inserted is None:
        log.warning(
            "chart post for location %s on %s: a record exists; message %s stays untracked",
            location.location_id,
            day,
            message_id,
        )


def _fail(
    action: Action, key: str, result: SendResult, answered: datetime, state: io_loop.RelayState
) -> None:
    """A failed step waits under its own key, counted from the answer (D-02, D-06).

    With n = the step's consecutive failures, this one included:

    - permanent (and, until 03-09, edit_target_missing): the fixed PERMANENT_BACKOFF;
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


def _unwritten(
    action: Action,
    key: str,
    result: SendResult,
    answered: datetime,
    state: io_loop.RelayState,
    exc: Error,
) -> None:
    """The call was made but its outcome could not be written: the step waits.

    A posted photo then stays untracked (never pinned or edited), as after an ambiguous
    post, and is posted again after ``step_delay``; so a record that keeps failing to be
    written gives at most a few photos per hour, never one per pass (D-06).
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
