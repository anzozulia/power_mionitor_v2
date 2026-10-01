"""One detection cycle of the worker: mark silent locations OFF (MON-02, KD2).

The worker's detection thread calls ``run_cycle`` every few seconds while it holds the
lease. A cycle only reads and writes the database: no Telegram call, no sleep (INV-14).
The OFF alert goes into the outbox inside the transition's own transaction, and the relay
sends it. 02-06 adds ``run_detection`` around this function (reconnect and clock-step
checks, the lapse carve, the cursor); it calls the module-level ``run_cycle``.

Loop-body pattern (INV-13 shape), copied by the relay loop:
- ``close_old_connections()`` first, so a connection the database dropped is replaced
  before the first statement instead of failing every cycle (D-16, MON-06);
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

from powermon.engine import rules, transitions
from powermon.engine.models import SystemState

log = logging.getLogger(__name__)


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
