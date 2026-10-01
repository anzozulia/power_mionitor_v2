"""The worker's Telegram I/O loop body: deliver queued alerts and ops notices (D-11, D-14).

``run_iteration`` is one pass over the outbox. For each location it looks only at the
oldest open row (``outbox.subscriber_heads``), so OFF always goes before ON. A due row is
rendered at send time in the location's current language, claimed with one conditional
UPDATE (committed at once, Django autocommit), and sent with no transaction open.

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
Expiry (ALRT-03) arrives later in 02-07.
"""

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import close_old_connections

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


@dataclass
class RelayState:
    """What the relay remembers between passes: when each bot may be called again."""

    not_before: dict[str, datetime] = field(default_factory=dict)


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
    session on every attempt (D-16). 02-07 flushes unapplied outcomes from ``state``
    after the connection step and before the recovery.
    """
    close_old_connections()
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
    skipped or failed, and after the ops step, so a long pass still shows progress.
    """
    close_old_connections()
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
    if not outbox.claim(row.pk):
        return False
    result = TelegramClient(location.bot_token).send_message(location.chat_id, text)
    # The send may have blocked for seconds: waits and sent_at count from its answer.
    _apply(row, row.attempts + 1, result, clock.now(), state, key)
    return True


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
    if not outbox.claim(row.pk):
        return False
    result = TelegramClient(token).send_message(chat_id, text)
    _apply(row, row.attempts + 1, result, clock.now(), state, key)
    return True


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
    outbox.mark_retry(row.pk, next_attempt_at, result.code or result.kind)
    state.not_before[key] = next_attempt_at
