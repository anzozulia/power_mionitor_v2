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
- Refresh: today's chart is due ``REFRESH_EVERY`` (15 min) after its last successful
  render (``last_rendered_at``, the answer time), and is edited in place in its recorded
  chat; "message is not modified" counts as rendered (D-05, CHRT-02). The state is in the
  database, so after downtime exactly one catch-up refresh is made, not one per missed
  slot (INV-18). Midnight-class steps (post, pin) beat any refresh; among due refreshes
  the oldest render goes first, ties to the lower location id.

Rendering runs inline in the I/O thread right before its call (D-05), with the location's
current name and language and the display time zone read at render time (D-14). The
render module (Pillow) is imported inside ``chart_content`` only, so the web process,
which imports the app's models, never loads Pillow.

Before each call the worker checks that its lease session (``RelayState.lease_pid``) still
holds the worker lock, with the same ``pg_locks`` predicate as the outbox claim; a stale
worker makes no chart call (C1).

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
from django.db import connection

from powermon.chart import model, source
from powermon.chart.models import ChartMessage
from powermon.clock import Clock
from powermon.i18n import chart_texts, times
from powermon.telegram.client import SendResult, TelegramClient
from powermon.worker import io_loop
from powermon.worker.lease import LOCK_KEY

log = logging.getLogger(__name__)

Step = Literal["post", "pin", "finalize", "unpin", "refresh"]

# Today's chart is due for a refresh this long after its last successful render (D-05).
REFRESH_EVERY = timedelta(minutes=15)
# The first wait after a failed step.
STEP_RETRY = timedelta(seconds=io_loop.BACKOFF_CAP_S)
# The steps that render a chart right before their call.
_RENDERED: tuple[Step, ...] = ("post", "refresh")

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
    step wins; within a location the steps go in D-02 order (post today's chart, then pin
    it). Only when none is due anywhere, the refresh that has waited longest goes: today's
    record with the oldest ``last_rendered_at`` at least ``REFRESH_EVERY`` ago, ties to
    the lower location id (CHRT-02). A step whose own key in ``not_before`` is in the
    future is skipped, so the next due step goes instead.
    """

    def waiting(key: str) -> bool:
        return not_before.get(key, now) > now

    due_refreshes: list[tuple[datetime, int, Action]] = []
    for location in locations:
        today_row = _today_row(rows, location.location_id, today)
        action = _midnight_step(location, today_row, waiting)
        if action is not None:
            return action
        if (
            today_row is not None
            and now - today_row.last_rendered_at >= REFRESH_EVERY
            and not waiting(chart_key(location.location_id, "refresh"))
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
    location: ChartLocation, today_row: ChartRow | None, waiting: Callable[[str], bool]
) -> Action | None:
    """The location's first due step of D-02 (post, pin), or None."""
    location_id = location.location_id
    if today_row is None:
        if not waiting(chart_key(location_id, "post")):
            return Action("post", location)
    elif _pin_due(today_row) and not waiting(chart_key(location_id, "pin", today_row.id)):
        return Action("pin", location, today_row)
    return None


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
    """Make the one chart call due now, if any; True if a Telegram call was made."""
    now = clock.now()
    tz = settings.CFG.display_tz
    today = model.local_today(now, tz)
    locations, rows = read_snapshot(today)
    action = plan(locations, rows, today=today, now=now, not_before=state.not_before)
    if action is None or _stopped(stop):
        return False
    location = action.location
    content: tuple[bytes, str] | None = None
    started = clock.monotonic()
    if action.step in _RENDERED:
        content = chart_content(location, today, now, live=True, tz=tz)
    render_ms = _ms(clock.monotonic() - started)
    if not _lease_holds(state.lease_pid):
        return False
    client = TelegramClient(location.bot_token)
    called = clock.monotonic()
    result = _call(client, action, content)
    call_ms = _ms(clock.monotonic() - called)
    answered = clock.now()
    _apply(action, result, today, answered, state)
    log.info(
        "chart %s for location %s: %s (%s) render_ms=%d call_ms=%d",
        action.step,
        location.location_id,
        result.kind,
        result.code,
        render_ms,
        call_ms,
    )
    return True


def _call(client: TelegramClient, action: Action, content: tuple[bytes, str] | None) -> SendResult:
    """The step's one Telegram call; a record's own chat is the one called (D-04)."""
    row = action.row
    if action.step == "post" and content is not None:
        png, caption = content
        return client.send_photo(action.location.chat_id, png, caption)
    if action.step == "pin" and row is not None:
        return client.pin_chat_message(row.chat_id, row.message_id)
    if action.step == "refresh" and row is not None and content is not None:
        png, caption = content
        return client.edit_message_media(row.chat_id, row.message_id, png, caption)
    raise ValueError(f"no call for the chart step {action.step!r}")


def _apply(
    action: Action,
    result: SendResult,
    today: date,
    answered: datetime,
    state: io_loop.RelayState,
) -> None:
    """Write the step's outcome: its record on success, else its own backoff key."""
    key = _key(action)
    row = action.row
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
        state.not_before.pop(key, None)
        return
    if result.kind == "permanent" and action.step == "pin" and row is not None:
        # The bot may not pin here: tried again after the next render, not before (D-07).
        ChartMessage.objects.filter(pk=row.id, pinned=False).update(pin_failed_at=answered)
    _back_off(action, key, result, answered, state)


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


def _back_off(
    action: Action, key: str, result: SendResult, answered: datetime, state: io_loop.RelayState
) -> None:
    """A failed step waits under its own key; it never writes the channel's ``chat_key``."""
    if result.kind == "permanent":
        state.not_before[key] = answered + io_loop.PERMANENT_BACKOFF
        log.warning(
            "chart %s for location %s: permanent error %s; the step waits 15 min",
            action.step,
            action.location.location_id,
            result.code,
        )
        return
    state.not_before[key] = answered + STEP_RETRY
