"""Where an ops notice goes: the env-configured admin chat, or the log (D-09, D-10, D-11).

This is the one module that decides the destination of a notice about the server's own
state. ``notify`` queues the notice on the outbox's "ops" channel when ``settings.CFG``
has an ops chat (``OPS_BOT_TOKEN`` + ``OPS_CHAT_ID``); the worker relay sends it with that
token to that chat only, after every subscriber alert of the pass (INV-20). With no ops
chat configured it writes no row and logs the rendered plain text once at WARNING. No code
path holds a chat ID or token of its own.

A notice's payload holds integers only: epoch microseconds (``instant_us``), a count or
the id of the subscriber row it is about. ``render_text`` reads names and rows and builds
the English text (``ops_texts``) at send time, so no text and no secret is stored
(OPS-08).

``mark_uncertain`` is the at-most-once rule with its notice (D-13, D-11 #5, INV-16): a
claimed subscriber row whose send may have reached Telegram becomes "uncertain", is never
resent, and queues exactly one "may not have been delivered" notice in the same
transaction. An ops row in the same situation is only logged, so a broken admin chat
cannot loop (the D-08 rule).

Every function runs inside the caller's transaction or opens its own. Nothing here does
network I/O.
"""

import logging
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import transaction

from powermon.alerts import ops_texts, outbox
from powermon.alerts.models import OutboxMessage
from powermon.locations.models import Location

log = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_US = timedelta(microseconds=1)


def instant_us(dt: datetime) -> int:
    """An aware instant as integer microseconds since the Unix epoch (a payload value)."""
    if dt.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return (dt - EPOCH) // _ONE_US


def from_instant_us(us: int) -> datetime:
    """The aware UTC instant ``us`` microseconds after the Unix epoch."""
    if not isinstance(us, int) or isinstance(us, bool):
        raise TypeError(f"an instant must be integer microseconds, not {type(us).__name__}")
    try:
        return EPOCH + timedelta(microseconds=us)
    except OverflowError:
        raise ValueError(f"instant out of range: {us} us") from None


def notify(
    kind: str,
    *,
    payload: dict[str, int],
    recorded_at: datetime,
    location_id: int | None = None,
) -> None:
    """Send an ops notice to the admin chat through the outbox, or log it (D-09).

    Call it inside the caller's ``transaction.atomic()``: the notice commits with the
    change it reports, or not at all. The kind and payload are checked in both modes. With
    no ops chat configured the plain-text notice is logged at WARNING; a notice that cannot
    be rendered is logged by its error class and never raised, because a notice must not
    abort the caller's transition.
    """
    if settings.CFG.ops_configured:
        outbox.enqueue_ops(kind, payload=payload, recorded_at=recorded_at, location_id=location_id)
        return
    outbox.check_ops_notice(kind, payload)
    try:
        # A savepoint, so a failed read cannot break the caller's transaction.
        with transaction.atomic():
            text = render_text(kind, payload, location_id, now=recorded_at, escape=False)
    except Exception as exc:
        log.warning(
            "ops notice %s (ops chat not configured) could not be rendered: %s",
            kind,
            type(exc).__name__,
        )
        return
    log.warning("ops notice (ops chat not configured): %s", text)


def render_text(
    kind: str,
    payload: object,
    location_id: int | None,
    *,
    now: datetime,
    escape: bool = True,
) -> str:
    """The English text of an ops notice, read and rendered now (D-10, D-11).

    ``escape`` HTML-escapes location names (Telegram HTML); the log gets the plain text.
    A missing referenced row raises LookupError, a missing payload key KeyError, a
    non-integer value TypeError, and an unknown kind ValueError.
    """
    tz = settings.CFG.display_tz
    if kind == outbox.KIND_OPS_GAP:
        return ops_texts.gap(_instant(payload, "start_us"), _instant(payload, "end_us"), tz)
    if kind == outbox.KIND_OPS_ALL_SILENT_START:
        return ops_texts.all_silent_start(
            _instant(payload, "since_us"), _int(payload, "count"), now, tz
        )
    if kind == outbox.KIND_OPS_ALL_SILENT_END:
        return ops_texts.all_silent_end(
            _instant(payload, "since_us"),
            _instant(payload, "first_us"),
            _location_name(location_id),
            tz,
            escape=escape,
        )
    if kind == outbox.KIND_OPS_EXPIRED:
        alert = _alert(_int(payload, "message_id"))
        return ops_texts.expired(
            alert.kind,
            alert.event_at,
            # The row's own maximum age, whatever the setting is now.
            alert.expires_at - alert.recorded_at,
            _location_name(alert.location_id),
            now,
            tz,
            escape=escape,
        )
    if kind == outbox.KIND_OPS_UNCERTAIN:
        alert = _alert(_int(payload, "message_id"))
        return ops_texts.uncertain(
            alert.kind,
            alert.event_at,
            _location_name(alert.location_id),
            interrupted=alert.last_error == "interrupted",
            now=now,
            tz=tz,
            escape=escape,
        )
    raise ValueError(f"unknown ops notice kind: {kind!r}")


def mark_uncertain(message_id: int, code: str, now: datetime) -> bool:
    """A claimed row may have reached Telegram: never resend it; notify once (D-13).

    One transaction: the row goes from "sending" to "uncertain", and for a subscriber row
    one ``ops_uncertain`` notice is queued (or logged) with it. An ops row is only logged.
    Returns whether the row changed.
    """
    with transaction.atomic():
        row = (
            OutboxMessage.objects.select_for_update()
            .filter(pk=message_id, status="sending")
            .values("channel", "location_id")
            .first()
        )
        if row is None or not outbox.mark_uncertain(message_id, code):
            return False
        if row["channel"] == outbox.CHANNEL_SUBSCRIBER:
            notify(
                outbox.KIND_OPS_UNCERTAIN,
                payload={"message_id": message_id},
                recorded_at=now,
                location_id=row["location_id"],
            )
        else:
            log.warning("ops notice %s may not have been delivered; it is not resent", message_id)
        return True


def _int(payload: object, key: str) -> int:
    if not isinstance(payload, dict):
        raise TypeError("an ops payload is not an object")
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"ops payload {key!r} is not an integer")
    return value


def _instant(payload: object, key: str) -> datetime:
    return from_instant_us(_int(payload, key))


def _alert(message_id: int) -> OutboxMessage:
    """The subscriber row a notice is about."""
    alert = OutboxMessage.objects.filter(pk=message_id, channel=outbox.CHANNEL_SUBSCRIBER).first()
    if alert is None:
        raise LookupError(f"no subscriber alert {message_id}")
    return alert


def _location_name(location_id: int | None) -> str:
    if location_id is None:
        raise LookupError("the notice has no location")
    name = Location.objects.filter(pk=location_id).values_list("name", flat=True).first()
    if name is None:
        raise LookupError(f"no location {location_id}")
    return name
