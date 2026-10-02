"""The maintenance toggle as an engine transition (LOC-08, D-02).

Maintenance is a flag on the location, not a status (ARCHITECTURE › Location Status
Machine): ``rules.desired_open_state`` stores a location in maintenance as
``not_monitored``. Turning it on or off therefore moves the open end of the stored timeline
(KD1), so the toggle is a transition of the engine, not a configuration save. The web view
only calls ``set_maintenance``.

One transaction on Django's connection, like every timeline writer: the location's
``location_state`` row lock first (``transitions.LOCK_SQL``, imported, never copied), then
the conditional flag UPDATE on the location row, then the timeline through
``timeline.set_open_state``. Lock order is state row, then location row, the order every
Phase 4 writer uses, so the toggle cannot deadlock with a heartbeat or ``mark_off``. Nothing
here does network I/O, and time always comes from the caller (``now``), never from SQL
``now()``. Parameters go in as ``%(name)s`` placeholders, never formatted into the SQL.
"""

import logging
from datetime import datetime

from django.db import connection, transaction

from powermon.engine import rules, timeline
from powermon.engine.transitions import LOCK_SQL

log = logging.getLogger(__name__)

# The flag itself, only when it changes and the location is not deleted: 0 rows means the
# location already had that value (UI-D3 "already") or is gone, and nothing else is written.
FLAG_SQL = """
UPDATE location
   SET maintenance = %(on)s
 WHERE id = %(id)s AND deleted_at IS NULL AND maintenance <> %(on)s
"""

# Every toggle that moves the timeline bumps the CAS token, so a detector snapshot read
# before it loses its OFF CAS (INV-01 style).
BUMP_SQL = """
UPDATE location_state
   SET state_version = state_version + 1
 WHERE location_id = %(id)s
"""


def set_maintenance(location_id: int, on: bool, now: datetime) -> bool:
    """Turn maintenance ``on`` or off for the location at ``now``; True if the flag changed.

    False, with nothing written, when the flag already had that value, the location is
    deleted or it has no state row. Otherwise, under the row lock:

    - status "waiting": only the flag changes (D-02). The first heartbeat during
      maintenance opens ``not_monitored``, as ``record_heartbeat`` already does.
    - status "on" or "off": the open interval is closed at the click and the desired state
      opens from it: ``not_monitored`` when turning on; the stored status when turning off,
      an off piece keeping the locked ``outage_started_at`` so an outage in progress stays
      one outage (INV-11). The status itself never changes, and ``state_version`` is bumped.

    The click is clamped to ``max(now, open start)`` under the lock (Pitfall 2): the web
    reads ``now`` before it gets the lock, and a lapse carve committed meanwhile can have
    moved the open piece's start past it. Closing there would violate
    ``power_interval_end_after_start``.
    """
    with transaction.atomic(), connection.cursor() as cur:
        # The row lock first, as every timeline writer takes it.
        cur.execute(LOCK_SQL, [location_id])
        locked = cur.fetchone()
        if locked is None:
            return False
        status, outage_started_at = locked
        cur.execute(FLAG_SQL, {"id": location_id, "on": on})
        if cur.rowcount != 1:
            return False
        at = now
        if status != "waiting":
            open_start = timeline.open_start(cur, location_id)
            if open_start is not None:
                at = max(now, open_start)
            state = rules.desired_open_state(status, on)
            timeline.set_open_state(
                cur,
                location_id,
                at,
                state,
                outage_start_at=outage_started_at if state == "off" else None,
            )
            cur.execute(BUMP_SQL, {"id": location_id})
    # Ids and times only: never a key or a token.
    log.info(
        "maintenance %s for location %s at %s", "on" if on else "off", location_id, at.isoformat()
    )
    return True
