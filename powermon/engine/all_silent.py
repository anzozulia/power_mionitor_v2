"""All-silent: every active location silent at once is told to the admin (OPS-04, INV-12).

*All-silent* (D-01, INV-12) starts when there are at least 2 active locations (status on
or off, not in maintenance, not deleted) and each of them has gone longer than its own
heartbeat period without a heartbeat, counted from max(last heartbeat, end of the last
lapse). ``system_state.detection_resumed_at`` is that lapse end. Silence is strict: at
exactly its period a location is not silent yet. The incident starts at the latest of
those counting points, the moment the last location fell quiet, and is opened (detected)
about one period later. The admin gets exactly one start notice and one end notice per
incident.

An open incident ends at the first of these heartbeats (D-04, refined after the wave-1
audit):
- one after its start from an active location (the Phase 2 rule: that heartbeat breaks
  the silence itself);
- one received after it was opened from a location in maintenance (monitored, not
  deleted). Such a heartbeat proves the server and network path work, so the end
  candidates are wider than the active set that starts it. It must come after the open,
  not just after the start, because the start is backdated: a device in maintenance
  usually beat between the start and the detection, and that beat says nothing about the
  path now. In the INV-12 #1 shape (ingress down while the devices stay powered) counting
  it closed the incident at the next evaluation with a false "Heartbeats are back" while
  the outage went on, and one incident per silence (below) then swallowed the real
  recovery notice.

The open time is stored with the incident by the transaction that opens it, as integer
microseconds ``opened_us`` in ``ops_incident.details`` (integers only, OPS-08). An
incident without a valid ``opened_us`` (opened before this rule) gets the time of the
first evaluation that sees it as its open time, stored the same way. That stand-in is
never earlier than the real open, so it can only ignore more maintenance heartbeats, never
end the incident falsely, and a device in maintenance that keeps beating still ends it.

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
incident is open, only a heartbeat ends it. Putting locations into maintenance or deleting
them never ends it by itself (D-04), and a deleted or waiting location's heartbeat time
never ends it. A location in maintenance whose device keeps beating ends an incident at
its first beat after the open (Pitfall 7, T-04-14 accepted): the admin then gets the start
and the end notice about one beat apart.

One incident per silence. Before D-04 an end always came from an active location, whose
heartbeat broke the silence. A heartbeat from a location in maintenance leaves the active
locations silent, so the start rule would hold again at once, with the same start, and
the stored heartbeat would end that incident too: a start and an end notice every cycle
for as long as the silence lasts. So a silence that starts no later than the latest
all-silent incident (already reported) opens no second one. A new silence opens one: an
active location that beat after it, a lapse end or a location that joined the active set
moves the start later.

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

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.models import Max

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident
from powermon.engine.models import SystemState

log = logging.getLogger(__name__)

KIND_ALL_SILENT = "all_silent"
# All-silent needs at least this many active locations (INV-12).
MIN_ACTIVE = 2
# The details key holding when the incident was opened (detected), integer microseconds.
OPENED_KEY = "opened_us"

# The active locations: monitored (on or off), not in maintenance, not deleted. Raw SQL in
# the style of transitions.SNAPSHOT_SQL.
ACTIVE_SQL = """
SELECT s.location_id, l.period_s, s.last_heartbeat_at
  FROM location_state s
  JOIN location l ON l.id = s.location_id
 WHERE s.status IN ('on', 'off') AND NOT l.maintenance AND l.deleted_at IS NULL
 ORDER BY s.location_id
"""

# The end candidates (D-04): every monitored, non-deleted location, in maintenance or not.
# ACTIVE_SQL's columns in the same order plus the maintenance flag, which decides whether
# a heartbeat counts after the start (active) or only after the open (maintenance). One
# statement, so a location toggled meanwhile is seen once, with one flag.
END_SQL = """
SELECT s.location_id, l.period_s, s.last_heartbeat_at, l.maintenance
  FROM location_state s
  JOIN location l ON l.id = s.location_id
 WHERE s.status IN ('on', 'off') AND l.deleted_at IS NULL
 ORDER BY s.location_id
"""

# Stores the open time of an open incident in its details (parameters: OPENED_KEY, the
# time in integer microseconds, the incident id), keeping any other key.
OPENED_SQL = """
UPDATE ops_incident
   SET details = details || jsonb_build_object(%s::text, %s::bigint)
 WHERE id = %s AND ended_at IS NULL
"""


@dataclass(frozen=True)
class Active:
    """One location as the all-silent check sees it (ACTIVE_SQL, or END_SQL for the end)."""

    location_id: int
    period_s: int
    # Always set for a location that is on or off; None is handled as "cannot tell".
    last_heartbeat_at: datetime | None
    # Only an end candidate can be in maintenance (END_SQL); its heartbeat ends an
    # incident only if it came after the incident was opened (D-04).
    maintenance: bool = False


def _quiet_since(row: Active, lapse_end: datetime | None) -> datetime | None:
    """When the location's current silence is counted from: max(last heartbeat, lapse end)."""
    known = [at for at in (row.last_heartbeat_at, lapse_end) if at is not None]
    return max(known) if known else None


def silence_since(
    rows: Sequence[Active], lapse_end: datetime | None, now: datetime
) -> datetime | None:
    """When all-silent started, or None if it has not (pure, INV-12).

    It has started when there are at least MIN_ACTIVE rows and every one of them has been
    quiet for longer than its own period at ``now`` (strict ``>``), counted from max(last
    heartbeat, ``lapse_end``); it started at the latest of those counting points. A row
    with neither a heartbeat nor a lapse end cannot be measured, so nothing starts.
    """
    if len(rows) < MIN_ACTIVE:
        return None
    started: datetime | None = None
    for row in rows:
        since = _quiet_since(row, lapse_end)
        if since is None or now - since <= timedelta(seconds=row.period_s):
            return None
        if started is None or since > started:
            started = since
    return started


def first_back(
    rows: Sequence[Active], since: datetime, opened_at: datetime | None = None
) -> Active | None:
    """The row whose heartbeat ends the incident first; the lowest id on a tie (pure, D-04).

    An active row's heartbeat counts after ``since`` (the start); a row in maintenance
    counts only with a heartbeat after ``opened_at`` (the open), and never without one.
    """
    found = _first_heartbeat_after(rows, since, opened_at)
    return None if found is None else found[1]


def _first_heartbeat_after(
    rows: Sequence[Active], since: datetime, opened_at: datetime | None
) -> tuple[datetime, Active] | None:
    back: list[tuple[datetime, int, Active]] = []
    for row in rows:
        after = opened_at if row.maintenance else since
        at = row.last_heartbeat_at
        if after is not None and at is not None and at > after:
            back.append((at, row.location_id, row))
    if not back:
        return None
    first_at, _location_id, first = min(back, key=lambda entry: (entry[0], entry[1]))
    return first_at, first


def _stored_open_time(details: object) -> datetime | None:
    """The open time stored in an incident's details; None if missing or malformed (pure)."""
    value = details.get(OPENED_KEY) if isinstance(details, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return ops.from_instant_us(value)
    except ValueError:
        return None


def evaluate(now: datetime) -> str | None:
    """Start or end all-silent at ``now``: "started", "ended" or None (D-01, D-04, D-11, D-12).

    One transaction. With an open all-silent incident, the first heartbeat that ends it
    (``first_back`` over ``_end_candidates``: an active location's after its start, or a
    location in maintenance's after its open) closes it at that heartbeat and queues (or
    logs) one end notice naming that location. An open incident without a stored open
    time gets ``now`` as its open time first (module docstring). With none open, a started
    silence among the active locations (``_active``) opens one at its start time, stores
    ``now`` as its open time and queues one start notice with the number of active
    locations. The notice is sent only by the evaluation whose open or close changed the
    database, so concurrent or repeated evaluations give at most one notice each way.
    """
    with transaction.atomic():
        incident = (
            OpsIncident.objects.filter(
                kind=KIND_ALL_SILENT, location__isnull=True, ended_at__isnull=True
            )
            .values_list("id", "started_at", "details")
            .first()
        )
        if incident is not None:
            incident_id, since, details = incident
            opened_at = _stored_open_time(details)
            if opened_at is None:
                # Opened before the open time was stored (or the value is malformed):
                # this evaluation stands in for the open, never earlier than the real one.
                _store_open_time(incident_id, now)
                opened_at = now
            return _end(incident_id, since, opened_at, _end_candidates(), now)
        lapse_end = (
            SystemState.objects.filter(pk=1).values_list("detection_resumed_at", flat=True).first()
        )
        reported = OpsIncident.objects.filter(
            kind=KIND_ALL_SILENT, location__isnull=True
        ).aggregate(latest=Max("started_at"))["latest"]
        return _start(_active(), lapse_end, now, reported)


def _start(
    rows: Sequence[Active],
    lapse_end: datetime | None,
    now: datetime,
    reported: datetime | None = None,
) -> str | None:
    """Open the incident at the silence's start, with one notice, if a new silence started.

    ``reported`` is the start of the latest all-silent incident. A silence that starts no
    later than that was already reported, and its incident has ended: it opens no second
    one (one incident per silence, D-04). ``now`` is stored as the open time (D-04).
    """
    since = silence_since(rows, lapse_end, now)
    if since is None or (reported is not None and since <= reported):
        return None
    incident_id = ops.open_incident(KIND_ALL_SILENT, since)
    if incident_id is None:
        return None
    _store_open_time(incident_id, now)
    ops.notify(
        outbox.KIND_OPS_ALL_SILENT_START,
        payload={"since_us": ops.instant_us(since), "count": len(rows)},
        recorded_at=now,
    )
    log.info("all-silent: %d active locations silent since %s", len(rows), since.isoformat())
    return "started"


def _end(
    incident_id: int,
    since: datetime,
    opened_at: datetime,
    rows: Sequence[Active],
    now: datetime,
) -> str | None:
    """Close the open incident at the first heartbeat that ends it, with one notice."""
    found = _first_heartbeat_after(rows, since, opened_at)
    if found is None:
        return None
    back_at, first = found
    if not ops.close_incident(incident_id, back_at):
        return None
    ops.notify(
        outbox.KIND_OPS_ALL_SILENT_END,
        payload={"since_us": ops.instant_us(since), "first_us": ops.instant_us(back_at)},
        recorded_at=now,
        location_id=first.location_id,
    )
    log.info(
        "all-silent ended: first heartbeat from location %s at %s",
        first.location_id,
        back_at.isoformat(),
    )
    return "ended"


def _store_open_time(incident_id: int, opened_at: datetime) -> None:
    """Store when the open incident was opened, in the caller's transaction (OPENED_SQL)."""
    with connection.cursor() as cur:
        cur.execute(OPENED_SQL, [OPENED_KEY, ops.instant_us(opened_at), incident_id])


def _active() -> list[Active]:
    """The active locations, which start an incident (ACTIVE_SQL)."""
    with connection.cursor() as cur:
        cur.execute(ACTIVE_SQL)
        return [Active(*row) for row in cur.fetchall()]


def _end_candidates() -> list[Active]:
    """The locations whose heartbeat ends an incident, maintenance included (END_SQL, D-04)."""
    with connection.cursor() as cur:
        cur.execute(END_SQL)
        return [Active(*row) for row in cur.fetchall()]
