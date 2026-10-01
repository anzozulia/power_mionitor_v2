"""Engine transitions as gate SQL: each change is one conditional UPDATE (KD2, INV-01).

Every function runs its statements in one transaction on Django's connection and decides
by the row count. Under Read Committed a competing UPDATE re-checks its WHERE clause after
the first one commits and changes 0 rows, so two writers never both win. Nothing here does
network I/O, and time always comes from the caller's Clock (``now``), never from SQL
``now()``. Parameters go in as ``%(name)s`` / ``%s`` placeholders, never formatted into
the SQL.

A gate that changes the status also writes the stored timeline (KD1) through
``timeline.set_open_state`` in the same transaction, while the gate UPDATE holds the
location_state row lock. 01-08 adds the off -> on gate as the first statement and the
"restored" result.
"""

from datetime import datetime

from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from powermon.engine import rules, timeline

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


def _config_row(cur: CursorWrapper, location_id: int) -> tuple[bool, bool]:
    """``(maintenance, alerts_enabled)`` of the location, read on the gate's cursor."""
    cur.execute(CONFIG_SQL, [location_id])
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"location {location_id} has a state row but no location row")
    return bool(row[0]), bool(row[1])


def record_heartbeat(location_id: int, now: datetime) -> str:
    """Apply one accepted heartbeat that the server received at ``now``.

    Returns "started" (waiting -> on), "plain" (already on) or "ignored" (no state row in
    a status this function handles yet).
    """
    params: dict[str, int | datetime] = {"id": location_id, "now": now}
    with transaction.atomic(), connection.cursor() as cur:
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
