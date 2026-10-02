"""Delivery health of a location's subscriber alerts (D-10, D-12, OPS-03).

The location's ``delivery_failing`` incident (``ops_incident``) is the single source of
the admin panel's failing badge (LOC-03). It is opened only by a permanent outcome
(400/401/403/404) of a subscriber alert send, and closed by a successful subscriber alert
send or by a recorded success of the admin's test message. A transient error (5xx, a
refused connection), a 429, an ambiguous send and every chart call never open or close it:
long Telegram outages are covered by the expiry notices, and a refused pin has its own
incident (D-10).

Exactly one failing notice and one restored notice per incident is the database's job,
not a code convention: the partial unique index ``ops_incident_one_open`` lets a single
opener get an id back, and the close is conditional on the incident still being open, so
a single closer sees one changed row. Each of them queues its notice only then, in the
same transaction (``powermon.alerts.ops``). A refusal while the incident is open adds no
incident and no notice, but replaces the open incident's details with its own, by one
conditional UPDATE in the same transaction (wave-2 audit). The details always describe
the latest refusal, so a supergroup Telegram reports after the first refusal still
reaches the location page (D-10).

A recorded success (D-12): after the admin's test message went through, the web calls
``record_test_success``, one transaction that closes the incident (one recovery notice)
and makes the location's waiting alerts due at once (``outbox.make_due``). The worker
holds that channel in memory for 15 minutes after a refusal (``RelayState.failing``); it
lifts the hold in its next pass because the incident it holds for is no longer open, so
the queued alerts go out within one pass, with their event times (ALRT-04).

A deleted location never gets a new failing incident or notice (D-09): its delete closes
its open incidents without a recovery notice, and a refusal answered for a send that was
in flight at delete time must not open one again. So ``open_failing`` first reads the
location row ``FOR SHARE`` in the caller's transaction and writes nothing for a
tombstone. ``FOR SHARE`` waits for a concurrent delete's uncommitted UPDATE of that row:
either the delete commits first and nothing opens, or this transaction commits first and
the delete, which runs its incident UPDATE afterwards, closes the new incident itself.
There is no deadlock: both transactions take the location row before they touch
incidents, and the delete's pending-only outbox UPDATE skips the row this transaction
holds, whose committed status is still "sending".

Every function runs on the caller's connection, inside its transaction: the relay calls
``open_failing`` and ``close_failing`` in the transaction that writes the outbox row's
outcome, so the incident and its notice commit with that outcome or not at all. Nothing
here does network I/O, and the incident's details and the notices' payloads hold integers
only (OPS-08). ``failing_incidents`` reads them back for the admin pages and never raises
on a malformed value.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident

KIND_DELIVERY_FAILING = "delivery_failing"
# A live location's row, held against a concurrent delete until the caller commits (D-09).
LIVE_LOCATION_SQL = "SELECT 1 FROM location WHERE id = %s AND deleted_at IS NULL FOR SHARE"
# The open failing incident's details, replaced by the latest refusal's (status and
# reported chat ID). Only an open incident of this kind and location: a closed one keeps
# the details it ended with.
LATEST_REFUSAL_SQL = """
UPDATE ops_incident
   SET details = %s::jsonb
 WHERE kind = %s AND location_id = %s AND ended_at IS NULL
"""
# The status a client code with no plausible HTTP status stands for (a notice needs one).
_DEFAULT_STATUS = 400
# A reported chat ID fits a signed 64-bit integer, as a location's chat_id does.
_MIN_CHAT_ID = -(2**63)
_MAX_CHAT_ID = 2**63 - 1


@dataclass(frozen=True)
class Failing:
    """An open ``delivery_failing`` incident, as the admin panel shows it (D-13)."""

    started_at: datetime
    http_status: int
    migrate_to_chat_id: int | None


def http_status(code: str) -> int:
    """The HTTP status in a client code such as ``http_403``; 400 when it holds none."""
    digits = code.removeprefix("http_")
    if digits.isascii() and digits.isdecimal() and 100 <= int(digits) <= 599:
        return int(digits)
    return _DEFAULT_STATUS


def open_failing(
    location_id: int,
    now: datetime,
    http_status: int,
    migrate_to_chat_id: int | None = None,
) -> bool:
    """Mark the location's delivery failing at ``now``; True if this opened the incident.

    The incident's details and the ``ops_delivery_failing`` notice carry the HTTP status
    and, when Telegram reported one, the supergroup's new chat ID; the location's chat is
    never changed here (PITFALLS 6e). Only the opener that got an incident id back queues
    the notice, so a location refused again while its incident is open gets no second
    incident and no second notice (INV-20 #1).

    That refusal still replaces the open incident's details: they always describe the
    latest refusal, which the admin pages show (D-10, D-13). So a supergroup reported
    after the first refusal reaches the location page, and a refusal without a chat ID
    clears an earlier one. Telegram repeats ``migrate_to_chat_id`` on every send to the
    old group, so the ID stays while the location points there; once the admin has pasted
    it, a refusal from the new chat shows its own cause, not a hint to set a chat ID that
    is already set. A deleted location (or one that is being deleted) gets nothing: False,
    and no write, not even to an incident still open (D-09).
    """
    details = {"http_status": http_status}
    if migrate_to_chat_id is not None:
        details["migrate_to_chat_id"] = migrate_to_chat_id
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(LIVE_LOCATION_SQL, [location_id])
            if cur.fetchone() is None:
                return False
        # Checks that the details are integers (OPS-08) before it writes anything.
        opened = ops.open_incident(
            KIND_DELIVERY_FAILING, now, location_id=location_id, details=details
        )
        if opened is None:
            # Already open: the details follow this refusal, and no notice is queued.
            with connection.cursor() as cur:
                cur.execute(
                    LATEST_REFUSAL_SQL,
                    [json.dumps(details), KIND_DELIVERY_FAILING, location_id],
                )
            return False
        ops.notify(
            outbox.KIND_OPS_DELIVERY_FAILING,
            payload=details,
            recorded_at=now,
            location_id=location_id,
        )
    return True


def close_failing(location_id: int, now: datetime) -> bool:
    """End the location's open failing incident at ``now``; True if this closed it.

    Only the closer whose conditional UPDATE changed the row queues the
    ``ops_delivery_restored`` notice, so the relay's successful send and a recorded test
    message success that meet never notify twice (D-12).
    """
    with transaction.atomic():
        incident = (
            OpsIncident.objects.filter(
                kind=KIND_DELIVERY_FAILING, location_id=location_id, ended_at__isnull=True
            )
            .values_list("id", flat=True)
            .first()
        )
        if incident is None or not ops.close_incident(incident, now):
            return False
        ops.notify(
            outbox.KIND_OPS_DELIVERY_RESTORED,
            payload={},
            recorded_at=now,
            location_id=location_id,
        )
    return True


def record_test_success(location_id: int, now: datetime) -> bool:
    """The admin's test message went through at ``now`` (D-12); True if this closed an incident.

    One transaction: the location's waiting subscriber alerts become due at once, and its
    open failing incident, if any, closes with one recovery notice. The worker sees the
    closed incident in its next pass and lifts its 15-minute hold of the channel, so the
    alerts go out then. A naive ``now`` raises ValueError and nothing changes.
    """
    with transaction.atomic():
        outbox.make_due(location_id, now)
        return close_failing(location_id, now)


def failing_incidents(location_ids: Iterable[int]) -> dict[int, Failing]:
    """The open failing incident of each of these locations that has one, in one query.

    For the admin pages (D-13), which must never fail on a stored value: details that are
    not integers read as status 400 and no reported chat ID.
    """
    ids = list(location_ids)
    if not ids:
        return {}
    rows = OpsIncident.objects.filter(
        kind=KIND_DELIVERY_FAILING, ended_at__isnull=True, location_id__in=ids
    ).values_list("location_id", "started_at", "details")
    found: dict[int, Failing] = {}
    for location_id, started_at, details in rows:
        values = details if isinstance(details, dict) else {}
        status = _int_in(values.get("http_status"), 100, 599)
        migrate_to = _int_in(values.get("migrate_to_chat_id"), _MIN_CHAT_ID, _MAX_CHAT_ID)
        found[location_id] = Failing(
            started_at, _DEFAULT_STATUS if status is None else status, migrate_to
        )
    return found


def _int_in(value: object, low: int, high: int) -> int | None:
    """``value`` if it is an int (not a bool) from ``low`` to ``high``, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None
