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

Time comes only from the arguments: nothing here reads a clock (the injected Clock
belongs to the caller, ``powermon.worker.detection.run_detection``). SQL constants use
bound parameters only.
"""
