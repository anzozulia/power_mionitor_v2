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
same transaction (``powermon.alerts.ops``).

Every function runs on the caller's connection, inside its transaction: the relay calls
``open_failing`` and ``close_failing`` in the transaction that writes the outbox row's
outcome, so the incident and its notice commit with that outcome or not at all. Nothing
here does network I/O, and the incident's details and the notices' payloads hold integers
only (OPS-08).
"""

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident

KIND_DELIVERY_FAILING = "delivery_failing"
# The status a client code with no plausible HTTP status stands for (a notice needs one).
_DEFAULT_STATUS = 400


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
    the notice, so a location refused again while its incident is open adds nothing
    (INV-20 #1).
    """
    details = {"http_status": http_status}
    if migrate_to_chat_id is not None:
        details["migrate_to_chat_id"] = migrate_to_chat_id
    with transaction.atomic():
        opened = ops.open_incident(
            KIND_DELIVERY_FAILING, now, location_id=location_id, details=details
        )
        if opened is None:
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
