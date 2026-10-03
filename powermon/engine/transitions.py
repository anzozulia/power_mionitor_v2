"""Engine transitions as gate SQL: each change is one conditional UPDATE (KD2, INV-01).

The location's ``location_state`` row lock is the per-location mutex for every writer of
that location's state and timeline (MON-04, WR-01). ``record_heartbeat`` takes it first
with ``SELECT ... FOR UPDATE`` and only then chooses its gate from the locked status;
``mark_off`` takes it the same way before it reads the open interval and runs its CAS
UPDATE (D2). Writers of one location therefore run one after the other, and under the
lock two of them can neither both win nor both lose: a heartbeat that arrives while the
detector's OFF transaction is open waits for it and then restores the location, instead
of finding no gate that matches. The conditional UPDATEs (decided by the row count) and
the timeline's exclusion constraint stay as the second line of defence. Every
transaction locks exactly one location_state row and takes that lock first, so two of
them never wait on each other's second lock (no deadlock).

Every function runs its statements in one transaction on Django's connection. Nothing
here does network I/O, and time always comes from the caller's Clock (``now``), never
from SQL ``now()``. Parameters go in as ``%(name)s`` / ``%s`` placeholders, never
formatted into the SQL.

A gate that changes the status also writes the stored timeline (KD1) through
``timeline.set_open_state`` in the same transaction, under the row lock. When alerts are
on, it also queues the alert in the outbox in that same transaction (KD2, D-14): the
transition and its alert commit together or not at all. The worker relay sends the alert
later.
"""

import logging
import threading
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from powermon.alerts import outbox
from powermon.engine import rules, timeline

log = logging.getLogger(__name__)

# IN-01: a clamped restore (see record_heartbeat) logs one WARNING per process, not one
# per heartbeat. After a backward clock step every location clamps at once, from several
# web threads, so the flag is set under a lock.
_restore_clamp_warned = False
_restore_clamp_lock = threading.Lock()

# The heartbeat's first statement: lock the location's state row, then read the status
# that chooses the gate. One table only, so it locks the state row and never the location
# row (inserting an outbox row only needs KEY SHARE on that one).
LOCK_SQL = """
SELECT status, outage_started_at
  FROM location_state
 WHERE location_id = %s
   FOR UPDATE
"""

# off -> on: the first heartbeat after an outage (MON-03). outage_started_at is left as
# it is; "was OFF for" is computed from the value LOCK_SQL read under the lock.
RESTORE_SQL = """
UPDATE location_state
   SET status = 'on', on_since = %(now)s,
       last_heartbeat_at = GREATEST(last_heartbeat_at, %(now)s),
       state_version = state_version + 1
 WHERE location_id = %(id)s AND status = 'off'
"""

# waiting -> on: the first heartbeat starts monitoring, silently (MON-01).
FIRST_SQL = """
UPDATE location_state
   SET status = 'on', on_since = %(now)s, last_heartbeat_at = %(now)s,
       state_version = state_version + 1
 WHERE location_id = %(id)s AND status = 'waiting'
"""

# Back to "waiting for first heartbeat" (MON-01): the history reset (DATA-03, D-05) and
# the post-restore restart (D-13) run it under the row lock; the FIRST gate leaves it at
# the next heartbeat. Status waiting with a NULL outage start keeps
# location_state_off_needs_outage_start true.
WAITING_SQL = """
UPDATE location_state
   SET status = 'waiting', last_heartbeat_at = NULL, on_since = NULL,
       outage_started_at = NULL, window_start_at = NULL,
       state_version = state_version + 1
 WHERE location_id = %(id)s
"""

# on: a plain heartbeat. GREATEST keeps an older timestamp from moving it back (D-08).
PLAIN_SQL = """
UPDATE location_state
   SET last_heartbeat_at = GREATEST(last_heartbeat_at, %(now)s),
       state_version = state_version + 1
 WHERE location_id = %(id)s AND status = 'on'
"""

# Is the location deleted? Read right after LOCK_SQL, under the state row lock: a delete
# commits its tombstone in a transaction that holds the same lock (D-09), so a heartbeat
# that looked up its key before the delete and waited on the lock sees it here (LOC-04).
# No row (the location row is gone) counts as deleted.
DELETED_SQL = "SELECT deleted_at IS NOT NULL FROM location WHERE id = %s"

# Whether an alert is sent is decided when its transition is recorded, inside the
# transition's own transaction (INV-05, D-06): a heartbeat gate reads the settings here,
# under the row lock; the OFF transition reads alerts_enabled from its CAS row
# (OFF_CAS_SQL's RETURNING). Neither uses a value read before the transaction began.
CONFIG_SQL = "SELECT maintenance, alerts_enabled FROM location WHERE id = %s"

# What the detector checks: monitored locations that are on, with their CAS token. No
# alerts_enabled here: the snapshot is read outside the OFF transaction, and the admin may
# toggle alerts before the CAS runs (WR-05).
SNAPSHOT_SQL = """
SELECT s.location_id, s.state_version, s.last_heartbeat_at, s.on_since, s.window_start_at,
       l.period_s, l.grace_s, l.router_grace
  FROM location_state s
  JOIN location l ON l.id = s.location_id
 WHERE s.status = 'on' AND NOT l.maintenance AND l.deleted_at IS NULL
 ORDER BY s.location_id
"""

# on -> off, only if nothing changed since the snapshot: any heartbeat in between has
# bumped state_version, and then no row comes back (INV-01). Maintenance or deletion in
# between also stops it. The returned alerts_enabled is the location's setting at the
# moment the OFF is recorded, which decides the OFF alert (INV-05, D-06; Phase 1 WR-05).
OFF_CAS_SQL = """
UPDATE location_state s
   SET status='off', outage_started_at=%(start)s, state_version=s.state_version+1
  FROM location l
 WHERE s.location_id=%(id)s AND l.id=s.location_id
   AND s.status='on' AND s.state_version=%(v)s
   AND NOT l.maintenance AND l.deleted_at IS NULL
RETURNING l.alerts_enabled
"""


def _us(td: timedelta) -> int:
    """A duration as exact integer microseconds (no float, Pitfall 4)."""
    return td // timedelta(microseconds=1)


def _config_row(cur: CursorWrapper, location_id: int) -> tuple[bool, bool]:
    """``(maintenance, alerts_enabled)`` of the location, read on the gate's cursor."""
    cur.execute(CONFIG_SQL, [location_id])
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"location {location_id} has a state row but no location row")
    return bool(row[0]), bool(row[1])


def _deleted(cur: CursorWrapper, location_id: int) -> bool:
    """True when the location is deleted or its row is gone, read on the gate's cursor."""
    cur.execute(DELETED_SQL, [location_id])
    row = cur.fetchone()
    return row is None or bool(row[0])


def _run_gate(cur: CursorWrapper, sql: str, params: dict[str, int | datetime]) -> None:
    """Run one heartbeat gate UPDATE; it must change exactly the locked row.

    Under the row lock the status cannot change between LOCK_SQL and the gate, so the
    gate's WHERE clause always matches. If it ever changes 0 rows (a writer that skipped
    the lock), raise and roll back instead of writing a second transition or alert.
    """
    cur.execute(sql, params)
    if cur.rowcount != 1:
        raise RuntimeError(
            f"location {params['id']}: heartbeat gate changed {cur.rowcount} rows "
            "under the row lock"
        )


def _warn_restore_clamped(location_id: int, at: datetime) -> None:
    """Log the first clamped restore of this process at WARNING; later ones stay silent.

    The line names only the location id and the restore time: never the key or a token.
    """
    global _restore_clamp_warned
    with _restore_clamp_lock:
        if _restore_clamp_warned:
            return
        _restore_clamp_warned = True
    log.warning(
        "heartbeat for location %s restored at %s, after its receive time: the server "
        "clock stepped back or a lapse carve ran at the same time",
        location_id,
        at.isoformat(),
    )


def record_heartbeat(location_id: int, now: datetime) -> str:
    """Apply one accepted heartbeat that the server received at ``now``.

    First the location's state row is locked (LOCK_SQL), then one gate is chosen by the
    locked status, all in one transaction with no network I/O (INV-01, HB-03). A heartbeat
    that arrives while another writer of this location is mid-transaction waits for it
    and sees its result (WR-01). Returns "restored" (off -> on, with one power_on alert
    queued when alerts are on), "started" (waiting -> on, silent), "plain" (already on) or
    "ignored" (this location has no state row, or it is deleted; nothing is written).

    The deleted check runs right after the row lock and before any gate (D-09, LOC-04).
    The heartbeat lookup already skips deleted locations, but a heartbeat that looked up
    its key before a delete may wait on the lock the delete holds; once it gets the lock
    it sees the tombstone and changes nothing: no restore, no interval, no ON alert.

    A restore is stamped at ``max(now, outage start, open interval start)`` (IN-01). After
    a backward clock step ``now`` can lie before the outage start, and a heartbeat that
    waited on a lapse carve's lock can lie before the open off piece's new start. Closing
    the off interval there would violate ``power_interval_end_after_start`` and answer 500
    on every heartbeat; the clamp keeps the CHECK true and "was OFF for" never negative.
    The ON alert is dated at ``at`` but recorded at ``now``: the outbox makes a row due at
    its recorded_at, so after a backward clock step the alert goes out at once instead of
    waiting until the wall clock reaches ``at`` again. Without a clamp ``at == now``.
    """
    params: dict[str, int | datetime] = {"id": location_id, "now": now}
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(LOCK_SQL, [location_id])
        locked = cur.fetchone()
        if locked is None or _deleted(cur, location_id):
            return "ignored"
        status, outage_started_at = locked
        if status == "off":
            open_start = timeline.open_start(cur, location_id)
            at = max(t for t in (now, outage_started_at, open_start) if t is not None)
            if at > now:
                _warn_restore_clamped(location_id, at)
            _run_gate(cur, RESTORE_SQL, {"id": location_id, "now": at})
            maintenance, alerts_enabled = _config_row(cur, location_id)
            # Closes the off interval at ``at``, never before its start (the clamp above):
            # an off piece that starts at ``at`` is deleted instead.
            timeline.set_open_state(
                cur, location_id, at, rules.desired_open_state("on", maintenance)
            )
            if alerts_enabled:
                outbox.enqueue(
                    outbox.KIND_POWER_ON,
                    location_id,
                    event_at=at,
                    recorded_at=now,
                    payload={"was_off_us": _us(at - outage_started_at)},
                )
            return "restored"
        if status == "waiting":
            _run_gate(cur, FIRST_SQL, params)
            # MON-01 stays silent: the timeline opens, no outbox row is written.
            maintenance, _alerts_enabled = _config_row(cur, location_id)
            timeline.set_open_state(
                cur, location_id, now, rules.desired_open_state("on", maintenance)
            )
            return "started"
        _run_gate(cur, PLAIN_SQL, params)
        return "plain"


def read_snapshots() -> list[rules.Snapshot]:
    """Every monitored location that is on, as its snapshot, by id.

    Locations in maintenance, deleted locations and locations still waiting for their
    first heartbeat are never candidates for OFF. A snapshot carries no alerts setting:
    ``mark_off`` reads it from its own CAS row (INV-05, D-06).
    """
    with connection.cursor() as cur:
        cur.execute(SNAPSHOT_SQL)
        rows = cur.fetchall()
    return [
        rules.Snapshot(
            location_id=row[0],
            state_version=row[1],
            last_heartbeat_at=row[2],
            on_since=row[3],
            window_start_at=row[4],
            period_s=row[5],
            grace_s=row[6],
            router_grace=row[7],
        )
        for row in rows
    ]


def mark_off(snap: rules.Snapshot, d: rules.Decision, now: datetime) -> bool:
    """Record the OFF transition ``d`` for ``snap``, decided by the detector at ``now``.

    One transaction: the row lock (LOCK_SQL), the CAS UPDATE, then the timeline (on closed
    at the outage start, off opened from it), then the power_off outbox row when alerts
    are on. Returns False and writes nothing when the CAS changes no row: a heartbeat or
    another writer got there first, so the decision is stale and is skipped quietly
    (INV-01).

    Whether alerts are on comes from the CAS UPDATE's own row (``RETURNING
    l.alerts_enabled``), never from the snapshot: the admin may toggle alerts between the
    snapshot and this transaction, and the setting at the moment the OFF is recorded is
    the one that counts (INV-05, D-06). An OFF recorded while alerts are off queues
    nothing, and nothing is held for later: turning alerts back on never sends it.

    The outage starts at ``max(decided outage start, open interval start)`` (D2), read
    under the row lock, so a lapse carve that committed after the snapshot is seen. A
    carve can move the open on piece's start past the decided start: a carver that lost
    the cursor CAS with a later ``now``, or a carve between the snapshot and this call.
    Closing the open piece there would violate ``power_interval_end_after_start``, and
    every later OFF of the location would fail the same way. The clamp keeps the CHECK
    true, as IN-01's does for a restore; "was ON for" is unchanged (D-03). Without a carve
    the open piece starts at or before the decided start, and nothing changes.
    """
    if not d.off or d.outage_start is None or d.was_on is None:
        raise ValueError("mark_off needs an OFF decision with an outage start and was_on")
    with transaction.atomic(), connection.cursor() as cur:
        # The row lock first, as every timeline writer takes it: the open interval read
        # next is the one this transaction closes. With no state row the CAS changes 0 rows.
        cur.execute(LOCK_SQL, [snap.location_id])
        open_start = timeline.open_start(cur, snap.location_id)
        start = d.outage_start if open_start is None else max(d.outage_start, open_start)
        params: dict[str, int | datetime] = {
            "id": snap.location_id,
            "v": snap.state_version,
            "start": start,
        }
        cur.execute(OFF_CAS_SQL, params)
        row = cur.fetchone()
        if row is None:
            return False
        alerts_enabled = bool(row[0])
        timeline.set_open_state(cur, snap.location_id, start, "off", outage_start_at=start)
        if alerts_enabled:
            outbox.enqueue(
                outbox.KIND_POWER_OFF,
                snap.location_id,
                event_at=start,
                recorded_at=now,
                payload={"was_on_us": _us(d.was_on)},
            )
    if start != d.outage_start:
        # Ids and times only: never a key or a token.
        log.warning(
            "OFF for location %s starts at %s, where its open interval starts, not at %s: "
            "a lapse carve moved that start",
            snap.location_id,
            start.isoformat(),
            d.outage_start.isoformat(),
        )
    return True
