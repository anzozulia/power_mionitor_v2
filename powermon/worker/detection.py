"""One detection cycle of the worker: record lapses, then mark silent locations OFF.

The worker's detection thread calls ``run_detection`` every few seconds while it holds
the lease (MON-02, MON-05, KD2). A cycle only reads and writes the database: no Telegram
call, no sleep (INV-14). An OFF alert or a gap notice goes into the outbox inside its own
transaction, and the relay sends it.

Cycle order (D-04, D-15, RESEARCH Pattern 3):

1. connections: ``close_old_connections()``, then a cursor, so a connection the database
   dropped is replaced before the first statement (D-16, MON-06);
2. the backend pid of the detection connection (it changes only when the session was
   re-established, because the worker's connections are persistent, CONN_MAX_AGE None);
3. the clock-step check: wall-clock against monotonic time since the previous cycle;
4. the lapse carve (``lapse.carve_if_needed``), forced by a new lease generation, a new
   backend pid, a lost connection since the previous cycle or a forward clock step over
   5 s, otherwise run when the gap since the last completed cycle is over 15 s. It commits
   before any timeout decision, and a carve moves ``detection_resumed_at`` to now, so no
   OFF can follow from the gap itself;
5. the cursor: ``last_cycle_completed_at`` moves to now with GREATEST (never backwards)
   and the tracker remembers this cycle. Both happen once the lapse check committed and
   before the decisions, so a decisions step that keeps failing neither keeps a forced
   trigger armed nor lets the cursor fall behind the threshold: it is logged by the loop,
   never a new gap notice on every cycle (plan-check advisory 1);
6. the decisions (``run_cycle``), skipped on a cycle whose clock stepped by more than
   5 s either way. A backward step records nothing, only one WARNING;
7. the all-silent check (``all_silent.evaluate``, OPS-04, INV-12), after the decisions and
   skipped together with them on a clock step. It only informs the admin and never holds
   a subscriber alert (D-01), so it runs after the OFFs of the cycle are recorded. An
   error in it never stops detection: a connectivity error on a connection that is gone
   reaches the loop (one WARNING per outage, and the next cycle carves), any other error
   is logged with its traceback.

``run_cycle`` is the decisions step on its own (Phase 1 shape, INV-13):
- each location in its own ``try``, so one failing location never stops the others. The
  log line names only the location id: no token, key or URL (OPS-08);
- a database connectivity error raised while the connection is no longer usable aborts
  the whole cycle: every later location would fail the same way, so the loop logs one
  WARNING for the outage (``DbOutageLog``) instead of one traceback per location. Any
  other error, a statement timeout on a working connection included, stays one
  location's problem;
- ``tick`` (the watchdog's progress stamp) runs after every location (D-15).
"""

import logging
from collections.abc import Callable
from datetime import datetime

from django.db import InterfaceError, OperationalError, close_old_connections, connection
from django.db.models import Value
from django.db.models.functions import Greatest

from powermon.clock import Clock
from powermon.engine import all_silent, lapse, rules, transitions
from powermon.engine.models import SystemState

log = logging.getLogger(__name__)


def run_detection(
    clock: Clock,
    generation: int,
    tracker: lapse.CycleTracker,
    tick: Callable[[], None] | None = None,
) -> int:
    """One cycle for lease ``generation``: lapse check, decisions, all-silent; the OFFs recorded.

    ``tracker`` carries what the previous cycle of this process saw. A database error is
    raised to the detection loop, which marks ``tracker.db_failed`` when the connection
    was lost, so the next cycle carves the failed window.
    """
    close_old_connections()
    # The health check and, for a dropped session, the reconnect happen here.
    with connection.cursor():
        pass
    pid: int = connection.connection.info.backend_pid
    now, mono = clock.now(), clock.monotonic()
    step = tracker.clock_step(now, mono)
    force = (
        generation != tracker.generation
        or (tracker.pid is not None and pid != tracker.pid)
        or tracker.db_failed
        or (step is not None and step > lapse.CLOCK_STEP_LIMIT_S)
    )
    if step is not None and step > lapse.CLOCK_STEP_LIMIT_S:
        # The carve below records the gap (and logs it); the decisions wait a cycle.
        log.warning("wall clock stepped forward %d s; skipping this cycle's decisions", round(step))
    elif step is not None and step < -lapse.CLOCK_STEP_LIMIT_S:
        # Nothing is recorded (the window before the cursor is history): one line only.
        log.warning("wall clock stepped back %d s; skipping this cycle's decisions", round(-step))
    lapse.carve_if_needed(now, force=force, tick=tick)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(now))
    )
    tracker.remember(generation, pid, now, mono)
    if step is not None and abs(step) > lapse.CLOCK_STEP_LIMIT_S:
        return 0
    recorded = run_cycle(now, tick=tick)
    _check_all_silent(now)
    return recorded


def _check_all_silent(now: datetime) -> None:
    """Run the all-silent check; only a lost connection's error leaves this function."""
    try:
        all_silent.evaluate(now)
    except OperationalError, InterfaceError:
        if connection_lost():
            raise
        log.exception("all-silent evaluation failed")
    except Exception:
        log.exception("all-silent evaluation failed")


def connection_lost() -> bool:
    """True when this thread's database connection is gone (closed or no longer usable).

    The detection loop asks after a connectivity error: only a lost connection forces the
    next cycle's carve, not an error on a connection that still works.
    """
    return connection.connection is None or not connection.is_usable()


def run_cycle(now: datetime, tick: Callable[[], None] | None = None) -> int:
    """Check each monitored location that is on at ``now``; return the OFFs recorded.

    A transition that lost to a concurrent heartbeat is not counted (INV-01). Raises the
    database error that showed the connection is gone; every other error is logged per
    location.
    """
    close_old_connections()
    # get_or_create: the singleton comes from a data migration, but a missing row must
    # not stop detection.
    system, _created = SystemState.objects.get_or_create(pk=1)
    anchors = rules.Anchors(
        detection_resumed_at=system.detection_resumed_at,
        web_started_at=system.web_started_at,
    )
    recorded = 0
    for snap, alerts_enabled in transitions.read_snapshots():
        try:
            decision = rules.decide(snap, anchors, now)
            if decision.off and transitions.mark_off(snap, decision, now, alerts_enabled):
                recorded += 1
        except OperationalError, InterfaceError:
            if not connection.is_usable():
                raise
            log.exception("detection failed for location %s", snap.location_id)
        except Exception:
            log.exception("detection failed for location %s", snap.location_id)
        finally:
            if tick is not None:
                tick()
    return recorded
