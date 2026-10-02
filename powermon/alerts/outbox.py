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
    pending --expire_due (expires_at <= now)--> expired (never sent, ALRT-03)
    pending --mark_dropped (an ops notice that cannot be rendered)--> dropped (never sent)
    pending --drop_deleted_pending (its location was deleted)--> dropped (never sent, D-09)
    pending --make_due (a recorded success or a channel change)--> pending, due now

A deleted location's subscriber alerts are never sent (D-09, INV-19 #2): its rows are not
heads (``subscriber_heads``), and the relay drops its pending rows on every pass, before
expiry (``drop_deleted_pending``). That also catches a row whose send was in flight at
delete time and came back to "pending" after a refusal: it is dropped, not resent, and
never expires into a notice about the deleted location.

``make_due`` is the admin side's one write to a queued row: after a recorded test message
success (D-12) or a chat or token change (D-08) a location's subscriber rows that wait for
a backoff become due at once, so a 15-minute hold earned by a channel that works again
does not delay them.

A subscriber row that becomes uncertain queues one ``ops_uncertain`` notice in the same
transaction (``powermon.alerts.ops``, D-11 #5); an uncertain ops row is only logged.

The worker's claim is fenced by its lease (C1): ``claim(message_id, lease_pid=...)``
moves the row only while the backend ``lease_pid``, the worker's lease session, holds the
worker lock (``powermon.worker.lease.LOCK_KEY``) at the moment of the UPDATE, read from
``pg_locks`` in the same statement. A worker whose lease session was lost (a database
restart, a terminated backend) therefore claims, and so sends, nothing more, even while
its last published lease status still says HELD and another worker already holds the
lock. A send claimed just before the loss is the one in flight: the next holder's
activation turns it into "uncertain". Without ``lease_pid`` (a direct call, as in tests)
the claim is unfenced.

The relay's reset of a row it claimed (``mark_retry`` with ``attempts``) is fenced too
(WR-01): it moves the row only while it still carries the attempt count that claim gave
it, so it never reaches a later claim of the row by another worker, which counts one
more. With ``lease_pid`` it also needs that session to hold the worker lock, as the claim.

An ops notice whose text cannot be built from its integer payload (a referenced row gone,
an end before its start after a backward clock step) never will be: it is dropped at
once, so it never holds the one-line ops queue (B2). Expiry leaves dropped rows alone.

Every row expires ``ALERT_MAX_AGE_HOURS`` after it was recorded (D-07): ``expires_at`` is
set at enqueue from ``settings.CFG.alert_max_age_hours``, read at call time. The relay
runs ``expire_due`` at the start of every pass, before any head is sent, and queues one
``ops_expired`` notice per expired subscriber row in the same transaction; an expired ops
row is only logged (D-08). Only a pending row expires: a "sending" row belongs to the send
in flight (or to activation's recovery).
``last_error`` is always a short code (at most 64 characters), never a URL or a token.
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from django.conf import settings
from django.db import connection
from django.db.models import F

from powermon.alerts.models import OPEN_STATUSES, OutboxMessage
from powermon.worker.lease import LOCK_KEY

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
KIND_OPS_PIN_FAILED = "ops_pin_failed"  # {http_status}; location = the chart's
KIND_OPS_PIN_RESTORED = "ops_pin_restored"  # {}; location = the chart's
# A location's subscriber alerts are refused / delivered again (D-10); location = the alert's.
KIND_OPS_DELIVERY_FAILING = "ops_delivery_failing"  # {http_status[, migrate_to_chat_id]}
KIND_OPS_DELIVERY_RESTORED = "ops_delivery_restored"  # {}
OPS_KINDS = (
    KIND_OPS_GAP,
    KIND_OPS_ALL_SILENT_START,
    KIND_OPS_ALL_SILENT_END,
    KIND_OPS_EXPIRED,
    KIND_OPS_UNCERTAIN,
    KIND_OPS_PIN_FAILED,
    KIND_OPS_PIN_RESTORED,
    KIND_OPS_DELIVERY_FAILING,
    KIND_OPS_DELIVERY_RESTORED,
)
# The database column is varchar(64).
MAX_ERROR_LENGTH = 64
# ``last_error`` of a subscriber row dropped because its location was deleted (D-09).
LOCATION_DELETED = "location_deleted"

RECOVER_SQL = """
UPDATE outbox_message SET status = 'uncertain', last_error = 'interrupted'
 WHERE status = 'sending'
RETURNING id, channel, location_id
"""

EXPIRE_SQL = """
UPDATE outbox_message SET status = 'expired', last_error = 'expired'
 WHERE status = 'pending' AND expires_at <= %(now)s
RETURNING id, channel, location_id
"""

# The worker's claim (C1): only while its lease session holds the worker lock. pg_locks
# shows a session advisory lock on a bigint key as locktype 'advisory' with the key's high
# 32 bits in classid, its low 32 bits in objid and objsubid 1, in the current database.
CLAIM_HELD_SQL = """
UPDATE outbox_message SET status = 'sending', attempts = attempts + 1
 WHERE id = %(id)s AND status = 'pending'
   AND EXISTS (
       SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND pid = %(pid)s
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND classid = %(classid)s::oid AND objid = %(objid)s::oid AND objsubid = 1
   )
"""
# The relay's reset of its own claim (WR-01): only the attempt that claim counted, and only
# while the lease session holds the worker lock (the same pg_locks check as the claim's).
RETRY_HELD_SQL = """
UPDATE outbox_message
   SET status = 'pending', next_attempt_at = %(next_attempt_at)s, last_error = %(code)s
 WHERE id = %(id)s AND status IN ('pending', 'sending') AND attempts = %(attempts)s
   AND EXISTS (
       SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND pid = %(pid)s
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND classid = %(classid)s::oid AND objid = %(objid)s::oid AND objsubid = 1
   )
"""
_LOCK_CLASSID = LOCK_KEY >> 32
_LOCK_OBJID = LOCK_KEY & 0xFFFFFFFF


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
        expires_at=_expires_at(recorded_at),
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
    microseconds, a count, a message id, an HTTP status); names and texts are read at send
    time (OPS-08).
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
        expires_at=_expires_at(recorded_at),
    )


def subscriber_heads() -> list[OutboxMessage]:
    """Each location's oldest open subscriber row, with its location loaded.

    Only the head of a location may be sent, so OFF always goes before ON. A head in
    "sending" was left by an interrupted send; it holds its location's line until worker
    activation turns it into "uncertain". A deleted location has no head: its alerts are
    never sent (D-09, INV-19 #2). PostgreSQL ``DISTINCT ON (location_id)``, served by the
    partial index ``outbox_open_idx``.
    """
    return list(
        OutboxMessage.objects.filter(
            channel=CHANNEL_SUBSCRIBER,
            status__in=OPEN_STATUSES,
            location__deleted_at__isnull=True,
        )
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


def claim(message_id: int, lease_pid: int | None = None) -> bool:
    """Move a pending row to "sending" and count the attempt; False if it was not pending.

    With ``lease_pid`` (the worker's lease session) it is also False unless that session
    holds the worker lock right now (C1), in the same statement.
    """
    if lease_pid is not None:
        with connection.cursor() as cur:
            cur.execute(
                CLAIM_HELD_SQL,
                {
                    "id": message_id,
                    "pid": lease_pid,
                    "classid": _LOCK_CLASSID,
                    "objid": _LOCK_OBJID,
                },
            )
            return cur.rowcount == 1
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


def mark_dropped(message_id: int, code: str) -> bool:
    """Retire a pending row that can never be sent (B2); it is not sent and not expired."""
    updated = OutboxMessage.objects.filter(pk=message_id, status="pending").update(
        status="dropped", last_error=_short(code)
    )
    return updated == 1


def mark_retry(
    message_id: int,
    next_attempt_at: datetime,
    code: str,
    *,
    attempts: int | None = None,
    lease_pid: int | None = None,
) -> bool:
    """Put an open row back to "pending", due again at ``next_attempt_at``.

    With ``attempts`` (the count the relay's claim gave the row) only while the row still
    carries it, so the reset never reaches another worker's later claim (WR-01). With
    ``lease_pid`` also only while that session holds the worker lock, as ``claim``; that
    fenced reset needs ``attempts`` too and moves nothing without it.
    """
    if lease_pid is not None:
        with connection.cursor() as cur:
            cur.execute(
                RETRY_HELD_SQL,
                {
                    "id": message_id,
                    "next_attempt_at": next_attempt_at,
                    "code": _short(code),
                    "attempts": attempts,
                    "pid": lease_pid,
                    "classid": _LOCK_CLASSID,
                    "objid": _LOCK_OBJID,
                },
            )
            return cur.rowcount == 1
    rows = OutboxMessage.objects.filter(pk=message_id, status__in=OPEN_STATUSES)
    if attempts is not None:
        rows = rows.filter(attempts=attempts)
    updated = rows.update(
        status="pending", next_attempt_at=next_attempt_at, last_error=_short(code)
    )
    return updated == 1


def drop_deleted_pending() -> int:
    """Drop every pending subscriber row of a deleted location; return how many (D-09).

    Such a row is never sent (INV-19 #2). The delete itself drops the pending rows, but a
    send in flight at that moment ("sending") is not pending yet and can come back to
    "pending" after a refusal; this sweep, run by the relay on every pass before expiry,
    drops it, so it is neither resent nor expired into a notice about a deleted location.
    Rows in flight or finished, ops notices and live locations' rows are left alone. Runs
    on the caller's connection, inside its transaction.
    """
    return OutboxMessage.objects.filter(
        channel=CHANNEL_SUBSCRIBER,
        status="pending",
        location__deleted_at__isnull=False,
    ).update(status="dropped", last_error=LOCATION_DELETED)


def make_due(location_id: int, now: datetime) -> int:
    """Make the location's waiting subscriber alerts due at ``now``; return how many moved.

    Only pending subscriber rows whose ``next_attempt_at`` is later than ``now`` change:
    a row already due keeps its time, a row in flight or finished is left alone, and ops
    rows and other locations are never touched. Runs on the caller's connection, inside
    its transaction (D-08, D-12). A naive ``now`` raises ValueError before any write.
    """
    if now.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return OutboxMessage.objects.filter(
        channel=CHANNEL_SUBSCRIBER,
        location_id=location_id,
        status="pending",
        next_attempt_at__gt=now,
    ).update(next_attempt_at=now)


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


def expire_due(now: datetime) -> list[RowRef]:
    """Turn pending rows whose ``expires_at`` has come into "expired"; return them by id.

    An expired row is never sent (ALRT-03). One UPDATE ... RETURNING on the caller's
    connection, so the caller can queue one notice per subscriber row in the same
    transaction (D-08). A row expires at exactly ``expires_at``. Rows in "sending" and
    finished rows are left alone.
    """
    with connection.cursor() as cur:
        cur.execute(EXPIRE_SQL, {"now": now})
        return sorted(RowRef(*row) for row in cur.fetchall())


def _expires_at(recorded_at: datetime) -> datetime:
    """``recorded_at`` plus the configured maximum age, read now (D-07)."""
    return recorded_at + timedelta(hours=settings.CFG.alert_max_age_hours)


def _check_payload(payload: dict[str, int], rule: str) -> None:
    for key, value in payload.items():
        # bool is an int subclass, but True is neither a duration nor an id.
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"payload {key!r} {rule}")


def _short(code: str) -> str:
    return code[:MAX_ERROR_LENGTH]
