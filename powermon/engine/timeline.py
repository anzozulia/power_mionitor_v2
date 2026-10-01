"""The single writer of the stored power timeline (KD1, ARCHITECTURE Pattern 3).

Everything here runs on the caller's cursor, inside the caller's transaction, while the
caller holds the location's ``location_state`` row lock: ``SELECT ... FOR UPDATE`` in
``record_heartbeat``, the OFF CAS UPDATE in ``mark_off``, and ``SELECT ... FOR UPDATE``
in the lapse carve (every later writer takes the same lock first). So no two writers of a
location ever interleave here. ``set_open_state`` moves the open end of the timeline;
``overwrite`` rewrites a window of history (the lapse carve, Phase 5 corrections). Raw
SQL with ``%s`` parameters only; nothing reads the clock, every time is passed in.
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

# overwrite: the location's pieces that overlap [a, b), oldest first. A piece that ends at
# a or starts at b only touches the window and is not selected.
OVERLAP_SQL = """
SELECT id, state, start_at, end_at, outage_start_at
  FROM power_interval
 WHERE location_id = %(location_id)s AND start_at < %(b)s
   AND (end_at IS NULL OR end_at > %(a)s)
 ORDER BY start_at
"""
SHRINK_END_SQL = "UPDATE power_interval SET end_at = %s WHERE id = %s"
SHRINK_START_SQL = "UPDATE power_interval SET start_at = %s WHERE id = %s"
INSERT_PIECE_SQL = """
INSERT INTO power_interval (location_id, state, start_at, end_at, outage_start_at)
VALUES (%s, %s, %s, %s, %s)
"""
# The states overwrite may write: an off piece would need an outage start.
OVERWRITE_STATES = ("not_monitored", "on")


def open_start(cur: CursorWrapper, location_id: int) -> datetime | None:
    """The start of the location's open interval, or None when it has none.

    A restore must not close the open interval before this instant (IN-01: a lapse carve
    can move the open off piece's start past a waiting heartbeat's receive time).
    """
    cur.execute(SELECT_OPEN_SQL, [location_id])
    row = cur.fetchone()
    if row is None:
        return None
    start: datetime = row[2]
    return start


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


def overwrite(
    cur: CursorWrapper,
    location_id: int,
    a: datetime,
    b: datetime,
    state: str = "not_monitored",
) -> int:
    """Make the covered time inside ``[a, b)`` ``state``; return the pieces changed.

    The lapse carve (``powermon.engine.lapse``) writes "not_monitored" over a gap in
    monitoring; Phase 5's false-outage removal (DATA-02) writes "on". The caller holds the
    location's ``location_state`` row lock (RESEARCH Pattern 4, verified in spike 11):

    - only time that has data changes: no-data time (before the first heartbeat, K-1) stays
      no data, and a piece that ends at ``a`` or starts at ``b`` is untouched;
    - a piece across an edge is shrunk to end at ``a`` or to start at ``b``, a piece inside
      the window is replaced, and a piece across the whole window keeps both ends, each
      with its own state and ``outage_start_at`` (an outage in progress stays one outage,
      INV-11 #2); the covered middle gets ``state`` with no outage start;
    - a piece already in ``state`` is skipped, so a re-run is a no-op and a later ``b``
      only appends.

    Statement order follows the close-before-open rule of ``set_open_state``: each piece is
    shrunk or deleted before the pieces that replace it are inserted, so the exclusion
    constraint and the one-open unique index hold after every statement. ``a >= b``
    changes nothing; a state other than "not_monitored" or "on" raises ValueError before
    any write.
    """
    if state not in OVERWRITE_STATES:
        raise ValueError(f"overwrite writes {OVERWRITE_STATES}, not {state!r}")
    if not a < b:
        return 0
    cur.execute(OVERLAP_SQL, {"location_id": location_id, "a": a, "b": b})
    changed = 0
    for piece_id, piece_state, start, end, outage_start in cur.fetchall():
        if piece_state == state:
            continue
        left = start < a
        right = end is None or end > b
        middle = (max(start, a), b if end is None else min(end, b))
        if left:
            cur.execute(SHRINK_END_SQL, [a, piece_id])
        elif right:
            cur.execute(SHRINK_START_SQL, [b, piece_id])
        else:
            cur.execute(DELETE_SQL, [piece_id])
        cur.execute(INSERT_PIECE_SQL, [location_id, state, middle[0], middle[1], None])
        if left and right:
            # The piece spans the whole window: its tail resumes at b, unchanged.
            cur.execute(INSERT_PIECE_SQL, [location_id, piece_state, b, end, outage_start])
        changed += 1
    return changed
