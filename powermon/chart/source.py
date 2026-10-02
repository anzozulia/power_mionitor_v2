"""The chart's one read of the stored timeline (KD1, INV-03).

Worker side. The chart reads only ``power_interval``, the timeline that the engine and the
lapse carve write. It never reads heartbeats (they are not stored), ``location_state`` or
the outbox, so the chart, its totals and the alerts all come from the same intervals.

One overlap query per chart, served by the ``power_interval_loc_start`` index and bounded
to the seven shown days: an interval that started before the week, or is still open,
overlaps it and is included. The pure ``model`` then slices the pieces into local days.
"""

from datetime import date, datetime

from django.db import connection

from powermon.chart import model

# The location's pieces that overlap [a, b), oldest first. The same predicate as
# powermon.engine.timeline.OVERLAP_SQL, on UTC instants only.
WEEK_SQL = """
SELECT state, start_at, end_at, outage_start_at
  FROM power_interval
 WHERE location_id = %(location_id)s AND start_at < %(b)s
   AND (end_at IS NULL OR end_at > %(a)s)
 ORDER BY start_at
"""


def read_pieces(location_id: int, start: datetime, end: datetime) -> list[model.Piece]:
    """The location's stored pieces that overlap ``[start, end)``, in start order."""
    with connection.cursor() as cur:
        cur.execute(WEEK_SQL, {"location_id": location_id, "a": start, "b": end})
        rows = cur.fetchall()
    return [model.Piece(state=r[0], start=r[1], end=r[2], outage_start=r[3]) for r in rows]


def load_week(location_id: int, *, today: date, now: datetime, tz: str, live: bool) -> model.Week:
    """The chart week of ``today`` for one location, built from its stored timeline.

    ``now`` must be aware (ValueError otherwise, before any query runs); ``tz`` is the
    display time zone's IANA name.
    """
    start, end = model.week_window(today, now, tz)
    pieces = read_pieces(location_id, start, end)
    return model.build_week(pieces, today=today, now=now, tz=tz, live=live)
