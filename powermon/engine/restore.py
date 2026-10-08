"""After a database restore: restart every location silently; fingerprint the history (OPS-06).

A restore must never replay the dump's past to subscribers (PITFALLS Pitfall 12). The dump
holds a live state from hours or days ago: locations "on" or "off" whose last heartbeat is
long gone, alerts still queued, incidents still open. Started on that, the detector would
decide a wave of false OFFs, the relay would send stale or duplicate alerts, and every
open incident could end with a late recovery notice. ``restart_after_restore`` turns the
dump into a clean start, before web and worker run on it (``manage.py post_restore``):

- D-13: every location that is not deleted and not waiting goes back to "waiting for
  first heartbeat" (``transitions.WAITING_SQL``). Its open interval becomes
  ``not_monitored`` from the dump's last known moment, the detection cursor
  ``system_state.last_cycle_completed_at``, never before the open interval's start
  (``max`` of the two; the open start alone when there is no cursor). The lost hours are
  then drawn hatched (KD3) once the first heartbeat closes that piece, and the FIRST gate
  restarts monitoring with no alert (MON-01, K-1). History and the maintenance flag are
  kept: an open piece that is already ``not_monitored`` (maintenance) stays as it is.
  Accepted trade-off: an outage still in progress when the server was lost never gets
  its ON alert, and an outage that starts during the rebuild is not alerted until the
  device has reported once. In exchange no stale or duplicate alert is ever sent (SC4).
- D-14: every ``pending`` or ``sending`` outbox row, subscriber alerts and ops notices
  alike, is marked ``dropped`` with ``last_error`` "restored", with no expiry or
  uncertain notice. A ``sending`` row matters too: the worker's activation would turn it
  into "uncertain" and queue an ops notice for it (RESEARCH Pitfall 5). Every open ops
  incident (all-silent, delivery failing, pin failed) is closed at ``now`` with no
  notice, so none can send a late recovery notice; a problem that persists opens a fresh
  incident with its own notice. A removed outage's dropped ON alert whose location still
  has an OFF delete request the dump had not settled is re-tagged "restored" in the same
  transaction (``KEEP_REMOVED_ON_DROPPED_SQL``), so a delete refused after the restore
  never sends it; the delete requests themselves are kept, so the false OFF is still
  removed (quick task 261008-vdk).
- ``system_state`` is never written. The worker's first forced carve (a new lease
  generation) then records exactly one gap from the dump's cursor and queues the single
  "monitoring gap" ops notice: the only message after a restore. The carve rewrites only
  locations that are on or off, so the waiting locations keep the open
  ``not_monitored`` piece written here until their first heartbeat.

Lock order is the one every timeline writer uses: one transaction per location, which
takes that location's ``location_state`` row lock first (``transitions.LOCK_SQL``,
imported, never copied), as ``lapse.carve_window`` does. The outbox and incident writes
run afterwards in their own transaction and lock no state row. The whole step refuses to
run, before any write, while any database session holds the worker lock
(``powermon.worker.lease.LOCK_KEY``, read from ``pg_locks``): run against a live system
it would drop real queued alerts and reset live locations. A second run changes nothing.

D-16: ``fingerprint`` is the restore drill's comparison tool (INV-25 #2, ``manage.py
history_fingerprint``). It returns the row count and an md5 checksum of every row of
``location``, ``power_interval`` and ``chart_message``, in id order, so a source and a
restored database can be compared table by table. It runs as its own outermost
transaction, made READ ONLY by its first statement, with no statement timeout, through
Django's connection: the session time zone is UTC (``USE_TZ``), so timestamps have the
same text on both sides (RESEARCH Pitfall 14). Secrets enter the location checksum only as
md5 inside the aggregate; the output never holds a token or a device key. Compare before
``post_restore``, which changes the open pieces and ``location_state``.

Nothing here does network I/O, and time comes only from the caller (``now``); SQL
constants use bound parameters only.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction

from powermon.alerts import outbox
from powermon.engine import timeline
from powermon.engine.models import SystemState
from powermon.engine.transitions import LOCK_SQL, WAITING_SQL
from powermon.worker.lease import LOCK_KEY

log = logging.getLogger(__name__)

# ``last_error`` of every outbox row dropped by the post-restore step (D-14); a short code.
RESTORED = "restored"

# Every location the restart applies to: not waiting and not deleted, by id (Pattern 7).
MONITORED_SQL = """
SELECT s.location_id
  FROM location_state s
  JOIN location l ON l.id = s.location_id
 WHERE s.status <> 'waiting' AND l.deleted_at IS NULL
 ORDER BY s.location_id
"""
# Every queued row of both channels, never sent and never notified about (D-14).
DROP_QUEUED_SQL = """
UPDATE outbox_message SET status = 'dropped', last_error = %(code)s
 WHERE status IN ('pending', 'sending')
"""
# A removed outage's dropped ON alert while its location still has an OFF delete request
# the dump had not settled: re-tagged "restored", so a delete refused after the restore
# never sends it (outbox.fail_delete puts back only "outage_removed"). The delete request
# itself is kept, so the false OFF is still removed from the channel (261008-vdk, F-13).
# The EXISTS is served by the partial index outbox_delete_due_idx.
KEEP_REMOVED_ON_DROPPED_SQL = """
UPDATE outbox_message m SET last_error = %(code)s
 WHERE m.channel = %(subscriber)s AND m.kind = %(on)s
   AND m.status = 'dropped' AND m.last_error = %(removed)s
   AND EXISTS (SELECT 1 FROM outbox_message d
                WHERE d.location_id = m.location_id AND d.kind = %(off)s
                  AND d.delete_requested_at IS NOT NULL AND d.delete_result IS NULL
                  AND d.event_at <= m.event_at)
"""
# Every open incident of any kind, closed quietly (D-14 as refined on 2026-10-03).
CLOSE_INCIDENTS_SQL = "UPDATE ops_incident SET ended_at = %(now)s WHERE ended_at IS NULL"
# Does any session hold the worker lock in this database? The pg_locks predicate of the
# relay's fenced claim (outbox.CLAIM_HELD_SQL) without its pid filter: a session advisory
# lock on a bigint key shows the key's high 32 bits in classid, its low 32 bits in objid
# and objsubid 1.
WORKER_LOCK_HELD_SQL = """
SELECT EXISTS (
    SELECT 1 FROM pg_locks
     WHERE locktype = 'advisory' AND granted
       AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
       AND classid = %(classid)s::oid AND objid = %(objid)s::oid AND objsubid = 1
)
"""
_LOCK_CLASSID = LOCK_KEY >> 32
_LOCK_OBJID = LOCK_KEY & 0xFFFFFFFF

# The history a restore must carry over, in output order (D-16).
FINGERPRINT_TABLES = ("location", "power_interval", "chart_message")
# One statement per table: the row count and the md5 of every row's text joined in id order
# (RESEARCH Pattern 7); empty tables give 0 and ''. The location projection names its
# columns and hashes the bot token and the device key inside the aggregate, so neither is
# ever a column of the result; the other two tables are fingerprinted as whole rows.
FINGERPRINT_SQL: dict[str, str] = {
    "location": r"""
SELECT count(*), coalesce(md5(string_agg(t::text, E'\n' ORDER BY t.id)), '')
  FROM (SELECT id, name, period_s, grace_s, router_grace, maintenance, alerts_enabled,
               language, chat_id, deleted_at, created_at,
               md5(bot_token) AS bot_token_md5, md5(device_key) AS device_key_md5
          FROM location) t
""",
    "power_interval": r"""
SELECT count(*), coalesce(md5(string_agg(t::text, E'\n' ORDER BY t.id)), '')
  FROM (SELECT * FROM power_interval) t
""",
    "chart_message": r"""
SELECT count(*), coalesce(md5(string_agg(t::text, E'\n' ORDER BY t.id)), '')
  FROM (SELECT * FROM chart_message) t
""",
}


class WorkerActive(Exception):
    """A worker holds the worker lock: the post-restore step must not run (nothing written)."""


@dataclass(frozen=True)
class RestoreCounts:
    """What one post-restore run changed."""

    # Locations set back to "waiting for first heartbeat".
    locations: int
    # Outbox rows (both channels) dropped with last_error "restored".
    dropped: int
    # Open ops incidents closed without a notice.
    incidents: int


def worker_lock_held() -> bool:
    """True while any session of this database holds the worker lock (``lease.LOCK_KEY``)."""
    with connection.cursor() as cur:
        cur.execute(WORKER_LOCK_HELD_SQL, {"classid": _LOCK_CLASSID, "objid": _LOCK_OBJID})
        row = cur.fetchone()
    return row is not None and bool(row[0])


def _read_cursor() -> datetime | None:
    """The dump's last known moment: ``system_state.last_cycle_completed_at``, read only.

    Unlike ``lapse.read_cursor`` it never recreates a missing singleton: the post-restore
    step writes nothing to ``system_state``, so the worker's first carve sees the dump's
    cursor as it was.
    """
    cursor: datetime | None = (
        SystemState.objects.filter(pk=1).values_list("last_cycle_completed_at", flat=True).first()
    )
    return cursor


def _restart_one(location_id: int, cursor: datetime | None) -> bool:
    """Set one location waiting, its open piece not monitored from the dump's cursor (D-13).

    One transaction, the row lock first. False, with nothing written, when the location has
    no state row or is already waiting.
    """
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(LOCK_SQL, [location_id])
        locked = cur.fetchone()
        if locked is None or locked[0] == "waiting":
            return False
        start = timeline.open_start(cur, location_id)
        if start is not None:
            # Never before the open piece's start; an open piece that starts there is
            # replaced, and one that is already not monitored (maintenance) is kept.
            at = start if cursor is None else max(cursor, start)
            timeline.set_open_state(cur, location_id, at, "not_monitored")
        cur.execute(WAITING_SQL, {"id": location_id})
    return True


def restart_after_restore(now: datetime) -> RestoreCounts:
    """Restart every location silently after a restore; drop the queue, close incidents.

    Run after ``pg_restore`` and ``migrate``, before web and worker start. Raises
    ``WorkerActive`` before any write while a worker holds the worker lock, and ValueError
    for a naive ``now``. Each location changes in its own transaction under its row lock
    (D-13); then one transaction drops every queued outbox row of both channels and closes
    every open ops incident at ``now`` (D-14); it also re-tags a removed outage's dropped
    ON alert "restored" while its OFF's delete is unsettled, which the counts do not
    include (F-13). ``system_state`` is left alone, so the worker's first forced carve
    sends the single gap notice. Idempotent: a second run returns zero counts and writes
    nothing.
    """
    if now.utcoffset() is None:
        raise ValueError("restart_after_restore needs an aware now, not a naive datetime")
    if worker_lock_held():
        raise WorkerActive("a worker holds the worker lock")
    cursor = _read_cursor()
    with connection.cursor() as cur:
        cur.execute(MONITORED_SQL)
        location_ids = [row[0] for row in cur.fetchall()]
    restarted = sum(1 for location_id in location_ids if _restart_one(location_id, cursor))
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(DROP_QUEUED_SQL, {"code": RESTORED})
        dropped = cur.rowcount
        cur.execute(
            KEEP_REMOVED_ON_DROPPED_SQL,
            {
                "code": RESTORED,
                "subscriber": outbox.CHANNEL_SUBSCRIBER,
                "on": outbox.KIND_POWER_ON,
                "off": outbox.KIND_POWER_OFF,
                "removed": outbox.OUTAGE_REMOVED,
            },
        )
        cur.execute(CLOSE_INCIDENTS_SQL, {"now": now})
        closed = cur.rowcount
    counts = RestoreCounts(locations=restarted, dropped=dropped, incidents=closed)
    # Counts and times only: never a key or a token.
    log.info(
        "post-restore at %s: %d location(s) waiting from %s, %d queued message(s) dropped, "
        "%d open incident(s) closed",
        now.isoformat(),
        counts.locations,
        "the open start" if cursor is None else cursor.isoformat(),
        counts.dropped,
        counts.incidents,
    )
    return counts


def fingerprint() -> list[tuple[str, int, str]]:
    """``(table, row count, checksum)`` for each of FINGERPRINT_TABLES, read only (D-16).

    Must be the outermost transaction: called inside a caller's transaction it raises
    RuntimeError before running any statement. There its atomic block would only be a
    savepoint, and ``SET TRANSACTION READ ONLY`` would stay on in the caller's transaction
    after the savepoint is released. Otherwise one transaction whose first statement makes
    it READ ONLY, then lifts the 5 s statement cap for this transaction only (``SET
    LOCAL``), then runs FINGERPRINT_SQL in table order. A statement that tried to write
    would be refused by PostgreSQL. ``manage.py history_fingerprint`` runs in autocommit,
    so the guard never fires there.
    """
    if connection.in_atomic_block:
        raise RuntimeError("fingerprint() must run outside a transaction")
    result: list[tuple[str, int, str]] = []
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = 0")
        for table in FINGERPRINT_TABLES:
            cur.execute(FINGERPRINT_SQL[table])
            # An aggregate with no GROUP BY returns exactly one row.
            count, checksum = cur.fetchone() or (0, "")
            result.append((table, int(count), str(checksum)))
    return result
