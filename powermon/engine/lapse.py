"""The lapse rules: server downtime is never reported as a power outage (MON-05, D-04).

This module is the only home of the lapse rules (D-04, D-15, INV-10, INV-11).

A *lapse* is any gap in monitoring: the whole stack down, a worker start or deploy, a
crash, a stall of the detection loop, the database unreachable, or a wall-clock step. The
cursor is ``system_state.last_cycle_completed_at``. When a lapse is found, the carve:

1. Overwrites ``[cursor, now)`` with ``not_monitored`` on the stored timeline of every
   monitored location (``carve_window``, through ``timeline.overwrite``). Each location
   gets its own transaction, which first locks that location's ``location_state`` row and
   then rewrites only that location's intervals. No-data time stays no data, and an
   outage already in progress stays one outage: the off pieces on both sides keep their
   ``outage_start_at`` (INV-11 #2, D-02).
2. Then commits the cursor in one transaction (``record_gap``). That transaction moves
   ``last_cycle_completed_at`` and ``detection_resumed_at`` to ``now``, records one
   closed ``monitoring_gap`` incident and sends one gap notice to the admin (D-11 #1,
   OPS-02). The cursor UPDATE is conditional on the cursor that was read, so a second
   carver, or a re-run after a crash, adds no second notice. Moving
   ``detection_resumed_at`` gives every location that is on a fresh detection window, so
   subscribers get nothing for the gap (INV-10).

Crash safety: a crash between the overwrites and the cursor transaction leaves the cursor
where it was. The next carve covers ``[cursor, now')``, re-runs the overwrite (a no-op
over the pieces already ``not_monitored``) and sends the one notice, with the longer
window.

Lock ordering (no deadlock): every transaction that writes ``power_interval`` locks
exactly one ``location_state`` row, and takes that lock first. That covers heartbeats,
``mark_off`` and the per-location carve. The cursor transaction touches only
``system_state``, ``ops_incident`` and ``outbox_message``, never ``location_state``.

An error while carving one location is logged with its id, and the carve of the others and
the cursor commit go on (INV-13 #4). Only a database error that shows the connection is
gone aborts the carve; the next cycle then re-runs it, before the cursor ever moved.

Time comes only from the arguments: nothing here reads a clock (the injected Clock
belongs to the caller, ``powermon.worker.detection.run_detection``). SQL constants use
bound parameters only.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import InterfaceError, OperationalError, connection, transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident
from powermon.engine import timeline
from powermon.engine.models import SystemState

log = logging.getLogger(__name__)

# A gap between completed cycles longer than this is a lapse (D-15, INV-10). It stays below
# the smallest effective timeout the instance allows (period and grace >= 10 s each, so
# 20 s), and one missed 5 s cycle (a 10 s gap) is not a lapse.
LAPSE_THRESHOLD = timedelta(seconds=15)
# Wall-clock and monotonic time since the previous cycle may disagree by this much before
# the cycle counts as a clock step (D-15; NTP slewing stays far below it).
CLOCK_STEP_LIMIT_S = 5.0
KIND_MONITORING_GAP = "monitoring_gap"

# Every location whose timeline may hold monitored time: on or off and not deleted
# ("waiting" has no intervals, and a location in maintenance is skipped by overwrite).
CARVE_LOCATIONS_SQL = """
SELECT s.location_id
  FROM location_state s
  JOIN location l ON l.id = s.location_id
 WHERE s.status IN ('on', 'off') AND l.deleted_at IS NULL
 ORDER BY s.location_id
"""
# The per-location mutex every timeline writer takes first (RESEARCH Pattern 6).
LOCK_SQL = "SELECT 1 FROM location_state WHERE location_id = %s FOR UPDATE"
# The first ever cycle (and the first Phase 2 start, Pitfall 13): nothing was monitored
# before, so there is no gap, only a window that starts now.
START_FRESH_SQL = """
UPDATE system_state
   SET last_cycle_completed_at = %(now)s, detection_resumed_at = %(now)s
 WHERE id = 1 AND last_cycle_completed_at IS NULL
"""
# Move the cursor only if nobody else did: a second carver, or a re-run, adds no second
# notice (RESEARCH Pattern 5).
CURSOR_CAS_SQL = """
UPDATE system_state
   SET last_cycle_completed_at = %(now)s, detection_resumed_at = %(now)s
 WHERE id = 1 AND last_cycle_completed_at IS NOT DISTINCT FROM %(cursor)s
"""


@dataclass(frozen=True)
class Gap:
    """A recorded gap in monitoring: ``[start, end)`` is not monitored."""

    start: datetime
    end: datetime


@dataclass
class CycleTracker:
    """What the detection loop's previous lapse check saw (one per worker process).

    The forced carve triggers of D-04 compare against it: a new lease ``generation``, a
    new backend ``pid`` of the detection connection (a re-established session), and
    ``db_failed`` (the loop lost its connection since). ``wall``/``mono`` are the previous
    cycle's wall-clock and monotonic times, for the clock-step check (D-15).
    """

    # 0: no lease generation seen yet in this process, so the first one forces a carve.
    generation: int = 0
    pid: int | None = None
    db_failed: bool = False
    wall: datetime | None = None
    mono: float | None = None

    def clock_step(self, now: datetime, mono: float) -> float | None:
        """Seconds the wall clock moved beyond the monotonic clock since the last cycle.

        Positive for a forward step, negative for a backward one; None on the first cycle.
        """
        if self.wall is None or self.mono is None:
            return None
        return (now - self.wall).total_seconds() - (mono - self.mono)

    def remember(self, generation: int, pid: int, now: datetime, mono: float) -> None:
        """Record a completed lapse check; the failure flag is cleared with it."""
        self.generation = generation
        self.pid = pid
        self.wall = now
        self.mono = mono
        self.db_failed = False


def read_cursor() -> datetime | None:
    """``system_state.last_cycle_completed_at``; the singleton is recreated if missing."""
    system, _created = SystemState.objects.get_or_create(pk=1)
    return system.last_cycle_completed_at


def start_fresh(now: datetime) -> bool:
    """Set the cursor and the detection window to ``now`` if no cycle was ever recorded.

    No carve and no notice: nothing was monitored before. Returns whether it wrote.
    """
    with connection.cursor() as cur:
        cur.execute(START_FRESH_SQL, {"now": now})
        return cur.rowcount == 1


def carve_window(a: datetime, b: datetime, tick: Callable[[], None] | None = None) -> int:
    """Record ``[a, b)`` as not monitored for every monitored location; return how many changed.

    Each location in its own transaction, which locks its ``location_state`` row first.
    ``tick`` (the watchdog's progress stamp) runs after each location. A database error on
    a connection that is no longer usable is raised (the cursor must not move past a carve
    that did not happen); any other error is logged with the location id and the carve goes
    on with the next location (INV-13 #4).
    """
    with connection.cursor() as cur:
        cur.execute(CARVE_LOCATIONS_SQL)
        location_ids = [row[0] for row in cur.fetchall()]
    changed = 0
    for location_id in location_ids:
        try:
            if _carve_one(location_id, a, b):
                changed += 1
        except OperationalError, InterfaceError:
            if not connection.is_usable():
                raise
            log.exception("lapse carve failed for location %s", location_id)
        except Exception:
            log.exception("lapse carve failed for location %s", location_id)
        finally:
            if tick is not None:
                tick()
    return changed


def _carve_one(location_id: int, a: datetime, b: datetime) -> bool:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(LOCK_SQL, [location_id])
        if cur.fetchone() is None:
            return False
        return timeline.overwrite(cur, location_id, a, b, "not_monitored") > 0


def record_gap(cursor: datetime, now: datetime) -> bool:
    """Commit the cursor, one closed gap incident and one gap notice; False if beaten to it.

    One transaction (D-04, RESEARCH Pattern 5): the conditional cursor UPDATE first. If it
    changes no row, another carver (or a re-run) already recorded this gap, and nothing
    else is written. Otherwise ``last_cycle_completed_at`` and ``detection_resumed_at``
    move to ``now``, a ``monitoring_gap`` incident ``[cursor, now]`` is opened and closed in
    the same statement, and the gap notice is queued (or, with no ops chat, logged) with
    the exact window in integer epoch microseconds (OPS-02). It never touches
    ``location_state`` (lock ordering).
    """
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(CURSOR_CAS_SQL, {"cursor": cursor, "now": now})
        if cur.rowcount != 1:
            return False
        OpsIncident.objects.create(
            kind=KIND_MONITORING_GAP, location=None, started_at=cursor, ended_at=now
        )
        ops.notify(
            outbox.KIND_OPS_GAP,
            payload={"start_us": ops.instant_us(cursor), "end_us": ops.instant_us(now)},
            recorded_at=now,
        )
    return True


def carve_if_needed(
    now: datetime, *, force: bool, tick: Callable[[], None] | None = None
) -> Gap | None:
    """Record the gap since the last completed cycle, if there is one; return it.

    - No cycle was ever recorded: start fresh at ``now`` (no carve, no notice).
    - ``now`` is not after the cursor (a backward clock step): the window is empty, and
      history before the cursor is never rewritten.
    - Otherwise the gap is carved when ``force`` is set (a new lease generation, a
      re-established connection, a failed cycle or a forward clock step, D-04), or when it
      is longer than LAPSE_THRESHOLD (strict ``>``).

    The overwrites commit before the cursor transaction, so a crash in between re-runs the
    carve idempotently. Returns None when no gap was recorded, including when another
    carver recorded it first.
    """
    cursor = read_cursor()
    if cursor is None:
        if start_fresh(now):
            log.info("detection starts at %s: no earlier cycle is recorded", now.isoformat())
        return None
    if now <= cursor:
        return None
    if not force and now - cursor <= LAPSE_THRESHOLD:
        return None
    changed = carve_window(cursor, now, tick)
    if not record_gap(cursor, now):
        return None
    log.info(
        "monitoring gap %s - %s recorded as not monitored (%d location(s) rewritten)",
        cursor.isoformat(),
        now.isoformat(),
        changed,
    )
    return Gap(cursor, now)
