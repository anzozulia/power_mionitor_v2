"""Engine transitions as gate SQL: each change is one conditional UPDATE (KD2, INV-01).

Every function runs its statements in one transaction on Django's connection and decides
by the row count. Under Read Committed a competing UPDATE re-checks its WHERE clause after
the first one commits and changes 0 rows, so two writers never both win. Nothing here does
network I/O, and time always comes from the caller's Clock (``now``), never from SQL
``now()``. Parameters go in as ``%(name)s`` placeholders, never formatted into the SQL.

01-06 adds the timeline write to the waiting -> on branch. 01-08 adds the off -> on gate as
the first statement and the "restored" result.
"""

from datetime import datetime

from django.db import connection, transaction

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


def record_heartbeat(location_id: int, now: datetime) -> str:
    """Apply one accepted heartbeat that the server received at ``now``.

    Returns "started" (waiting -> on), "plain" (already on) or "ignored" (no state row in
    a status this function handles yet).
    """
    params: dict[str, int | datetime] = {"id": location_id, "now": now}
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(FIRST_SQL, params)
        if cur.rowcount == 1:
            return "started"
        cur.execute(PLAIN_SQL, params)
        if cur.rowcount == 1:
            return "plain"
    return "ignored"
