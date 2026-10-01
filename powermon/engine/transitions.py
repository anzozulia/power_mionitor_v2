"""Engine transitions as gate SQL: each change is one conditional UPDATE (KD2, INV-01).

The location's ``location_state`` row lock is the per-location mutex for every writer of
that location's state and timeline (MON-04, WR-01). ``record_heartbeat`` takes it first
with ``SELECT ... FOR UPDATE`` and only then chooses its gate from the locked status;
``mark_off``'s CAS UPDATE takes the same lock. Writers of one location therefore run one
after the other, and under the lock two of them can neither both win nor both lose: a
heartbeat that arrives while the detector's OFF transaction is open waits for it and then
restores the location, instead of finding no gate that matches. The conditional UPDATEs
(decided by the row count) and the timeline's exclusion constraint stay as the second
line of defence. Every transaction locks exactly one location_state row and takes that
lock first, so two of them never wait on each other's second lock (no deadlock).

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

from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from powermon.alerts import outbox
from powermon.engine import rules, timeline

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


def record_heartbeat(location_id: int, now: datetime) -> str:
    """Apply one accepted heartbeat that the server received at ``now``.

    First the location's state row is locked (LOCK_SQL), then one gate is chosen by the
    locked status, all in one transaction with no network I/O (INV-01, HB-03). A heartbeat
    that arrives while another writer of this location is mid-transaction waits for it
    and sees its result (WR-01). Returns "restored" (off -> on, with one power_on alert
    queued when alerts are on), "started" (waiting -> on, silent), "plain" (already on) or
    "ignored" (only when this location has no state row; nothing is written).
    """
    params: dict[str, int | datetime] = {"id": location_id, "now": now}
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(LOCK_SQL, [location_id])
        locked = cur.fetchone()
        if locked is None:
            return "ignored"
        status, outage_started_at = locked
        if status == "off":
            _run_gate(cur, RESTORE_SQL, params)
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
