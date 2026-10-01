"""Engine transitions as gate SQL: each change is one conditional UPDATE (KD2, INV-01).

Every function runs its statements in one transaction on Django's connection and decides
by the row count. Under Read Committed a competing UPDATE re-checks its WHERE clause after
the first one commits and changes 0 rows, so two writers never both win. Nothing here does
network I/O, and time always comes from the caller's Clock (``now``), never from SQL
``now()``. Parameters go in as ``%(name)s`` / ``%s`` placeholders, never formatted into
the SQL.

A gate that changes the status also writes the stored timeline (KD1) through
``timeline.set_open_state`` in the same transaction, while the gate UPDATE holds the
location_state row lock. When alerts are on, it also queues the alert in the outbox in
that same transaction (KD2, D-14): the transition and its alert commit together or not at
all. The worker relay sends the alert later.
"""

from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from powermon.alerts import outbox
from powermon.engine import rules, timeline

# off -> on: the first heartbeat after an outage (MON-03). RETURNING gives the stored
# outage start for "was OFF for"; the UPDATE must not clear it, or RETURNING sees NULL.
RESTORE_SQL = """
UPDATE location_state
   SET status = 'on', on_since = %(now)s,
       last_heartbeat_at = GREATEST(last_heartbeat_at, %(now)s),
       state_version = state_version + 1
 WHERE location_id = %(id)s AND status = 'off'
RETURNING outage_started_at
"""

# waiting -> on: the first heartbeat starts monitoring, silently (MON-01).
FIRST_SQL = """
UPDATE location_state
   SET status = 'on', on_since = %(now)s, last_heartbeat_at = %(now)s,
       state_version = state_version + 1
 WHERE location_id = %(id)s AND status = 'waiting'
"""

# on: a plain heartbeat. GREATEST keeps an older timestamp from moving it back (D-08).
PLAIN_SQL = """
UPDATE location_state
   SET last_heartbeat_at = GREATEST(last_heartbeat_at, %(now)s),
       state_version = state_version + 1
 WHERE location_id = %(id)s AND status = 'on'
"""

# The settings a transition reads, inside the same transaction (INV-05).
CONFIG_SQL = "SELECT maintenance, alerts_enabled FROM location WHERE id = %s"

# What the detector checks: monitored locations that are on, with their CAS token.
SNAPSHOT_SQL = """
SELECT s.location_id, s.state_version, s.last_heartbeat_at, s.on_since, s.window_start_at,
       l.period_s, l.grace_s, l.router_grace, l.alerts_enabled
  FROM location_state s
  JOIN location l ON l.id = s.location_id
 WHERE s.status = 'on' AND NOT l.maintenance AND l.deleted_at IS NULL
 ORDER BY s.location_id
"""

# on -> off, only if nothing changed since the snapshot: any heartbeat in between has
# bumped state_version, and then 0 rows change (INV-01). Maintenance or deletion in
# between also stops it. Verbatim from RESEARCH Pattern 4.
OFF_CAS_SQL = """
UPDATE location_state s
   SET status='off', outage_started_at=%(start)s, state_version=s.state_version+1
  FROM location l
 WHERE s.location_id=%(id)s AND l.id=s.location_id
   AND s.status='on' AND s.state_version=%(v)s
   AND NOT l.maintenance AND l.deleted_at IS NULL
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


def record_heartbeat(location_id: int, now: datetime) -> str:
    """Apply one accepted heartbeat that the server received at ``now``.

    The gates run in the order RESTORE, FIRST, PLAIN, each a conditional UPDATE, all in
    one transaction with no network I/O (INV-01). Returns "restored" (off -> on, with one
    power_on alert queued when alerts are on), "started" (waiting -> on, silent), "plain"
    (already on) or "ignored" (no state row for this location).
    """
    params: dict[str, int | datetime] = {"id": location_id, "now": now}
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(RESTORE_SQL, params)
        restored = cur.fetchone()
        if restored is not None:
            (outage_started_at,) = restored
            maintenance, alerts_enabled = _config_row(cur, location_id)
            # Closes the off interval at ``now``. A restore dated before the outage start
            # would close it before its start: the CHECK rejects that and the whole
            # transaction rolls back, so nothing is half-written.
            timeline.set_open_state(
                cur, location_id, now, rules.desired_open_state("on", maintenance)
            )
            if alerts_enabled:
                outbox.enqueue(
                    outbox.KIND_POWER_ON,
                    location_id,
                    event_at=now,
                    recorded_at=now,
                    payload={"was_off_us": _us(now - outage_started_at)},
                )
            return "restored"
        cur.execute(FIRST_SQL, params)
        if cur.rowcount == 1:
            # MON-01 stays silent: the timeline opens, no outbox row is written.
            maintenance, _alerts_enabled = _config_row(cur, location_id)
            timeline.set_open_state(
                cur, location_id, now, rules.desired_open_state("on", maintenance)
            )
            return "started"
        cur.execute(PLAIN_SQL, params)
        if cur.rowcount == 1:
            return "plain"
    return "ignored"


def read_snapshots() -> list[tuple[rules.Snapshot, bool]]:
    """Every monitored location that is on, as ``(snapshot, alerts_enabled)``, by id.

    Locations in maintenance, deleted locations and locations still waiting for their
    first heartbeat are never candidates for OFF.
    """
    with connection.cursor() as cur:
        cur.execute(SNAPSHOT_SQL)
        rows = cur.fetchall()
    return [
        (
            rules.Snapshot(
                location_id=row[0],
                state_version=row[1],
                last_heartbeat_at=row[2],
                on_since=row[3],
                window_start_at=row[4],
                period_s=row[5],
                grace_s=row[6],
                router_grace=row[7],
            ),
            bool(row[8]),
        )
        for row in rows
    ]


def mark_off(snap: rules.Snapshot, d: rules.Decision, now: datetime, alerts_enabled: bool) -> bool:
    """Record the OFF transition ``d`` for ``snap``, decided by the detector at ``now``.

    One transaction: the CAS UPDATE, then the timeline (on closed at the outage start, off
    opened from it), then the power_off outbox row when alerts are on. Returns False and
    writes nothing when the CAS changes 0 rows: a heartbeat or another writer got there
    first, so the decision is stale and is skipped quietly (INV-01).
    """
    if not d.off or d.outage_start is None or d.was_on is None:
        raise ValueError("mark_off needs an OFF decision with an outage start and was_on")
    params: dict[str, int | datetime] = {
        "id": snap.location_id,
        "v": snap.state_version,
        "start": d.outage_start,
    }
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(OFF_CAS_SQL, params)
        if cur.rowcount != 1:
            return False
        timeline.set_open_state(
            cur, snap.location_id, d.outage_start, "off", outage_start_at=d.outage_start
        )
        if alerts_enabled:
            outbox.enqueue(
                outbox.KIND_POWER_OFF,
                snap.location_id,
                event_at=d.outage_start,
                recorded_at=now,
                payload={"was_on_us": _us(d.was_on)},
            )
    return True
