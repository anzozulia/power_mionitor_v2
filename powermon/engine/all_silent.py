"""All-silent: every active location silent at once is told to the admin (OPS-04, INV-12).

*All-silent* (D-01, INV-12) starts when there are at least 2 active locations (status on
or off, not in maintenance, not deleted) and each of them has gone longer than its own
heartbeat period without a heartbeat, counted from max(last heartbeat, end of the last
lapse). ``system_state.detection_resumed_at`` is that lapse end. Silence is strict: at
exactly its period a location is not silent yet. The incident starts at the latest of
those counting points, the moment the last location fell quiet. It ends when any active
location's heartbeat arrives after that start. The admin gets exactly one start notice
and one end notice per incident.

No hold (D-01, Pitfall 11): subscriber alerts are untouched. Ukrainian queue blackouts
really do take several locations off the grid at once, so silence everywhere is either an
area outage or a server/network problem, and every location still gets its OFF at its own
timeout and its ON at its first heartbeat. Only the admin is told, in neutral words (D-12,
``powermon.alerts.ops_texts``).

The end is heartbeat-based on purpose. A lapse carve sets the lapse end to now, which
makes every location "not silent" for a period: an end rule of "nobody is silent" would
close the incident on every worker restart and open a new one a minute later. Off
locations stay in the active set because they are monitored; without them the set would
empty itself as the silent locations time out. When the active set drops below 2 while an
incident is open, only a heartbeat ends it (Phase 2; Phase 4 decides about maintenance).

Exactly one notice per incident is the database's job (D-11, ARCHITECTURE Pattern 10):
``ops.open_incident`` is ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` against the
partial unique index ``ops_incident_one_open``, and ``ops.close_incident`` is a
conditional UPDATE decided by its row count. Each runs in the same transaction as its
notice, so two evaluations at once (or a re-run after a restart) open at most one
incident and send at most one start and one end notice.

``silence_since`` and ``first_back`` are pure; ``evaluate(now)`` reads and writes the
database in one transaction and is called by the detection cycle after its decisions
(``powermon.worker.detection.run_detection``). Time comes only from the arguments.
"""
