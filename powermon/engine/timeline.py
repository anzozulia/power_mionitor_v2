"""The single writer of the stored power timeline (KD1, ARCHITECTURE Pattern 3).

``set_open_state`` runs on the caller's cursor, inside the caller's transaction, right
after the caller's gate UPDATE on ``location_state``. That UPDATE already holds the
location's row lock, so two writers never interleave here. Raw SQL with ``%s``
parameters only; nothing reads the clock, every time is passed in.
"""

from datetime import datetime

from django.db.backends.utils import CursorWrapper

SELECT_OPEN_SQL = """
SELECT id, state, start_at, outage_start_at
  FROM power_interval
 WHERE location_id = %s AND end_at IS NULL
"""
DELETE_SQL = "DELETE FROM power_interval WHERE id = %s"
CLOSE_SQL = "UPDATE power_interval SET end_at = %s WHERE id = %s"
OPEN_SQL = """
INSERT INTO power_interval (location_id, state, start_at, end_at, outage_start_at)
VALUES (%s, %s, %s, NULL, %s)
"""


def set_open_state(
    cur: CursorWrapper,
    location_id: int,
    at: datetime,
    state: str | None,
    outage_start_at: datetime | None = None,
) -> None:
    """Make ``state`` the location's open interval from ``at`` on.

    - The open interval already has ``state`` and ``outage_start_at``: nothing changes.
    - Otherwise it is closed at ``at``, or deleted when it started at ``at`` (a stored
      interval never has zero length). Closing before its start violates
      ``power_interval_end_after_start`` and raises ``IntegrityError``.
    - Then, unless ``state`` is None, a new open interval starts at ``at``.

    Close before open: the exclusion constraint and the one-open unique index are checked
    per statement, so the old interval must end before the new one is inserted.
    """
    cur.execute(SELECT_OPEN_SQL, [location_id])
    row = cur.fetchone()
    if row is not None:
        open_id, open_state, open_start, open_outage_start = row
        if open_state == state and open_outage_start == outage_start_at:
            return
        if at == open_start:
            cur.execute(DELETE_SQL, [open_id])
        else:
            cur.execute(CLOSE_SQL, [at, open_id])
    if state is not None:
        cur.execute(OPEN_SQL, [location_id, state, at, outage_start_at])
