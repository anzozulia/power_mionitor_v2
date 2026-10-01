"""The worker's Telegram I/O loop body: deliver queued alerts and ops notices (D-11, D-14).

``run_iteration`` is one pass over the outbox, always in this order: the connection step
(``close_old_connections()``), the flush of outcomes kept from an earlier pass (WR-04),
expiry (ALRT-03), the subscriber heads, then at most one ops row. For each location it
looks only at the oldest open row (``outbox.subscriber_heads``), so OFF always goes before
ON. A due row is rendered at send time in the location's current language, claimed with
one conditional UPDATE (committed at once, Django autocommit), and sent with no
transaction open.

After every subscriber head of the pass, and only when an ops chat is configured, the
pass sends at most one ops notice: the oldest open row of the ops queue
(``outbox.ops_head``), rendered at send time (``ops.render_text``) and sent with
``settings.CFG.ops_bot_token`` to ``settings.CFG.ops_chat_id``, never to any other chat
(D-09, INV-20). So a broken or rate-limited admin chat never delays a subscriber alert
(INV-20 #2, ALRT-06), and with no ops chat no request is made for an ops row at all.

The result decides the row's next status, for both channels (D-14 policy):

- ok: sent.
- maybe_delivered (read timeout, connection dropped after sending): uncertain, never
  resent (at-most-once, INV-16, D-13). A subscriber row queues exactly one "may not have
  been delivered" ops notice in the same transaction (``ops.mark_uncertain``, D-11 #5); an
  ops row is only logged, so a broken admin chat cannot loop.
- not_sent (nothing left the client) or transient (5xx): retried after
  min(2 ** attempts, BACKOFF_CAP_S) seconds.
- rate_limited (429): retried after retry_after seconds, capped at MAX_RETRY_AFTER_S.
- permanent (400/401/403/404): retried after PERMANENT_BACKOFF, with one warning.

Every retry also backs off the whole bot in ``RelayState.not_before``; the ops bot has its
own key there. A bot that is backing off is skipped, and the pass moves on to other bots:
nothing here sleeps (INV-14).
The thread's only blocking wait is its idle ``stop.wait`` in ``run_worker``.

Sends are one after another, and each can block for the client's connect plus read
timeouts. So the pass reads the injected ``Clock`` again for each row's due checks, and
once more after each send returns: a retry wait (429 retry_after, backoff) and ``sent_at``
count from when Telegram answered, never from when the pass started (INV-16 #3).

Once ``stop`` is set (SIGTERM), the pass claims no further row. A send already in flight
ends within the client's timeouts and its outcome is written; every other due row stays
pending for the next worker. A row claimed and then cut off by the process exit would be
left "sending", and ``activate`` turns that into "uncertain", never sent (INV-15). The
I/O thread calls ``activate`` once per lease generation, before that generation's first
pass (D-15).

Bots are keyed by a short hash of the token, never the token. Log lines name the location
and a short code only (OPS-08). Loop-body pattern of ``detection.run_cycle``:
``close_old_connections()`` first in every entry point (``activate`` and
``run_iteration``), so a connection the database dropped is replaced before the first
statement (D-16, MON-06), then each location in its own ``try``, with a progress ``tick``
after each one for the watchdog (D-15).

A late alert states when its event happened (ALRT-04, D-05 to D-07). Right before a
subscriber row is claimed, ``_deliver`` reads the clock and, when the row goes out more
than ``LATE_AFTER`` (120 s) after its ``recorded_at``, renders the event's local time
(``times.event_prefix`` in ``settings.CFG.display_tz``: ``HH:MM``, or ``DD.MM HH:MM`` on
another local date) before the bold status. Lateness counts from ``recorded_at``, never
from the backdated outage start (INV-15). Ops notices carry their own times and never get
this prefix.

Expiry (ALRT-03, D-07, D-08) is the first work step of every pass, before any head is
sent: in one transaction every pending row whose ``expires_at`` has come becomes
"expired" (``outbox.expire_due``) and is never sent, and each expired subscriber alert
queues one ``ops_expired`` notice; an expired ops row is only logged, so a broken admin
chat cannot loop. The location's next alert (the ON after an expired OFF) is then its head
and goes out in the same pass. A database error in this step ends the pass before any
claim and reaches the caller (``run_worker`` logs it once per outage, by class name).

WR-04 (D-13): an error after a row was claimed never blocks its queue (a location's line,
or the ops queue) for the rest of a lease generation, and never turns a known send into an
uncertain one. Both channels go through one helper, ``_send``:

- the claim raises a database error: no request was made, so the row may go back to
  "pending"; that reset is kept in ``RelayState.unapplied``;
- an exception after the claim and before the HTTP call (building the client): the
  request provably never left, so the row goes back to "pending" ("pre_send_error");
- the outcome write raises a database error after Telegram answered: the known outcome
  (``Unapplied``: the row, attempts, result, answer time and bot key) is kept.

Kept outcomes are written by ``_flush_unapplied`` at the start of the next pass, right
after the connection step and before any new claim, and at activation before leftover
"sending" rows are declared uncertain; so a known "ok" becomes "sent", never "uncertain".
A database error stops the flush and propagates: the pass (or the activation) ends and is
retried on a replaced connection. Only database errors are kept: any other error is a bug,
logged by class name, and the row waits for activation as before. The flush runs before
any head, so a head with a kept outcome is never seen by the same process.
"""

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import Error, close_old_connections, transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.alerts.texts import render_alert
from powermon.clock import Clock
from powermon.i18n import times
from powermon.telegram.client import DEFAULT_RETRY_AFTER_S, SendResult, TelegramClient

log = logging.getLogger(__name__)

BACKOFF_CAP_S = 30
PERMANENT_BACKOFF = timedelta(minutes=15)
# The client passes Telegram's retry_after through uncapped. A larger value would only be
# a bug or a hostile answer (and could overflow the datetime arithmetic): after an hour
# the next try simply gets a fresh 429 with the remaining wait.
MAX_RETRY_AFTER_S = 3600
# An alert sent later than this after it was recorded states its event time (D-07).
LATE_AFTER = timedelta(seconds=120)
# Which payload value holds the previous state's duration for each alert kind.
_DURATION_KEYS = {outbox.KIND_POWER_OFF: "was_on_us", outbox.KIND_POWER_ON: "was_off_us"}


@dataclass(frozen=True)
class Unapplied:
    """A claimed row's outcome that this process knows but could not write yet (WR-04).

    ``result`` None means no request was made (the claim's own outcome is unknown, or an
    error came before the HTTP call): the row goes back to "pending". Otherwise it is
    Telegram's answer at ``answered_at``, applied as if it had been written then.
    """

    row: OutboxMessage
    attempts: int
    result: SendResult | None
    answered_at: datetime
    key: str


@dataclass
class RelayState:
    """What the relay remembers between passes.

    ``not_before``: when each bot may be called again. ``unapplied``: outcomes kept after
    a database error, by row id, written before the next claim (WR-04).
    """

    not_before: dict[str, datetime] = field(default_factory=dict)
    unapplied: dict[int, Unapplied] = field(default_factory=dict)


def bot_key(token: str) -> str:
    """A short, stable name for a bot: the first 12 hex digits of SHA-256 of its token."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def activate(state: RelayState, clock: Clock) -> int:
    """Start a lease generation's relay term; return the sends marked uncertain.

    The I/O thread calls this once per new lease generation, before that generation's
    first pass, and calls it again while it raises. Rows the previous holder left in
    "sending" become uncertain with one notice each, never resent (INV-16, D-13).

    It starts with ``close_old_connections()``: Django health-checks or drops a dead
    connection only after that call, so a retry without it would hit the same dead
    session on every attempt (D-16). Then this process's own kept outcomes are written
    (WR-04), so a send it knows went through becomes "sent" and is not declared uncertain
    by the recovery. A database error there propagates, and the recovery waits for the
    next attempt.
    """
    close_old_connections()
    _flush_unapplied(state)
    recovered = ops.recover_interrupted(clock.now())
    log.info("relay activated: %d interrupted send(s) marked uncertain", recovered)
    return recovered


def run_iteration(
    clock: Clock,
    state: RelayState,
    stop: threading.Event | None = None,
    tick: Callable[[], None] | None = None,
) -> bool:
    """One pass over each location's oldest open alert; True if any send was attempted.

    Returns early, before claiming another row, once ``stop`` is set. ``tick`` (the
    watchdog's progress stamp) runs after every subscriber head, whether it was sent,
    skipped or failed, and after the ops step, so a long pass still shows progress. A
    database error in the flush or in expiry ends the pass before any claim and
    propagates to the caller.
    """
    close_old_connections()
    # Outcomes kept after a DB error are written before any new claim (WR-04).
    _flush_unapplied(state)
    # Nothing past its maximum age may go out, so expiry runs before any head (ALRT-03).
    _expire(clock.now())
    attempted = False
    for row in outbox.subscriber_heads():
        if stop is not None and stop.is_set():
            break
        # Read per row: earlier sends in this pass may have taken seconds each.
        if row.status == "pending" and row.next_attempt_at <= clock.now():
            try:
                attempted = _deliver(row, clock, state) or attempted
            except Exception as exc:
                # The type only: an exception's text can carry connection details or a URL.
                log.error("relay failed for location %s: %s", row.location_id, type(exc).__name__)
        if tick is not None:
            tick()
    # Ops notices go after every subscriber head (INV-20 #2), and only to a configured chat.
    if not (stop is not None and stop.is_set()):
        try:
            attempted = _deliver_ops(clock, state) or attempted
        except Exception as exc:
            log.error("relay failed for the ops chat: %s", type(exc).__name__)
        if tick is not None:
            tick()
    return attempted


def _expire(now: datetime) -> int:
    """Expire every pending row past its ``expires_at``; notify once per subscriber alert.

    One transaction: the rows become "expired" and each subscriber row queues (or, with no
    ops chat, logs) one ``ops_expired`` notice with them (D-08). An expired ops row is only
    logged: a notice about a notice would loop on a broken admin chat. Returns how many
    rows expired.
    """
    with transaction.atomic():
        rows = outbox.expire_due(now)
        for ref in rows:
            if ref.channel == outbox.CHANNEL_SUBSCRIBER:
                ops.notify(
                    outbox.KIND_OPS_EXPIRED,
                    payload={"message_id": ref.id},
                    recorded_at=now,
                    location_id=ref.location_id,
                )
            else:
                log.warning("ops notice %s expired undelivered; it is not resent", ref.id)
    return len(rows)


def _deliver(row: OutboxMessage, clock: Clock, state: RelayState) -> bool:
    """Send one due head row unless its bot is backing off; True if a send was attempted."""
    location = row.location
    if location is None:
        raise ValueError("a subscriber alert without a location")
    key = bot_key(location.bot_token)
    now = clock.now()
    if state.not_before.get(key, now) > now:
        return False
    try:
        # Rendered now, right before the claim: lateness is measured at send time (D-07).
        text = _render(row, location.language, _late_prefix(row, now))
    except KeyError, TypeError, ValueError:
        # A broken row backs off alone: the bot is fine, so other locations keep sending.
        outbox.mark_retry(row.pk, now + PERMANENT_BACKOFF, "render_error")
        log.warning("relay: cannot render alert %s for location %s", row.pk, row.location_id)
        return False
    return _send(row, text, location.bot_token, location.chat_id, key, clock, state)


def _deliver_ops(clock: Clock, state: RelayState) -> bool:
    """Send the oldest open ops notice if due and the ops bot is not backing off.

    At most one ops row per pass; True if a send was attempted.
    """
    token = settings.CFG.ops_bot_token
    chat_id = settings.CFG.ops_chat_id
    if not token or chat_id is None:
        # No ops chat: notices go to the log (D-09); a row queued earlier just waits.
        return False
    row = outbox.ops_head()
    if row is None:
        return False
    now = clock.now()
    if row.status != "pending" or row.next_attempt_at > now:
        return False
    key = bot_key(token)
    if state.not_before.get(key, now) > now:
        return False
    try:
        text = ops.render_text(row.kind, row.payload, row.location_id, now=now)
    except LookupError, TypeError, ValueError:
        # A broken notice backs off alone: the ops bot is fine.
        outbox.mark_retry(row.pk, now + PERMANENT_BACKOFF, "render_error")
        log.warning("relay: cannot render ops notice %s", row.pk)
        return False
    return _send(row, text, token, chat_id, key, clock, state)


def _send(
    row: OutboxMessage,
    text: str,
    token: str,
    chat_id: int,
    key: str,
    clock: Clock,
    state: RelayState,
) -> bool:
    """Claim, send and record one row of either channel; True if the request was made.

    From the claim on, no error leaves the row "sending" for good (WR-04, D-13): a
    database error keeps what is known in ``state.unapplied``, and an error before the
    HTTP call puts the row back to "pending".
    """
    attempts = row.attempts + 1
    try:
        claimed = outbox.claim(row.pk)
    except Error as exc:
        # The claim may or may not have committed; either way no request was made.
        state.unapplied[row.pk] = Unapplied(row, attempts, None, clock.now(), key)
        log.warning("relay: could not claim alert %s (%s); retrying", row.pk, type(exc).__name__)
        return False
    if not claimed:
        return False
    try:
        client = TelegramClient(token)
    except Exception as exc:
        # Raised before the HTTP call: the request provably never left.
        log.warning(
            "relay: alert %s failed before the send (%s); it is pending again",
            row.pk,
            type(exc).__name__,
        )
        _write(Unapplied(row, attempts, None, clock.now(), key), state)
        return False
    result = client.send_message(chat_id, text)
    # The send may have blocked for seconds: waits and sent_at count from its answer.
    _write(Unapplied(row, attempts, result, clock.now(), key), state)
    return True


def _write(outcome: Unapplied, state: RelayState) -> None:
    """Write a known outcome now, or keep it for the next pass if the database fails."""
    try:
        _record(outcome, state)
    except Error as exc:
        state.unapplied[outcome.row.pk] = outcome
        log.warning(
            "relay: could not record the outcome of alert %s (%s); retrying",
            outcome.row.pk,
            type(exc).__name__,
        )


def _flush_unapplied(state: RelayState) -> None:
    """Write every kept outcome, oldest first; a database error stops and propagates.

    Each entry is removed once written, so a flush cut short by the error resumes where it
    stopped on the next call.
    """
    for outcome in list(state.unapplied.values()):
        _record(outcome, state)
        del state.unapplied[outcome.row.pk]


def _record(outcome: Unapplied, state: RelayState) -> None:
    """Write one outcome on its claimed row: back to pending if nothing was sent."""
    if outcome.result is None:
        outbox.mark_retry(outcome.row.pk, outcome.answered_at, "pre_send_error")
        return
    _apply(outcome.row, outcome.attempts, outcome.result, outcome.answered_at, state, outcome.key)


def _late_prefix(row: OutboxMessage, now: datetime) -> str | None:
    """The event's local time if the alert goes out more than LATE_AFTER after it was recorded.

    The event time is the outage start for OFF and the restore time for ON (``event_at``);
    lateness counts from ``recorded_at`` (D-05 to D-07, INV-15).
    """
    if now - row.recorded_at <= LATE_AFTER:
        return None
    return times.event_prefix(row.event_at, now, settings.CFG.display_tz)


def _render(row: OutboxMessage, language: str, prefix: str | None) -> str:
    payload = row.payload
    if not isinstance(payload, dict):
        raise TypeError("the alert payload is not an object")
    return render_alert(row.kind, language, payload[_DURATION_KEYS[row.kind]], prefix)


def _apply(
    row: OutboxMessage,
    attempts: int,
    result: SendResult,
    now: datetime,
    state: RelayState,
    key: str,
) -> None:
    """Record a send's outcome on the claimed row (and on the bot, for a retry)."""
    if result.kind == "ok":
        outbox.mark_sent(row.pk, now)
        return
    if result.kind == "maybe_delivered":
        # Never resent; a subscriber row queues one notice, an ops row is only logged.
        ops.mark_uncertain(row.pk, result.code or "maybe_delivered", now)
        return
    if result.kind == "rate_limited":
        wait = min(result.retry_after or DEFAULT_RETRY_AFTER_S, MAX_RETRY_AFTER_S)
        delay = timedelta(seconds=wait)
    elif result.kind == "permanent":
        delay = PERMANENT_BACKOFF
        if row.channel == outbox.CHANNEL_OPS:
            log.warning(
                "relay: permanent error %s for the ops chat; the ops bot backs off for 15 min",
                result.code,
            )
        else:
            log.warning(
                "relay: permanent error %s for location %s; its bot backs off for 15 min",
                result.code,
                row.location_id,
            )
    else:  # not_sent or transient: nothing was delivered, retry with a capped backoff
        delay = timedelta(seconds=min(2**attempts, BACKOFF_CAP_S))
    next_attempt_at = now + delay
    # In memory first: if the write below fails, the bot still waits (WR-04).
    state.not_before[key] = next_attempt_at
    outbox.mark_retry(row.pk, next_attempt_at, result.code or result.kind)
