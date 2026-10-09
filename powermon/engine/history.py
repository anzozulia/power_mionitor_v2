"""History corrections (Phase 5, DATA-02, DATA-03): the recent outages, the removal of a
false one and the reset of a location's whole history.

The list reads the stored timeline the way the chart does (KD1): only ``power_interval``,
never heartbeats or the outbox. An outage is the group of off pieces that share one
``outage_start_at`` (chart-spec §8 count rule, D-01), so an outage split by not-monitored
time is one outage. The location page lists every outage that overlaps the last
``WINDOW_DAYS`` local days, newest start first, plus the current outage whatever its age.

The removal (``remove_outage``) is one transaction on Django's connection, like every
timeline writer:

1. the location's ``location_state`` row lock first (``transitions.LOCK_SQL``, imported,
   never copied), so a removal serializes with heartbeats, ``mark_off``, the lapse carve
   and the maintenance toggle; then the deleted check (``transitions.DELETED_SQL``);
2. the in-progress refusal, decided from the row the lock returned (status off with that
   ``outage_started_at``, also while its open piece is not monitored), never from a value
   read before the transaction, so a stale tab or a hand-made POST is refused too
   (INV-07 #3, D-02);
3. while an attempt to send one of the outage's alerts is in flight (status "sending"), the
   removal is deferred with nothing written: "sending" is not final, because a failed
   attempt (not sent, 429, 4xx, 5xx) puts the row back to "pending", and the removed
   outage would then be announced late. That is its OFF alert (D-04 wave-1 audit
   amendment, W1-A1) or its ON alert matched by ``outbox.restores`` (wave-2 audit, W2-A1:
   when the OFF alert expired or was never queued, the ON alert is its location's head and
   can be sending itself). The attempt settles within seconds, and the next removal decides;
4. each off piece of the outage becomes ``on`` through ``timeline.overwrite``, one piece at
   a time: not-monitored time inside the outage stays not monitored (D-02);
5. if the outage is the one that last turned the location on (status on, no later off
   piece, and no on piece starting between its end and ``on_since``, which rules out a
   FIRST after a DB restore unless the outage was still in progress at the dump; that
   corner rewinds across the restore gap, D-6), ``on_since`` goes back to where it would be
   had the outage never been recorded. That is the end of the latest remaining off piece
   before it, or else the start of the stored history (``transitions.REWIND_ON_SINCE_SQL``,
   imported, never copied; quick task 261007-llg). Not-monitored pieces are ignored, as
   lapses never move ``on_since``. The UPDATE bumps ``state_version`` only when it rewinds,
   so a detector snapshot read before the removal loses its OFF CAS (INV-01); otherwise it
   changes no row;
6. the outage's alerts are settled (``_settle_alerts``, D-04 as refined by the maintainer
   on 2026-10-03 and amended by quick task 261006-qv7): when an OFF alert of the outage
   cannot be deleted (uncertain, sent before message ids were stored, or sent more than 47
   h ago), nothing is dropped or deleted and a queued ON alert is still sent, so the
   channel is never left at "power off";
7. otherwise its queued alerts are dropped, and its sent alerts get a delete request
   (``delete_requested_at``), which the worker's delete step carries out, OFF first;
8. today's active chart record of the location is marked for a redraw
   (``MARK_REDRAW_SQL``): the worker redraws today's chart within seconds, at the time of
   its last update (D-03 as amended by 261006-qv7).

Apart from that one conditional UPDATE, the removal never touches ``location_state``. The
status, the last heartbeat, the outage start and the window start stay as they are
(INV-07 as amended by 261007-llg), so detection carries on unchanged, and the next OFF
alert's "was ON for" counts from the rewound ``on_since``. A later outage's OFF alert that
is already queued keeps the value it was recorded with (261007-llg D-7). It queues no new
message (no subscriber message, no ops notice): it only asks the worker, through rows
written in its transaction, to delete the outage's alerts and to redraw today's chart
(KD2, KD3), and logs one INFO line. The lock order is location_state, outbox_message,
power_interval, chart_message. Adjacent ``on`` pieces left by a removal are not merged:
the chart sums pieces by state, so they read as one span.

The reset (``reset_history``, DATA-03) is one transaction under the same row lock, then the
deleted check:

1. it is refused while the locked status is off, in maintenance or not (D-06): the
   subscribers got that outage's OFF alert, and after a reset the next heartbeat restarts
   silently, so its ON alert would never be sent;
2. a location with no stored interval has nothing to reset: nothing is written, not even
   a ``state_version`` bump (UI5-D9), so a double submit changes nothing;
3. otherwise every ``power_interval`` row of the location is deleted, a real delete with
   no undo, and ``transitions.WAITING_SQL`` (imported, never copied; the post-restore
   restart shares it) sets the location back to "waiting for first heartbeat" with the
   four times NULL and ``state_version`` bumped, so a detector snapshot read before the
   reset loses its OFF CAS (D-05). The next heartbeat takes the FIRST gate: on, no alert
   (MON-01, K-1);
4. the location's active, unmarked chart records get ``history_reset_at = now``. The
   worker releases each marked record on its next I/O pass (unpin by its own message id
   in its stored chat, then retire; ``powermon.chart.lifecycle``, D-08). The web never
   retires a record itself: a record retired here would leave the snapshot without ever
   being unpinned (INV-19).

The reset keeps the configuration, the device key, the three switches, an open
``delivery_failing``, ``chart_failing`` or ``chart_pin_failed`` incident and every alert
already queued: they report real events (D-05, D-07). It queues nothing, deletes no message
(owner default 2, 261006-qv7) and logs one INFO line.

Nothing here does network I/O (KD2), and time always comes from the caller (``now``, and
for the removal also the display zone ``tz`` that names today's chart), never from SQL
``now()``. Parameters go in as ``%(name)s`` / ``%s`` placeholders, never
formatted into the SQL.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.db import connection, transaction

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart import model
from powermon.engine import timeline
from powermon.engine.transitions import DELETED_SQL, LOCK_SQL, REWIND_ON_SINCE_SQL, WAITING_SQL

log = logging.getLogger(__name__)

ONE_US = timedelta(microseconds=1)
# The list covers today and the 13 local days before it (D-01, UI-SPEC A1).
WINDOW_DAYS = 14
# ``last_error`` of a subscriber alert dropped because its outage was removed (D-04).
OUTAGE_REMOVED = outbox.OUTAGE_REMOVED
# The ON alerts a removal reads: queued, in flight (defers it) or sent (may be deleted).
ON_STATUSES = ("pending", "sending", "sent")
# An attempt to send one of the outage's alerts is in flight, its OFF alert (W1-A1) or its
# matched ON alert (W2-A1): the removal is deferred.
IN_FLIGHT = "sending"

RemoveResult = Literal["removed", "gone", "in_progress", "sending"]
ResetResult = Literal["reset", "in_progress", "nothing", "gone"]

# Does the location have any stored interval at all? "No power history yet" otherwise
# (UI5-D10), and nothing to reset (UI5-D9).
HAS_HISTORY_SQL = "SELECT EXISTS (SELECT 1 FROM power_interval WHERE location_id = %s)"

# The reset's real delete of the location's whole timeline (D-05).
DELETE_HISTORY_SQL = "DELETE FROM power_interval WHERE location_id = %(id)s"

# The reset marker on the location's active records that are not marked yet (D-08). The
# worker unpins each marked record in its stored chat and only then retires it.
MARK_CHARTS_SQL = """
UPDATE chart_message SET history_reset_at = %(now)s
 WHERE location_id = %(id)s AND retired_at IS NULL AND history_reset_at IS NULL
"""

# Every off piece of every outage that has a piece ending after ``since`` (or still open),
# or that is the current outage, oldest outage first. The outer query fetches all pieces of
# those outages, so an outage that started before the window keeps its real start and its
# full off time. With no current outage ``current`` is NULL, and ``= NULL`` is never true.
OUTAGE_PIECES_SQL = """
SELECT start_at, end_at, outage_start_at
  FROM power_interval
 WHERE location_id = %(id)s AND state = 'off'
   AND outage_start_at IN (
       SELECT outage_start_at FROM power_interval
        WHERE location_id = %(id)s AND state = 'off'
          AND (end_at IS NULL OR end_at > %(since)s OR outage_start_at = %(current)s))
 ORDER BY outage_start_at, start_at
"""

# Today's active chart record of the location is redrawn by the worker (261006-qv7 D3). A
# record a history reset marked is released, never redrawn.
MARK_REDRAW_SQL = """
UPDATE chart_message SET redraw_requested_at = %(now)s
 WHERE location_id = %(id)s AND local_date = %(today)s
   AND retired_at IS NULL AND history_reset_at IS NULL
"""

# The off pieces of one outage of the location, oldest first.
OUTAGE_SQL = """
SELECT start_at, end_at
  FROM power_interval
 WHERE location_id = %(id)s AND state = 'off' AND outage_start_at = %(start)s
 ORDER BY start_at
"""

# The live status, read without a lock: for the page and the confirmation (GET) only. The
# removal itself decides from LOCK_SQL's row under the lock.
STATE_SQL = "SELECT status, outage_started_at FROM location_state WHERE location_id = %s"


@dataclass(frozen=True)
class Outage:
    """One outage: the off pieces that share ``start`` (their ``outage_start_at``).

    ``end`` is the end of its last off piece, None only while it is in progress. ``off_us``
    is the summed length of its off pieces in integer microseconds, an open piece counted
    up to now: not-monitored time inside it is left out (UI5-D3).
    """

    start: datetime
    end: datetime | None
    off_us: int
    in_progress: bool


@dataclass(frozen=True)
class RecentOutages:
    """The location page's Recent outages: the outages, newest first, and whether the
    location has any stored interval at all (the two empty states, UI5-D10)."""

    outages: tuple[Outage, ...]
    has_history: bool


def _aware(now: datetime) -> None:
    if now.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")


def window_start(now: datetime, tz: str) -> datetime:
    """The UTC instant of local midnight ``WINDOW_DAYS - 1`` days before ``now``'s local date.

    Computed in local days with the chart's DST-safe helpers, never ``now - 14 x 24 h``: a
    23 h or 25 h day in the window would move the start by an hour (RESEARCH Pitfall 6).
    ValueError for a naive ``now``.
    """
    _aware(now)
    first_day = model.local_today(now, tz) - timedelta(days=WINDOW_DAYS - 1)
    return model.day_bounds(first_day, tz)[0]


def group_outages(
    pieces: Iterable[tuple[datetime, datetime | None, datetime]],
    *,
    now: datetime,
    current: datetime | None,
) -> list[Outage]:
    """Group off pieces ``(start_at, end_at, outage_start_at)`` into outages, newest first.

    Pure. One ``Outage`` per ``outage_start_at``. It is in progress when its start is
    ``current`` (the stored status is off with that outage start) or when one of its pieces
    is still open; an outage in progress has no end. ``off_us`` counts an open piece up to
    ``now`` and never goes negative. ValueError for a naive ``now``.
    """
    _aware(now)
    groups: dict[datetime, list[tuple[datetime, datetime | None]]] = {}
    for start_at, end_at, outage_start_at in pieces:
        groups.setdefault(outage_start_at, []).append((start_at, end_at))
    outages = []
    for start, spans in groups.items():
        open_piece = any(end is None for _start, end in spans)
        in_progress = start == current or open_piece
        off_us = sum(
            (max(now if end is None else end, piece_start) - piece_start) // ONE_US
            for piece_start, end in spans
        )
        ends = [end for _start, end in spans if end is not None]
        end = None if in_progress or not ends else max(ends)
        outages.append(Outage(start=start, end=end, off_us=off_us, in_progress=in_progress))
    outages.sort(key=lambda outage: outage.start, reverse=True)
    return outages


def _current_outage(row: tuple[str, datetime | None] | None) -> datetime | None:
    """The current outage's start from a ``(status, outage_started_at)`` row, or None."""
    if row is None or row[0] != "off":
        return None
    return row[1]


def recent_outages(location_id: int, now: datetime, tz: str) -> RecentOutages:
    """The location's outages that overlap the last ``WINDOW_DAYS`` local days, at ``now``.

    Read-only. The current outage is listed whatever its age, with no end. ``tz`` is the
    display time zone's IANA name. ValueError for a naive ``now``.
    """
    since = window_start(now, tz)
    with connection.cursor() as cur:
        cur.execute(STATE_SQL, [location_id])
        current = _current_outage(cur.fetchone())
        cur.execute(OUTAGE_PIECES_SQL, {"id": location_id, "since": since, "current": current})
        pieces = cur.fetchall()
        cur.execute(HAS_HISTORY_SQL, [location_id])
        exists = cur.fetchone()
    outages = group_outages(pieces, now=now, current=current)
    return RecentOutages(outages=tuple(outages), has_history=bool(exists and exists[0]))


def has_history(location_id: int) -> bool:
    """True when the location has at least one stored interval (read-only, no lock).

    For the page and the reset confirmation (GET) only: the reset itself decides under the
    row lock (UI5-D9).
    """
    with connection.cursor() as cur:
        cur.execute(HAS_HISTORY_SQL, [location_id])
        exists = cur.fetchone()
    return bool(exists and exists[0])


def find_outage(location_id: int, outage_start: datetime, now: datetime) -> Outage | None:
    """The location's outage that starts at ``outage_start``, or None when it has none.

    Read-only, for the removal's confirmation page: any outage of the location, inside the
    list's window or not (UI5-D14). ValueError for a naive ``now``.
    """
    _aware(now)
    with connection.cursor() as cur:
        cur.execute(OUTAGE_SQL, {"id": location_id, "start": outage_start})
        rows = cur.fetchall()
        if not rows:
            return None
        cur.execute(STATE_SQL, [location_id])
        current = _current_outage(cur.fetchone())
    pieces = [(start, end, outage_start) for start, end in rows]
    [outage] = group_outages(pieces, now=now, current=current)
    return outage


def remove_outage(
    location_id: int, outage_start: datetime, *, now: datetime, tz: str
) -> RemoveResult:
    """Remove the location's outage that starts at ``outage_start`` (DATA-02, D-02, D-04).

    One transaction under the location's row lock (see the module docstring), at ``now``
    (the caller's clock); ``tz`` is the display zone, which names today's chart. Returns:

    - "removed": every off piece of the outage is ``on`` now, not-monitored pieces inside
      it are kept, its alerts are settled (``_settle_alerts``: queued ones dropped and sent
      ones requested for deletion, unless its OFF alert cannot be deleted), today's chart
      record is marked for a redraw and, when it is the outage that last turned the
      location on, ``on_since`` is rewound (``REWIND_ON_SINCE_SQL``);
    - "gone", with nothing written: the location is unknown or deleted, or it has no off
      piece with that outage start (a double click, a second tab, a reset in between, a
      hand-made start; UI5-D8);
    - "in_progress", with nothing written: it is the location's current outage, decided
      under the lock (INV-07 #3), or one of its pieces is still open;
    - "sending", with nothing written: an attempt to send one of the outage's alerts is in
      flight, its OFF alert (W1-A1) or an ON alert matched to it by ``outbox.restores``
      (W2-A1). Checked under the lock, after the checks above and before any timeline
      write. The next removal decides from the settled rows.

    "gone", "in_progress" and "sending" write nothing at all: no request and no mark.
    ``location_state`` is written only by that rewind. A restore keeps the delete requests
    (the requests in a dump match that dump's timeline) and re-tags a removed outage's
    dropped ON "restored" while its OFF's delete is unsettled
    (``restore.KEEP_REMOVED_ON_DROPPED_SQL``), so a refused delete after the restore never
    sends it. ValueError for a naive ``outage_start`` or ``now``.
    """
    _aware(outage_start)
    _aware(now)
    with transaction.atomic(), connection.cursor() as cur:
        # The row lock first, as every timeline writer takes it.
        cur.execute(LOCK_SQL, [location_id])
        locked = cur.fetchone()
        if locked is None:
            return "gone"
        cur.execute(DELETED_SQL, [location_id])
        deleted = cur.fetchone()
        if deleted is None or bool(deleted[0]):
            return "gone"
        status, outage_started_at = locked
        if status == "off" and outage_started_at == outage_start:
            # D-02: also while its open piece is not monitored (maintenance, a lapse).
            return "in_progress"
        cur.execute(OUTAGE_SQL, {"id": location_id, "start": outage_start})
        pieces = cur.fetchall()
        if not pieces:
            return "gone"
        if any(end is None for _start, end in pieces):
            # Defensive: an open off piece always belongs to the current outage.
            return "in_progress"
        offs = _off_alerts(location_id, outage_start)
        ons = _on_alerts(location_id, outage_start)
        if any(row.status == IN_FLIGHT for row in (*offs, *ons)):
            # Before any write: a failed attempt would put the row back to pending. The OFF
            # (W1-A1), or the ON, the head when the OFF expired or was never queued (W2-A1).
            return "sending"
        # One piece at a time: a single overwrite across the whole outage would also turn
        # its not-monitored pieces into on (D-02).
        for start, end in pieces:
            timeline.overwrite(cur, location_id, start, end, "on")
        # The 261007-llg on_since rewind: a no-op unless this outage last turned it on.
        outage_end = max(piece_end for _start, piece_end in pieces)
        cur.execute(
            REWIND_ON_SINCE_SQL, {"id": location_id, "start": outage_start, "end": outage_end}
        )
        dropped, requested = _settle_alerts(offs, ons, now)
        today = model.local_today(now, tz)
        cur.execute(MARK_REDRAW_SQL, {"id": location_id, "today": today, "now": now})
    # Ids, a time and counts only: never a key or a token (OPS-08).
    log.info(
        "outage %s of location %s removed, %s queued alert(s) dropped, "
        "%s alert message(s) to delete",
        outage_start.isoformat(),
        location_id,
        dropped,
        requested,
    )
    return "removed"


def _off_alerts(location_id: int, outage_start: datetime) -> list[OutboxMessage]:
    """The outage's OFF alerts (subscriber, power_off, ``event_at`` = the outage start).

    Read ``FOR UPDATE`` in the removal's transaction: a relay claim that commits meanwhile
    is seen, and a claim that comes later waits for the removal and then finds the row no
    longer pending.
    """
    return list(
        OutboxMessage.objects.select_for_update().filter(
            channel=outbox.CHANNEL_SUBSCRIBER,
            location_id=location_id,
            kind=outbox.KIND_POWER_OFF,
            event_at=outage_start,
        )
    )


def _on_alerts(location_id: int, outage_start: datetime) -> list[OutboxMessage]:
    """The outage's ON alerts a removal acts on: the location's subscriber power_on rows in
    "pending", "sending" or "sent" that ``outbox.restores`` matches to the outage that
    starts at ``outage_start``.

    Read ``FOR UPDATE`` in the removal's transaction, like ``_off_alerts``. A pending row
    may be dropped, a sending one defers the removal and a sent one may be deleted. An
    uncertain, expired or dropped ON row is final and is not read: an uncertain ON stays in
    the channel. A row dated before the outage start never matches.
    """
    rows = OutboxMessage.objects.select_for_update().filter(
        channel=outbox.CHANNEL_SUBSCRIBER,
        location_id=location_id,
        kind=outbox.KIND_POWER_ON,
        status__in=ON_STATUSES,
        event_at__gte=outage_start,
    )
    return [row for row in rows if outbox.restores(row, outage_start)]


def _deletable(row: OutboxMessage, now: datetime) -> bool:
    """True when the worker can still delete the sent alert ``row`` (261006-qv7 D2).

    Sent, with the chat and Telegram's message id stored (alerts sent before this release
    have none), and sent within ``outbox.DELETE_REQUEST_WINDOW`` (47 h) of ``now``, which
    leaves an hour before Telegram's 48 h limit.
    """
    return (
        row.status == "sent"
        and row.tg_chat_id is not None
        and row.tg_message_id is not None
        and row.sent_at is not None
        and row.sent_at > now - outbox.DELETE_REQUEST_WINDOW
    )


def _settle_alerts(
    offs: list[OutboxMessage], ons: list[OutboxMessage], now: datetime
) -> tuple[int, int]:
    """Drop the removed outage's queued alerts and request the deletion of its sent ones.

    Returns (dropped, requested). ``offs`` and ``ons`` are the outage's rows from
    ``_off_alerts`` and ``_on_alerts``, read ``FOR UPDATE`` earlier in the same
    transaction, so their statuses are still current. A row still "sending" never reaches
    this step: ``remove_outage`` defers the removal first (W1-A1, W2-A1).

    The channel is never left at "power off" (D-04 as refined on 2026-10-03, owner default
    3 of 261006-qv7): when an OFF alert of the outage went out, or may have, and cannot be
    deleted (uncertain; sent before message ids were stored; sent more than 47 h ago),
    nothing is dropped or requested and its queued ON alert is still sent. Otherwise the
    outage's pending OFF and ON alerts become "dropped" with last_error "outage_removed",
    exactly as before, so a false outage is never announced late, and each of its sent
    alerts that can be deleted gets ``delete_requested_at = now``. Each write is
    conditional (status "pending"; status "sent" and no request yet). Alerts of other
    outages are never touched.
    """
    if any(
        row.status == "uncertain" or (row.status == "sent" and not _deletable(row, now))
        for row in offs
    ):
        return 0, 0
    dropped = 0
    pending = [row.pk for row in (*offs, *ons) if row.status == "pending"]
    if pending:
        dropped = OutboxMessage.objects.filter(pk__in=pending, status="pending").update(
            status="dropped", last_error=OUTAGE_REMOVED
        )
    requested = 0
    deletable = [row.pk for row in (*offs, *ons) if _deletable(row, now)]
    if deletable:
        requested = OutboxMessage.objects.filter(
            pk__in=deletable, status="sent", delete_requested_at__isnull=True
        ).update(delete_requested_at=now)
    return dropped, requested


def reset_history(location_id: int, now: datetime) -> ResetResult:
    """Reset the location's history at ``now`` (DATA-03; D-05, D-06, D-07, D-08, UI5-D9).

    One transaction under the location's row lock (see the module docstring). Returns:

    - "reset": every interval of the location is deleted, it waits for its first heartbeat
      (``WAITING_SQL``, ``state_version`` bumped) and its active chart records are marked
      for the worker's release;
    - "in_progress", with nothing written: the locked status is off, in maintenance or not
      (D-06);
    - "nothing", with nothing written: the location has no stored interval (UI5-D9, also
      the second click of a double submit);
    - "gone", with nothing written: the location is unknown or deleted.

    Queued alerts, the configuration, the switches and open incidents are kept (D-05,
    D-07). ValueError for a naive ``now``.
    """
    _aware(now)
    with transaction.atomic(), connection.cursor() as cur:
        # The row lock first, as every timeline writer takes it.
        cur.execute(LOCK_SQL, [location_id])
        locked = cur.fetchone()
        if locked is None:
            return "gone"
        cur.execute(DELETED_SQL, [location_id])
        deleted = cur.fetchone()
        if deleted is None or bool(deleted[0]):
            return "gone"
        if locked[0] == "off":
            # D-06: decided from the row the lock returned, whatever the maintenance flag.
            return "in_progress"
        cur.execute(HAS_HISTORY_SQL, [location_id])
        exists = cur.fetchone()
        if not (exists and exists[0]):
            return "nothing"
        cur.execute(DELETE_HISTORY_SQL, {"id": location_id})
        cur.execute(WAITING_SQL, {"id": location_id})
        cur.execute(MARK_CHARTS_SQL, {"id": location_id, "now": now})
    # The id and a time only: never a key or a token (OPS-08).
    log.info("history of location %s reset at %s", location_id, now.isoformat())
    return "reset"
