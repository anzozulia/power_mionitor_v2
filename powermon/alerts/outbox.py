"""The alert outbox: queueing alerts and the relay's row transitions (KD2, D-14).

``enqueue`` (subscriber alerts) and ``enqueue_ops`` (admin ops notices, D-09) write through
the ORM on the caller's connection, inside the caller's ``transaction.atomic()`` block: the
row commits together with the transition that caused it, or not at all. They do no
network I/O, and a payload holds integers only, so no text or secret is ever stored
(OPS-08).

Two channels share the table. "subscriber" rows are drained per location, oldest first
(``subscriber_heads``). "ops" rows form one queue for the env-configured admin chat
(``ops_head``), drained after the subscriber heads of each pass (INV-20 #2).

The worker relay (``powermon.worker.io_loop``) moves a row of either channel through its
statuses with the functions below. Each is one conditional UPDATE in Django's autocommit
mode, so it is committed before the relay makes its HTTP call and no transaction is open
during it:

    pending --claim--> sending --mark_sent--> sent
       ^                  |----mark_uncertain--> uncertain (never resent, INV-16)
       '---mark_retry-----'
    sending --recover_interrupted (worker activation)--> uncertain

A subscriber row that becomes uncertain queues one ``ops_uncertain`` notice in the same
transaction (``powermon.alerts.ops``, D-11 #5); an uncertain ops row is only logged.
``last_error`` is always a short code (at most 64 characters), never a URL or a token.
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from django.db import connection
from django.db.models import F

from powermon.alerts.models import OPEN_STATUSES, OutboxMessage

KIND_POWER_OFF = "power_off"
KIND_POWER_ON = "power_on"
KINDS = (KIND_POWER_OFF, KIND_POWER_ON)
CHANNEL_SUBSCRIBER = "subscriber"
CHANNEL_OPS = "ops"
# Ops notice kinds (D-11) and their integer payloads; the text is rendered at send time.
KIND_OPS_GAP = "ops_gap"  # {start_us, end_us}
KIND_OPS_ALL_SILENT_START = "ops_all_silent_start"  # {since_us, count}
KIND_OPS_ALL_SILENT_END = "ops_all_silent_end"  # {since_us, first_us}; location = first
KIND_OPS_EXPIRED = "ops_expired"  # {message_id}; location = the alert's
KIND_OPS_UNCERTAIN = "ops_uncertain"  # {message_id}; location = the alert's
OPS_KINDS = (
    KIND_OPS_GAP,
    KIND_OPS_ALL_SILENT_START,
    KIND_OPS_ALL_SILENT_END,
    KIND_OPS_EXPIRED,
    KIND_OPS_UNCERTAIN,
)
# Written into expires_at now; enforced in Phase 2 (ALRT-03, default maximum age 6 h).
MAX_AGE = timedelta(hours=6)
# The database column is varchar(64).
MAX_ERROR_LENGTH = 64

RECOVER_SQL = """
UPDATE outbox_message SET status = 'uncertain', last_error = 'interrupted'
 WHERE status = 'sending'
RETURNING id, channel, location_id
"""


class RowRef(NamedTuple):
    """Which row a transition changed: enough to queue a notice about it."""

    id: int
    channel: str
    location_id: int | None


def enqueue(
    kind: str,
    location_id: int,
    *,
    event_at: datetime,
    recorded_at: datetime,
    payload: dict[str, int],
) -> OutboxMessage:
    """Queue one subscriber alert, due at once, as part of the caller's transaction.

    ``payload`` holds only integer microsecond durations (``was_on_us`` /
    ``was_off_us``); the text is rendered at send time. An unknown kind raises
    ValueError and a non-integer payload value raises TypeError, before any write.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown alert kind: {kind!r}")
    _check_payload(payload, "must be integer microseconds")
    return OutboxMessage.objects.create(
        channel=CHANNEL_SUBSCRIBER,
        location_id=location_id,
        kind=kind,
        event_at=event_at,
        recorded_at=recorded_at,
        payload=dict(payload),
        status="pending",
        next_attempt_at=recorded_at,
        expires_at=recorded_at + MAX_AGE,
    )


def check_ops_notice(kind: str, payload: dict[str, int]) -> None:
    """ValueError for a kind outside OPS_KINDS, TypeError for a non-integer payload value."""
    if kind not in OPS_KINDS:
        raise ValueError(f"unknown ops notice kind: {kind!r}")
    _check_payload(payload, "must be an integer")


def enqueue_ops(
    kind: str,
    *,
    payload: dict[str, int],
    recorded_at: datetime,
    location_id: int | None = None,
) -> OutboxMessage:
    """Queue one ops notice for the admin chat, due at once, in the caller's transaction.

    Use ``powermon.alerts.ops.notify``, which decides whether the notice is queued or,
    with no ops chat configured, logged (D-09). ``payload`` holds integers only (epoch
    microseconds, a count, a message id); names and texts are read at send time (OPS-08).
    The kind and payload are checked before any write.
    """
    check_ops_notice(kind, payload)
    return OutboxMessage.objects.create(
        channel=CHANNEL_OPS,
        location_id=location_id,
        kind=kind,
        event_at=recorded_at,
        recorded_at=recorded_at,
        payload=dict(payload),
        status="pending",
        next_attempt_at=recorded_at,
        expires_at=recorded_at + MAX_AGE,
    )


def subscriber_heads() -> list[OutboxMessage]:
    """Each location's oldest open subscriber row, with its location loaded.

    Only the head of a location may be sent, so OFF always goes before ON. A head in
    "sending" was left by an interrupted send; it holds its location's line until worker
    activation turns it into "uncertain". PostgreSQL ``DISTINCT ON (location_id)``, served
    by the partial index ``outbox_open_idx``.
    """
    return list(
        OutboxMessage.objects.filter(channel=CHANNEL_SUBSCRIBER, status__in=OPEN_STATUSES)
        .select_related("location")
        .order_by("location_id", "id")
        .distinct("location_id")
    )


def ops_head() -> OutboxMessage | None:
    """The oldest open ops row, or None: the ops queue is one line, oldest first.

    Like a subscriber head, an ops row left in "sending" holds the line until worker
    activation turns it into "uncertain".
    """
    return (
        OutboxMessage.objects.filter(channel=CHANNEL_OPS, status__in=OPEN_STATUSES)
        .order_by("id")
        .first()
    )


def claim(message_id: int) -> bool:
    """Move a pending row to "sending" and count the attempt; False if it was not pending."""
    claimed = OutboxMessage.objects.filter(pk=message_id, status="pending").update(
        status="sending", attempts=F("attempts") + 1
    )
    return claimed == 1


def mark_sent(message_id: int, now: datetime) -> bool:
    """Telegram accepted the claimed row."""
    updated = OutboxMessage.objects.filter(pk=message_id, status="sending").update(
        status="sent", sent_at=now, last_error=""
    )
    return updated == 1


def mark_uncertain(message_id: int, code: str) -> bool:
    """The claimed row may have reached Telegram: it is never sent again (INV-16)."""
    updated = OutboxMessage.objects.filter(pk=message_id, status="sending").update(
        status="uncertain", last_error=_short(code)
    )
    return updated == 1


def mark_retry(message_id: int, next_attempt_at: datetime, code: str) -> bool:
    """Put an open row back to "pending", due again at ``next_attempt_at``."""
    updated = OutboxMessage.objects.filter(pk=message_id, status__in=OPEN_STATUSES).update(
        status="pending", next_attempt_at=next_attempt_at, last_error=_short(code)
    )
    return updated == 1


def recover_interrupted() -> list[RowRef]:
    """Turn rows left in "sending" by a stopped worker into "uncertain"; return them by id.

    Such a send may have reached Telegram, so it is never repeated (at-most-once, INV-16).
    One UPDATE ... RETURNING, so the caller can queue one notice per subscriber row in the
    same transaction. Only the active worker calls this, through
    ``powermon.alerts.ops.recover_interrupted`` on activation, before its loops start.
    """
    with connection.cursor() as cur:
        cur.execute(RECOVER_SQL)
        return sorted(RowRef(*row) for row in cur.fetchall())


def _check_payload(payload: dict[str, int], rule: str) -> None:
    for key, value in payload.items():
        # bool is an int subclass, but True is neither a duration nor an id.
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"payload {key!r} {rule}")


def _short(code: str) -> str:
    return code[:MAX_ERROR_LENGTH]
