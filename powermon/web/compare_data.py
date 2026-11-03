"""The Compare page's reads of the stored timeline (quick task 261103-lfm, D-10).

The page reads ``power_interval`` only (KD1): the timeline that drives the alerts, the
Telegram chart and the totals. It never reads heartbeats, never reads ``location_state``
beyond the live rows the caller already has (``live.live_rows``), and never touches the
outbox, so it writes, sends and queues nothing.

- Query budget: the page makes 4 data queries whatever the number of locations, the two
  of ``live.live_rows`` plus the two here: ``PIECES_SQL`` (every selected location's pieces
  overlapping P, one ``location_id = ANY(...)`` overlap query in start order) and
  ``OUTAGES_SQL`` (every off piece of every outage that has an off piece overlapping P, so
  an outage that started before P keeps its whole off time). Named parameters only.
- Snapshot: both queries run in one read-only REPEATABLE READ transaction when this is the
  outermost transaction, so an outage removal or a history reset that commits between them
  can never split the two reads. Inside a caller's transaction they run in that
  transaction without the ``SET``, because PostgreSQL refuses ``SET TRANSACTION`` after a
  transaction's first query (the caveat of ``engine.restore.fingerprint``).
- Whole outages come from ``history.group_outages``, the same number as the location
  page's Recent outages.
- Orphans: an off piece whose outage has no grouped record (a race the snapshot should
  prevent) is dropped and treated as no data, with one WARNING carrying the count and the
  location id only (OPS-08: never the name). The page never answers 500 for it.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from django.db import connection, transaction

from powermon.chart import compare, model
from powermon.engine import history
from powermon.web import live

logger = logging.getLogger(__name__)

# Every selected location's pieces that overlap [a, b), by location then oldest first. The
# predicate of powermon.chart.source.WEEK_SQL.
PIECES_SQL = """
SELECT location_id, state, start_at, end_at, outage_start_at
  FROM power_interval
 WHERE location_id = ANY(%(ids)s) AND start_at < %(b)s
   AND (end_at IS NULL OR end_at > %(a)s)
 ORDER BY location_id, start_at
"""

# Every off piece of every outage of the selected locations that has an off piece
# overlapping [a, b), also the pieces outside [a, b).
OUTAGES_SQL = """
SELECT location_id, start_at, end_at, outage_start_at
  FROM power_interval
 WHERE state = 'off' AND location_id = ANY(%(ids)s)
   AND (location_id, outage_start_at) IN (
       SELECT location_id, outage_start_at FROM power_interval
        WHERE location_id = ANY(%(ids)s) AND state = 'off'
          AND start_at < %(b)s AND (end_at IS NULL OR end_at > %(a)s))
 ORDER BY location_id, outage_start_at, start_at
"""

READ_SNAPSHOT_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

_Row = tuple[Any, ...]


def _read(ids: list[int], period: compare.Period) -> tuple[list[_Row], list[_Row]]:
    params: dict[str, Any] = {"ids": ids, "a": period.start, "b": period.end}
    with connection.cursor() as cur:
        cur.execute(PIECES_SQL, params)
        pieces = cur.fetchall()
        cur.execute(OUTAGES_SQL, params)
        outages = cur.fetchall()
    return pieces, outages


def _snapshot(ids: list[int], period: compare.Period) -> tuple[list[_Row], list[_Row]]:
    """Both reads in one snapshot when outermost, else in the caller's transaction."""
    if connection.in_atomic_block:
        return _read(ids, period)
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(READ_SNAPSHOT_SQL)
        return _read(ids, period)


def _attach(
    location_id: int,
    pieces: Sequence[model.Piece],
    outages: Sequence[compare.OutageRec],
) -> tuple[model.Piece, ...]:
    """``pieces`` without the off pieces whose outage has no record (one WARNING if any)."""
    known = {o.start for o in outages}
    kept = tuple(p for p in pieces if p.state != "off" or p.outage_start in known)
    dropped = len(pieces) - len(kept)
    if dropped:
        logger.warning(
            "compare: dropped %d orphaned off piece(s) of location %s", dropped, location_id
        )
    return kept


def load_series(
    rows: Sequence[live.LiveRow], period: compare.Period, now: datetime
) -> list[compare.Series]:
    """One ``compare.Series`` per row, in ``rows`` order, from two queries in one snapshot.

    No query for empty ``rows`` or an empty period. ValueError for a naive ``now``, before
    any query.
    """
    if now.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    if not rows:
        return []
    if period.end <= period.start:
        return [compare.Series(r.pk, r.name, (), ()) for r in rows]
    ids = [r.pk for r in rows]
    piece_rows, outage_rows = _snapshot(ids, period)
    pieces: dict[int, list[model.Piece]] = {pk: [] for pk in ids}
    for loc, state, start, end, outage_start in piece_rows:
        pieces[loc].append(model.Piece(state, start, end, outage_start))
    off_pieces: dict[int, list[tuple[datetime, datetime | None, datetime]]] = {pk: [] for pk in ids}
    for loc, start, end, outage_start in outage_rows:
        off_pieces[loc].append((start, end, outage_start))
    series = []
    for r in rows:
        current = r.outage_started_at if r.power == "off" else None
        grouped = history.group_outages(off_pieces[r.pk], now=now, current=current)
        recs = tuple(
            sorted(
                (compare.OutageRec(o.start, o.end, o.off_us) for o in grouped),
                key=lambda o: o.start,
            )
        )
        series.append(compare.Series(r.pk, r.name, _attach(r.pk, pieces[r.pk], recs), recs))
    return series
