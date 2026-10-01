"""Queueing subscriber alerts in the outbox (KD2, D-14).

``enqueue`` writes through the ORM on the caller's connection, inside the caller's
``transaction.atomic()`` block: the alert row commits together with the transition that
caused it, or not at all. It does no network I/O. The worker relay (01-11) sends the row
later, with no transaction open.
"""

from datetime import datetime, timedelta

from powermon.alerts.models import OutboxMessage

KIND_POWER_OFF = "power_off"
KIND_POWER_ON = "power_on"
KINDS = (KIND_POWER_OFF, KIND_POWER_ON)
CHANNEL_SUBSCRIBER = "subscriber"
# Written into expires_at now; enforced in Phase 2 (ALRT-03, default maximum age 6 h).
MAX_AGE = timedelta(hours=6)


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


# RED-phase interface stubs (replaced in GREEN).


def subscriber_heads() -> list[OutboxMessage]:
    return []


def claim(message_id: int) -> bool:
    return False


def mark_sent(message_id: int, now: datetime) -> bool:
    return False


def mark_uncertain(message_id: int, code: str) -> bool:
    return False


def mark_retry(message_id: int, next_attempt_at: datetime, code: str) -> bool:
    return False


def recover_interrupted() -> int:
    return 0
