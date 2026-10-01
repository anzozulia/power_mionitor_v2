"""The alert outbox: queueing alerts and the relay's row transitions (KD2, D-14).

``enqueue`` writes through the ORM on the caller's connection, inside the caller's
``transaction.atomic()`` block: the alert row commits together with the transition that
caused it, or not at all. It does no network I/O.

The worker relay (``powermon.worker.io_loop``) moves a row through its statuses with the
functions below. Each is one conditional UPDATE in Django's autocommit mode, so it is
committed before the relay makes its HTTP call and no transaction is open during it:

    pending --claim--> sending --mark_sent--> sent
       ^                  |----mark_uncertain--> uncertain (never resent, INV-16)
       '---mark_retry-----'
    sending --recover_interrupted (worker activation)--> uncertain

``last_error`` is always a short code (at most 64 characters), never a URL or a token.
"""

from datetime import datetime, timedelta

from django.db.models import F

from powermon.alerts.models import OPEN_STATUSES, OutboxMessage

KIND_POWER_OFF = "power_off"
KIND_POWER_ON = "power_on"
KINDS = (KIND_POWER_OFF, KIND_POWER_ON)
CHANNEL_SUBSCRIBER = "subscriber"
# Written into expires_at now; enforced in Phase 2 (ALRT-03, default maximum age 6 h).
MAX_AGE = timedelta(hours=6)
# The database column is varchar(64).
MAX_ERROR_LENGTH = 64


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
    for key, value in payload.items():
        # bool is an int subclass, but True is not a duration.
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"payload {key!r} must be integer microseconds")
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


def recover_interrupted() -> int:
    """Turn rows left in "sending" by a stopped worker into "uncertain"; return the count.

    Such a send may have reached Telegram, so it is never repeated (at-most-once, INV-16).
    Only the active worker calls this, on activation, before its loops start.
    """
    return OutboxMessage.objects.filter(status="sending").update(
        status="uncertain", last_error="interrupted"
    )


def _short(code: str) -> str:
    return code[:MAX_ERROR_LENGTH]
