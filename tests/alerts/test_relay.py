"""The outbox relay: queued alerts reach Telegram one at a time (ALRT-01, ALRT-02, D-14).

Per location the oldest open row goes first, so OFF always precedes ON. The text is
rendered at send time in the location's current language. The D-14 delivery policy:
- ok: sent, once;
- maybe delivered (read timeout, dropped connection): uncertain, never resent (INV-16);
- not sent (connect failure) or a 5xx: retried after min(2 ** attempts, 30) s;
- 429: retried no earlier than retry_after;
- any other answer (400/401/403/404): that bot backs off for 15 min.
A bot that is backing off is skipped, and other bots' alerts go out in the same pass: no
thread ever sleeps out one bot's wait (INV-14).

WR-04 (D-13): an error after a row was claimed never blocks its queue. A known outcome
that could not be written is kept in ``RelayState.unapplied`` and written at the start of
the next pass (and at activation), so a known "ok" becomes "sent", never "uncertain"; an
error before the HTTP call puts the row back to "pending". The same holds for ops rows.
INV-14 #1 and INV-15 #1/#2 drive detection and the relay together on the injected clock.

``run_iteration`` calls ``close_old_connections()``, so every test that runs it is
``django_db(transaction=True)``. Time comes only from the ``FakeClock`` passed in; a fake
send can move it forward while the request is in flight (``fake_telegram.answer``).
Telegram is faked at the HTTP boundary (``fake_telegram``).
"""

import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
import responses
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    OPS_CHAT_ID,
    Actor,
    FakeClock,
)
from django.db import OperationalError, connection, transaction
from requests import PreparedRequest
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.engine import transitions
from powermon.engine.models import SystemState
from powermon.telegram.client import SendResult
from powermon.worker import detection, io_loop

TOKEN_A = DEFAULT_BOT_TOKEN
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
# The OFF transition of K-2 was recorded at 10:06:31.
T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
ON_EN = "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>"
OFF_UK = "🔴 <b>СВІТЛО ЗНИКЛО</b>\n⚡ Світло було: <b>5 хв</b>"
DEFAULT_PAYLOADS = {
    "power_off": {"was_on_us": 300_000_000},
    "power_on": {"was_off_us": 3_300_000_000},
}
RELAY_LOGGER = "powermon.worker.io_loop"


def _seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


def _queue(
    location: Any,
    kind: str = "power_off",
    *,
    at: datetime = T0,
    payload: dict[str, int] | None = None,
) -> OutboxMessage:
    """Queue one alert recorded (and so due) at ``at``, as a transition would."""
    with transaction.atomic():
        return outbox.enqueue(
            kind,
            location.pk,
            event_at=at - _seconds(91),
            recorded_at=at,
            payload=DEFAULT_PAYLOADS[kind] if payload is None else payload,
        )


def _row(message: OutboxMessage) -> OutboxMessage:
    return OutboxMessage.objects.get(pk=message.pk)


def _body(text: str, chat_id: int = DEFAULT_CHAT_ID) -> dict[str, Any]:
    return {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}


def _calls_to(fake: Any, token: str) -> int:
    return len([call for call in fake.calls if f"/bot{token}/" in call.request.url])


def _refused(token: str) -> requests.ConnectionError:
    # A real connect-phase exception carries the URL, and so the token, in its text (P-12).
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


# Order and rendering (ALRT-01, ALRT-02)


@pytest.mark.django_db(transaction=True)
def test_off_before_on_head_of_line(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    off = _queue(location, "power_off")
    on = _queue(location, "power_on", at=T0 + _seconds(30))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    now = T0 + _seconds(60)

    # Both rows are due, but only the location's oldest row goes out in a pass.
    assert io_loop.run_iteration(FakeClock(now), state) is True

    assert fake_telegram.sent == [_body(OFF_EN)]
    sent = _row(off)
    assert (sent.status, sent.sent_at, sent.attempts, sent.last_error) == ("sent", now, 1, "")
    assert _row(on).status == "pending"

    assert io_loop.run_iteration(FakeClock(now + _seconds(1)), state) is True

    assert fake_telegram.sent == [_body(OFF_EN), _body(ON_EN)]
    assert (_row(on).status, _row(on).sent_at) == ("sent", now + _seconds(1))
    assert io_loop.run_iteration(FakeClock(now + _seconds(2)), state) is False
    assert len(fake_telegram.calls) == 2


@pytest.mark.django_db(transaction=True)
def test_text_rendered_at_send_time_in_current_language(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory(language="en")
    _queue(location)
    # The admin switches the language after the alert was queued: the row holds no text.
    type(location).objects.filter(pk=location.pk).update(language="uk")
    fake_telegram.accept(TOKEN_A)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is True

    assert fake_telegram.sent == [_body(OFF_UK)]


# Ambiguous sends are never resent (INV-16)


@pytest.mark.django_db(transaction=True)
def test_read_timeout_marks_uncertain_and_never_resends(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    off = _queue(location, "power_off")
    on = _queue(location, "power_on", at=T0 + _seconds(30))
    # The first call (the OFF) reaches Telegram, then the answer times out; later calls succeed.
    fake_telegram.fail(TOKEN_A, exc=requests.ReadTimeout("read timed out"))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    now = T0 + _seconds(60)

    assert io_loop.run_iteration(FakeClock(now), state) is True

    row = _row(off)
    assert (row.status, row.last_error, row.attempts, row.sent_at) == (
        "uncertain",
        "read_timeout",
        1,
        None,
    )
    # The uncertain OFF no longer holds the line: the ON is the location's head now.
    assert io_loop.run_iteration(FakeClock(now + _seconds(1)), state) is True
    assert fake_telegram.sent == [_body(ON_EN)]
    assert _row(on).status == "sent"
    for minutes in (1, 10, 60):
        assert io_loop.run_iteration(FakeClock(now + timedelta(minutes=minutes)), state) is False
    assert len(fake_telegram.calls) == 2
    assert _row(off).status == "uncertain"


@pytest.mark.django_db(transaction=True)
def test_a_sending_head_blocks_its_location_until_recovered(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # A row left in "sending" by a crash may have reached Telegram. It holds its location's
    # line (it is never resent) until activation turns it into "uncertain".
    location = location_factory()
    interrupted = _queue(location, "power_off")
    on = _queue(location, "power_on", at=T0 + _seconds(30))
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    now = T0 + _seconds(60)

    assert io_loop.run_iteration(FakeClock(now), state) is False
    assert len(fake_telegram.calls) == 0

    assert outbox.recover_interrupted() == [
        outbox.RowRef(interrupted.pk, "subscriber", location.pk)
    ]
    assert io_loop.run_iteration(FakeClock(now), state) is True
    assert fake_telegram.sent == [_body(ON_EN)]
    assert _row(on).status == "sent"
    assert _row(interrupted).status == "uncertain"


# Retries: capped backoff, 429, permanent errors (D-14, INV-14, INV-16)


@pytest.mark.django_db(transaction=True)
def test_connect_timeout_retries_with_capped_backoff(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    fake_telegram.fail(TOKEN_A, exc=requests.ConnectTimeout("connect timed out"))
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "connect_timeout")
    assert row.next_attempt_at == T0 + _seconds(2)
    # Not due yet: nothing is sent, and nothing sleeps.
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is False
    assert len(fake_telegram.calls) == 1
    due = row.next_attempt_at
    # 2 ** attempts: 4, 8, 16, then 30 instead of 32.
    for attempts, delay in [(2, 4), (3, 8), (4, 16), (5, 30)]:
        assert io_loop.run_iteration(FakeClock(due), state) is True
        row = _row(off)
        assert (row.status, row.attempts) == ("pending", attempts)
        assert row.next_attempt_at == due + _seconds(delay)
        due = row.next_attempt_at
    assert len(fake_telegram.calls) == 5
    assert io_loop.BACKOFF_CAP_S == 30


@pytest.mark.django_db(transaction=True)
def test_server_error_is_retried(location_factory: Callable[..., Any], fake_telegram: Any) -> None:
    location = location_factory()
    off = _queue(location)
    fake_telegram.fail(TOKEN_A, status=502)
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "http_502")
    assert row.next_attempt_at == T0 + _seconds(2)
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(2)), state) is True
    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("sent", 2, "")
    assert fake_telegram.sent == [_body(OFF_EN)]


@pytest.mark.django_db(transaction=True)
def test_429_waits_retry_after_while_other_bots_send(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # A is created first, so the pass reaches A's 429 before B's alert.
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    row_a = _queue(a)
    row_b = _queue(b)
    flood = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 30",
        "parameters": {"retry_after": 30},
    }
    fake_telegram.fail(TOKEN_A, status=429, json_body=flood)
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(TOKEN_B)
    state = io_loop.RelayState()

    started = time.monotonic()
    assert io_loop.run_iteration(FakeClock(T0), state) is True
    assert time.monotonic() - started < 1.0

    waiting = _row(row_a)
    assert (waiting.status, waiting.last_error) == ("pending", "429")
    assert waiting.next_attempt_at >= T0 + _seconds(30)
    assert state.not_before[io_loop.bot_key(TOKEN_A)] >= T0 + _seconds(30)
    assert _row(row_b).status == "sent"
    assert fake_telegram.sent == [_body(OFF_EN, CHAT_B)]
    for offset in (1, 15, 29):
        assert io_loop.run_iteration(FakeClock(T0 + _seconds(offset)), state) is False
    assert _calls_to(fake_telegram, TOKEN_A) == 1

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(30)), state) is True
    assert _row(row_a).status == "sent"
    assert _calls_to(fake_telegram, TOKEN_A) == 2


@pytest.mark.django_db(transaction=True)
def test_huge_retry_after_is_capped(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # The client passes retry_after through uncapped; an absurd value must not overflow the
    # datetime arithmetic and strand the claimed row in "sending".
    location = location_factory()
    off = _queue(location)
    flood = {"ok": False, "error_code": 429, "parameters": {"retry_after": 10**12}}
    fake_telegram.fail(TOKEN_A, status=429, json_body=flood)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(off)
    assert (row.status, row.last_error) == ("pending", "429")
    assert row.next_attempt_at == T0 + _seconds(io_loop.MAX_RETRY_AFTER_S)
    assert io_loop.MAX_RETRY_AFTER_S == 3600


# Retry and sent times come from the clock after the send returns (D-14, INV-16)
#
# A pass sends one row after another, and each send can block for up to 5 s connect plus
# 10 s read. So every wait is counted from when Telegram answered, not from when the pass
# started, and each row's due check reads the clock again. The fake sends below move the
# clock forward while the request is in flight.


def _too_many_requests(retry_after: int) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": 429,
        "description": f"Too Many Requests: retry after {retry_after}",
        "parameters": {"retry_after": retry_after},
    }


@pytest.mark.django_db(transaction=True)
def test_sent_at_is_the_time_telegram_answered(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    off = _queue(location_factory())
    clock = FakeClock(T0)
    fake_telegram.answer(TOKEN_A, lambda: clock.advance(seconds=3))

    assert io_loop.run_iteration(clock, io_loop.RelayState()) is True

    assert fake_telegram.sent == [_body(OFF_EN)]
    assert (_row(off).status, _row(off).sent_at) == ("sent", T0 + _seconds(3))


@pytest.mark.django_db(transaction=True)
def test_INV16_429_after_a_slow_send_waits_retry_after_from_the_429(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # INV-16 acceptance #3: after a 429 with retry_after=30, the next attempt for that bot
    # comes at least 30 s later. A's send takes 10 s, so B's 429 arrives at T0 + 10 s.
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    _queue(a)
    row_b = _queue(b)
    clock = FakeClock(T0)
    fake_telegram.answer(TOKEN_A, lambda: clock.advance(seconds=10))
    fake_telegram.fail(TOKEN_B, status=429, json_body=_too_many_requests(30))
    fake_telegram.accept(TOKEN_B)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True

    limited_at = T0 + _seconds(10)
    waiting = _row(row_b)
    assert (waiting.status, waiting.attempts, waiting.last_error) == ("pending", 1, "429")
    assert waiting.next_attempt_at == limited_at + _seconds(30)
    assert state.not_before[io_loop.bot_key(TOKEN_B)] == limited_at + _seconds(30)
    # The busy pass is followed by the next one at once: B's bot is still not called.
    for offset in (0, 20, 29):
        clock.set(limited_at + _seconds(offset))
        assert io_loop.run_iteration(clock, state) is False
    assert _calls_to(fake_telegram, TOKEN_B) == 1

    clock.set(limited_at + _seconds(30))
    assert io_loop.run_iteration(clock, state) is True
    assert _row(row_b).status == "sent"
    assert _calls_to(fake_telegram, TOKEN_B) == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ({"exc": requests.ConnectTimeout("connect timed out")}, "connect_timeout"),
        ({"status": 502}, "http_502"),
    ],
    ids=["not_sent", "5xx"],
)
def test_INV16_retry_backoff_counts_from_the_failure(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    failure: dict[str, Any],
    code: str,
) -> None:
    # The failed send takes 5 s (the client's connect timeout). The 2 s backoff starts when
    # it fails; it must not be used up while the send was still running.
    off = _queue(location_factory())
    clock = FakeClock(T0)
    fake_telegram.answer(TOKEN_A, lambda: clock.advance(seconds=5), **failure)
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True

    failed_at = T0 + _seconds(5)
    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, code)
    assert row.next_attempt_at == failed_at + _seconds(2)
    assert state.not_before[io_loop.bot_key(TOKEN_A)] == failed_at + _seconds(2)
    clock.set(failed_at + _seconds(1))
    assert io_loop.run_iteration(clock, state) is False
    assert len(fake_telegram.calls) == 1

    clock.set(failed_at + _seconds(2))
    assert io_loop.run_iteration(clock, state) is True
    assert (_row(off).status, _row(off).sent_at) == ("sent", failed_at + _seconds(2))


@pytest.mark.django_db(transaction=True)
def test_a_row_that_falls_due_during_a_slow_pass_goes_out_in_that_pass(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Each row's due check reads the clock again: B is due at T0 + 5 s, and A's 10 s send
    # ends after that, so B is sent in the same pass.
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    _queue(a)
    row_b = _queue(b, at=T0 + _seconds(5))
    clock = FakeClock(T0)
    fake_telegram.answer(TOKEN_A, lambda: clock.advance(seconds=10))
    fake_telegram.accept(TOKEN_B)

    assert io_loop.run_iteration(clock, io_loop.RelayState()) is True

    assert fake_telegram.sent == [_body(OFF_EN), _body(OFF_EN, CHAT_B)]
    assert (_row(row_b).status, _row(row_b).sent_at) == ("sent", T0 + _seconds(10))


# Shutdown: no new send after a stop request (INV-15)


@pytest.mark.django_db(transaction=True)
def test_INV15_a_stop_mid_pass_claims_no_further_row(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # SIGTERM arrives while A's alert is in flight. A's send finishes and its outcome is
    # written, but B's alert is not claimed: it stays pending for the next worker. Claimed
    # and cut off by the exit, it would be left "sending", and activation would turn it
    # into an "uncertain" row that is never sent.
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    row_a = _queue(a)
    row_b = _queue(b)
    stop = threading.Event()
    fake_telegram.answer(TOKEN_A, stop.set)
    fake_telegram.accept(TOKEN_B)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState(), stop) is True

    assert fake_telegram.sent == [_body(OFF_EN)]
    assert _row(row_a).status == "sent"
    assert (_row(row_b).status, _row(row_b).attempts) == ("pending", 0)
    assert _calls_to(fake_telegram, TOKEN_B) == 0
    assert not OutboxMessage.objects.filter(status__in=("sending", "uncertain")).exists()


@pytest.mark.django_db(transaction=True)
def test_a_pass_after_a_stop_request_claims_nothing(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    row = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)
    stop = threading.Event()
    stop.set()

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState(), stop) is False

    assert len(fake_telegram.calls) == 0
    assert (_row(row).status, _row(row).attempts) == ("pending", 0)


@pytest.mark.django_db(transaction=True)
def test_permanent_error_backs_off_15_minutes(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    same_bot = location_factory()
    off = _queue(location)
    other = _queue(same_bot)
    kicked = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}
    fake_telegram.fail(TOKEN_A, status=403, json_body=kicked)
    state = io_loop.RelayState()
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "http_403")
    assert row.next_attempt_at == T0 + timedelta(minutes=15)
    assert io_loop.PERMANENT_BACKOFF == timedelta(minutes=15)
    assert state.not_before[io_loop.bot_key(TOKEN_A)] == T0 + timedelta(minutes=15)
    relay_lines = [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER]
    assert len(relay_lines) == 1
    assert "http_403" in relay_lines[0]
    assert str(location.pk) in relay_lines[0]
    # The bot backs off as a whole: its other location waits too, and nothing is retried
    # before the 15 minutes are up.
    assert _row(other).status == "pending"
    assert _row(other).attempts == 0
    assert io_loop.run_iteration(FakeClock(T0 + timedelta(minutes=14, seconds=59)), state) is False
    assert len(fake_telegram.calls) == 1


@pytest.mark.django_db(transaction=True)
def test_no_token_in_outbox_or_logs(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    row_a = _queue(a)
    row_b = _queue(b)
    fake_telegram.fail(
        TOKEN_A, status=403, json_body={"ok": False, "error_code": 403, "description": "x"}
    )
    fake_telegram.fail(TOKEN_B, exc=_refused(TOKEN_B))
    state = io_loop.RelayState()
    caplog.set_level(logging.DEBUG)

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    assert (_row(row_a).last_error, _row(row_b).last_error) == ("http_403", "connect_error")
    rows = repr(list(OutboxMessage.objects.values()))
    for token in (TOKEN_A, TOKEN_B):
        secret = token.split(":", 1)[1]
        for text in (caplog.text, rows, repr(state)):
            assert token not in text
            assert secret not in text
        for record in caplog.records:
            assert secret not in record.getMessage()


def test_bot_key_is_a_short_hash() -> None:
    key = io_loop.bot_key(TOKEN_A)

    assert re.fullmatch(r"[0-9a-f]{12}", key)
    assert key == hashlib.sha256(TOKEN_A.encode()).hexdigest()[:12]
    assert io_loop.bot_key(TOKEN_A) == key
    assert io_loop.bot_key(TOKEN_B) != key
    assert TOKEN_A.split(":", 1)[0] not in key
    assert TOKEN_A not in key


# Failures stay local to one row or one location (INV-13 shape)


@pytest.mark.django_db(transaction=True)
def test_render_failure_does_not_block_other_locations(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Both locations share one bot: a broken row must not back the bot off.
    broken = location_factory()
    healthy = location_factory(chat_id=CHAT_B)
    bad = _queue(broken, payload={})
    good = _queue(healthy)
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(bad)
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, "render_error")
    assert row.next_attempt_at == T0 + timedelta(minutes=15)
    assert _row(good).status == "sent"
    assert fake_telegram.sent == [_body(OFF_EN, CHAT_B)]
    assert io_loop.bot_key(TOKEN_A) not in state.not_before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("payload", [[300_000_000], 300_000_000, "5m"], ids=repr)
def test_a_payload_that_is_not_an_object_is_a_render_error(
    location_factory: Callable[..., Any], fake_telegram: Any, payload: Any
) -> None:
    # enqueue only writes objects, but the row is read back from a jsonb column.
    row = _queue(location_factory())
    OutboxMessage.objects.filter(pk=row.pk).update(payload=payload)
    fake_telegram.accept(TOKEN_A)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is False

    assert (_row(row).status, _row(row).last_error) == ("pending", "render_error")
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_a_row_that_cannot_be_claimed_is_not_sent(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The claim is the last gate before the HTTP call: a row that is no longer pending
    # (claimed elsewhere) is skipped, never sent twice.
    row = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)
    monkeypatch.setattr(outbox, "claim", lambda message_id: False)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is False

    assert len(fake_telegram.calls) == 0
    assert (_row(row).status, _row(row).attempts) == ("pending", 0)


@pytest.mark.django_db(transaction=True)
def test_unexpected_error_is_logged_without_details_and_others_continue(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = location_factory()
    healthy = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    bad = _queue(broken)
    _queue(healthy)
    fake_telegram.accept(TOKEN_B)
    real_claim = outbox.claim

    def claim(message_id: int) -> bool:
        if message_id == bad.pk:
            raise RuntimeError(f"database exploded near /bot{TOKEN_A}/")
        return real_claim(message_id)

    monkeypatch.setattr(outbox, "claim", claim)
    caplog.set_level(logging.DEBUG)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is True

    assert fake_telegram.sent == [_body(OFF_EN, CHAT_B)]
    assert _row(bad).status == "pending"
    relay_lines = [r for r in caplog.records if r.name == RELAY_LOGGER]
    assert [r.getMessage() for r in relay_lines] == [
        f"relay failed for location {broken.pk}: RuntimeError"
    ]
    assert relay_lines[0].exc_info is None
    assert TOKEN_A not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_idle_iteration_returns_false(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    # Nothing queued at all.
    assert io_loop.run_iteration(FakeClock(T0), state) is False
    # Queued, but not due until T0.
    off = _queue(location_factory())
    assert io_loop.run_iteration(FakeClock(T0 - _seconds(1)), state) is False

    assert len(fake_telegram.calls) == 0
    assert _row(off).status == "pending"


# The outbox functions the relay is built on


@pytest.mark.django_db
def test_subscriber_heads_returns_the_oldest_open_row_per_location(
    location_factory: Callable[..., Any],
) -> None:
    a = location_factory()
    b = location_factory()
    c = location_factory()
    done = _queue(a)
    head_a = _queue(a, "power_on", at=T0 + _seconds(60))
    _queue(a, "power_off", at=T0 + _seconds(120))
    gone = _queue(b)
    head_c = _queue(c)
    _queue(c, "power_on", at=T0 + _seconds(60))
    OutboxMessage.objects.filter(pk=done.pk).update(status="sent")
    OutboxMessage.objects.filter(pk=gone.pk).update(status="uncertain")
    OutboxMessage.objects.filter(pk=head_c.pk).update(status="sending")

    heads = outbox.subscriber_heads()

    # A sent or uncertain row is finished; a sending row still holds its location's line.
    assert [(h.pk, h.status) for h in heads] == [(head_a.pk, "pending"), (head_c.pk, "sending")]
    assert heads[0].location.pk == a.pk


@pytest.mark.django_db
def test_claim_moves_a_pending_row_to_sending_once(location_factory: Callable[..., Any]) -> None:
    row = _queue(location_factory())

    assert outbox.claim(row.pk) is True

    claimed = _row(row)
    assert (claimed.status, claimed.attempts) == ("sending", 1)
    # Already claimed, finished, or missing: nothing changes.
    assert outbox.claim(row.pk) is False
    OutboxMessage.objects.filter(pk=row.pk).update(status="sent")
    assert outbox.claim(row.pk) is False
    assert outbox.claim(row.pk + 1000) is False
    assert _row(row).attempts == 1


@pytest.mark.django_db
def test_mark_functions_store_short_codes_only(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    retry = _queue(location)
    unsure = _queue(location, "power_on", at=T0 + _seconds(60))
    long_code = "x" * 100
    assert outbox.claim(unsure.pk) is True

    assert outbox.mark_retry(retry.pk, T0 + _seconds(5), long_code) is True
    assert outbox.mark_uncertain(unsure.pk, long_code) is True

    assert (_row(retry).status, _row(retry).last_error) == ("pending", "x" * 64)
    assert _row(retry).next_attempt_at == T0 + _seconds(5)
    assert (_row(unsure).status, _row(unsure).last_error) == ("uncertain", "x" * 64)
    # Only a claimed row can be marked sent or uncertain.
    assert outbox.mark_sent(retry.pk, T0) is False
    assert outbox.mark_uncertain(retry.pk, "read_timeout") is False
    assert _row(retry).status == "pending"


@pytest.mark.django_db
def test_recover_interrupted_marks_sending_uncertain(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    interrupted = _queue(location)
    waiting = _queue(location, "power_on", at=T0 + _seconds(60))
    finished = _queue(location, "power_off", at=T0 + _seconds(120))
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)
    OutboxMessage.objects.filter(pk=finished.pk).update(status="sent", sent_at=T0)

    assert outbox.recover_interrupted() == [
        outbox.RowRef(interrupted.pk, "subscriber", location.pk)
    ]

    row = _row(interrupted)
    assert (row.status, row.last_error, row.attempts) == ("uncertain", "interrupted", 1)
    assert _row(waiting).status == "pending"
    assert _row(finished).status == "sent"
    # Nothing left to recover.
    assert outbox.recover_interrupted() == []


# WR-04 (D-13): an error after the claim never blocks a queue or loses a known outcome

OUTCOME_LINE = "relay: could not record the outcome of alert {} (OperationalError); retrying"


def _db_blip() -> OperationalError:
    return OperationalError("server closed the connection unexpectedly")


def _fail_once(monkeypatch: pytest.MonkeyPatch, target: Any, name: str) -> None:
    """Make ``target.name`` raise a DB error on its first call only."""
    real = getattr(target, name)
    failures = [_db_blip()]

    def flaky(*args: Any, **kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)


def _client_fails_once(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """The relay's TelegramClient raises once, after the claim and before any HTTP call."""
    real = io_loop.TelegramClient
    failures = [RuntimeError(f"client setup failed for /bot{token}/")]

    def client(bot_token: str, **kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return real(bot_token, **kwargs)

    monkeypatch.setattr(io_loop, "TelegramClient", client)


def _relay_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER]


def _gap_notice(start: datetime, end: datetime, at: datetime = T0) -> OutboxMessage:
    payload = {"start_us": ops.instant_us(start), "end_us": ops.instant_us(end)}
    with transaction.atomic():
        return outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload=payload, recorded_at=at)


def _kill_my_session() -> None:
    """End this thread's own DB session from another one while it is idle (a DB restart)."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        row = cur.fetchone()
    assert row is not None

    def terminate() -> bool:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s, 5000)", [row[0]])
            result = cur.fetchone()
        return bool(result and result[0])

    killer = Actor(terminate)
    killer.start()
    killer.join(10)
    assert killer.exc is None
    assert killer.result is True


def _kept_ok(location: Any, answered_at: datetime) -> tuple[OutboxMessage, io_loop.RelayState]:
    """A claimed OFF that Telegram accepted at ``answered_at``, its "sent" not yet written."""
    row = _queue(location)
    assert outbox.claim(row.pk) is True
    state = io_loop.RelayState()
    state.unapplied[row.pk] = io_loop.Unapplied(
        _row(row), 1, SendResult("ok"), answered_at, io_loop.bot_key(TOKEN_A)
    )
    return row, state


@pytest.mark.django_db(transaction=True)
def test_WR04_apply_failure_does_not_block_location(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    off = _queue(location)
    on = _queue(location, "power_on", at=T0 + _seconds(30))
    clock = FakeClock(T0 + _seconds(60))
    # Telegram accepts the OFF after 2 s; then writing "sent" fails once (a DB blip).
    fake_telegram.answer(TOKEN_A, lambda: clock.advance(seconds=2))
    fake_telegram.accept(TOKEN_A)
    _fail_once(monkeypatch, outbox, "mark_sent")
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True

    assert (_row(off).status, _row(on).status) == ("sending", "pending")
    assert _relay_lines(caplog) == [OUTCOME_LINE.format(off.pk)]
    assert list(state.unapplied) == [off.pk]

    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is True

    # The known outcome is written first, at the time Telegram answered; then the ON goes.
    assert (_row(off).status, _row(off).sent_at) == ("sent", T0 + _seconds(62))
    assert (_row(on).status, _row(on).sent_at) == ("sent", T0 + _seconds(63))
    assert fake_telegram.sent == [_body(OFF_EN), _body(ON_EN)]
    assert state.unapplied == {}
    assert _relay_lines(caplog) == [OUTCOME_LINE.format(off.pk)]


@pytest.mark.django_db(transaction=True)
def test_WR04_unapplied_ok_becomes_sent_not_uncertain_at_activation(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    answered = T0 + _seconds(3)
    row, state = _kept_ok(location_factory(), answered)
    caplog.set_level(logging.INFO)

    # A new lease generation: the known outcome is written before leftover "sending" rows
    # are declared uncertain, so nothing is left for the recovery.
    assert io_loop.activate(state, FakeClock(T0 + timedelta(minutes=10))) == 0

    assert (_row(row).status, _row(row).sent_at, _row(row).last_error) == ("sent", answered, "")
    assert state.unapplied == {}
    assert "may not have been delivered" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_WR04_activation_retries_while_the_flush_fails(
    location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    answered = T0 + _seconds(3)
    row, state = _kept_ok(location_factory(), answered)
    clock = FakeClock(T0 + timedelta(minutes=10))

    def broken(message_id: int, now: datetime) -> bool:
        raise _db_blip()

    monkeypatch.setattr(outbox, "mark_sent", broken)

    # The DB is still failing: activation raises (the I/O thread retries it), and the row
    # is neither declared uncertain nor forgotten.
    with pytest.raises(OperationalError):
        io_loop.activate(state, clock)
    assert _row(row).status == "sending"
    assert list(state.unapplied) == [row.pk]

    monkeypatch.undo()
    assert io_loop.activate(state, clock) == 0
    assert (_row(row).status, _row(row).sent_at) == ("sent", answered)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("result", "status", "last_error", "next_attempt_s"),
    [
        (SendResult("maybe_delivered", code="read_timeout"), "uncertain", "read_timeout", None),
        (SendResult("rate_limited", retry_after=30, code="429"), "pending", "429", 33),
        (SendResult("not_sent", code="connect_error"), "pending", "connect_error", 5),
    ],
    ids=["maybe_delivered", "rate_limited", "not_sent"],
)
def test_WR04_flush_applies_each_kept_outcome(
    location_factory: Callable[..., Any],
    result: SendResult,
    status: str,
    last_error: str,
    next_attempt_s: int | None,
) -> None:
    # The outcome is applied as if it had been written right away: waits count from when
    # Telegram answered (T0 + 3 s), and the bot's backoff is set under the kept key.
    row = _queue(location_factory())
    assert outbox.claim(row.pk) is True
    state = io_loop.RelayState()
    key = io_loop.bot_key(TOKEN_A)
    state.unapplied[row.pk] = io_loop.Unapplied(_row(row), 1, result, T0 + _seconds(3), key)

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(4)), state) is False

    after = _row(row)
    assert (after.status, after.last_error) == (status, last_error)
    if next_attempt_s is not None:
        assert after.next_attempt_at == T0 + _seconds(next_attempt_s)
        assert state.not_before[key] == T0 + _seconds(next_attempt_s)
    assert state.unapplied == {}


@pytest.mark.django_db(transaction=True)
def test_WR04_error_before_http_call_returns_to_pending(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)
    _client_fails_once(monkeypatch, TOKEN_A)
    caplog.set_level(logging.DEBUG)
    state = io_loop.RelayState()

    # The exception came before the HTTP call, so the request provably never left.
    assert io_loop.run_iteration(FakeClock(T0), state) is False

    after = _row(row)
    assert (after.status, after.last_error, after.next_attempt_at) == (
        "pending",
        "pre_send_error",
        T0,
    )
    assert len(fake_telegram.calls) == 0
    assert _relay_lines(caplog) == [
        f"relay: alert {row.pk} failed before the send (RuntimeError); it is pending again"
    ]
    assert TOKEN_A not in caplog.text

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert fake_telegram.sent == [_body(OFF_EN)]
    assert _row(row).status == "sent"
    for seconds in (2, 60):
        assert io_loop.run_iteration(FakeClock(T0 + _seconds(seconds)), state) is False
    assert len(fake_telegram.calls) == 1
    assert not OutboxMessage.objects.filter(status="uncertain").exists()


@pytest.mark.django_db(transaction=True)
def test_WR04_pre_send_error_with_a_failed_reset_is_retried(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The client fails before the send, and putting the row back to "pending" fails too:
    # the row stays "sending" for one pass, and the next pass resets it before any claim.
    row = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)
    _client_fails_once(monkeypatch, TOKEN_A)
    _fail_once(monkeypatch, outbox, "mark_retry")
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is False
    assert _row(row).status == "sending"
    assert list(state.unapplied) == [row.pk]

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert (_row(row).status, len(fake_telegram.calls)) == ("sent", 1)
    assert state.unapplied == {}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("committed", [False, True], ids=["claim_lost", "claimed_then_error"])
def test_WR04_claim_with_unknown_outcome_is_reset(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    committed: bool,
) -> None:
    # The claim raises: either it never reached the DB, or it committed and the answer was
    # lost. No HTTP call was made in either case, so the row may go back to "pending".
    row = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)
    real_claim = outbox.claim
    failures = [_db_blip()]

    def claim(message_id: int) -> bool:
        if failures:
            if committed:
                real_claim(message_id)
            raise failures.pop()
        return real_claim(message_id)

    monkeypatch.setattr(outbox, "claim", claim)
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is False

    assert len(fake_telegram.calls) == 0
    assert _row(row).status == ("sending" if committed else "pending")
    assert list(state.unapplied) == [row.pk]
    assert _relay_lines(caplog) == [
        f"relay: could not claim alert {row.pk} (OperationalError); retrying"
    ]

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert fake_telegram.sent == [_body(OFF_EN)]
    assert (_row(row).status, _row(row).last_error) == ("sent", "")
    assert state.unapplied == {}
    assert not OutboxMessage.objects.filter(status="uncertain").exists()


@pytest.mark.django_db(transaction=True)
def test_WR04_flush_runs_on_a_replaced_connection(location_factory: Callable[..., Any]) -> None:
    answered = T0 + _seconds(3)
    row, state = _kept_ok(location_factory(), answered)

    try:
        # The DB dropped this thread's session while it was idle (D-16): the pass replaces
        # the connection before it writes the kept outcome.
        _kill_my_session()
        assert io_loop.run_iteration(FakeClock(T0 + _seconds(5)), state) is False
    finally:
        # On a failure, never leave this thread's dead session to the teardown flush.
        connection.close()

    assert (_row(row).status, _row(row).sent_at) == ("sent", answered)
    assert state.unapplied == {}


@pytest.mark.django_db(transaction=True)
def test_WR04_ops_outcome_failure_does_not_block_the_ops_queue(
    fake_telegram: Any,
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ops_settings.CFG = dataclasses.replace(ops_settings.CFG, display_tz="Europe/Kyiv")
    # 10:00:12 - 10:10:40 and 10:20:00 - 10:25:00 in Kyiv (UTC+3 on 2026-10-01).
    first = _gap_notice(
        datetime(2026, 10, 1, 7, 0, 12, tzinfo=UTC), datetime(2026, 10, 1, 7, 10, 40, tzinfo=UTC)
    )
    second = _gap_notice(
        datetime(2026, 10, 1, 7, 20, tzinfo=UTC), datetime(2026, 10, 1, 7, 25, tzinfo=UTC)
    )
    fake_telegram.accept(OPS_BOT_TOKEN)
    _fail_once(monkeypatch, outbox, "mark_sent")
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    assert (_row(first).status, _row(second).status) == ("sending", "pending")
    assert _relay_lines(caplog) == [OUTCOME_LINE.format(first.pk)]

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True

    # The kept "sent" is written first; the second notice follows, and the first was
    # sent exactly once.
    assert (_row(first).status, _row(first).sent_at) == ("sent", T0)
    assert (_row(second).status, _row(second).sent_at) == ("sent", T0 + _seconds(1))
    texts = [body["text"] for body in fake_telegram.sent]
    assert len(texts) == 2
    assert texts[0].startswith("⏸ Monitoring gap 01.10 10:00:12 – ")
    assert texts[1].startswith("⏸ Monitoring gap 01.10 10:20:00 – ")
    assert {body["chat_id"] for body in fake_telegram.sent} == {OPS_CHAT_ID}
    assert state.unapplied == {}


@pytest.mark.django_db(transaction=True)
def test_WR04_ops_pre_send_error_returns_to_pending(
    fake_telegram: Any, ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = _gap_notice(T0 - timedelta(minutes=10), T0)
    fake_telegram.accept(OPS_BOT_TOKEN)
    _client_fails_once(monkeypatch, OPS_BOT_TOKEN)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is False

    after = _row(notice)
    assert (after.status, after.last_error) == ("pending", "pre_send_error")
    assert len(fake_telegram.calls) == 0

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert (_row(notice).status, len(fake_telegram.calls)) == ("sent", 1)
    assert not OutboxMessage.objects.filter(status="uncertain").exists()


# B1 (wave 2 audit): a failing admin chat never delays a subscriber alert, even when the
# admin reuses a location's bot as OPS_BOT_TOKEN (INV-20 #2, ALRT-06)


def _shared_bot(fake: Any, token: str, ops_answers: list[tuple[int, Any]]) -> list[int]:
    """One bot for a location and the admin chat, faked per chat.

    The admin chat gets ``ops_answers`` (status, JSON body) in turn, then ok; every other
    chat is accepted. Returns the chat id of every request, in order.
    """
    chats: list[int] = []

    def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        body = json.loads(request.body or b"{}")
        chats.append(body["chat_id"])
        if body["chat_id"] == OPS_CHAT_ID and ops_answers:
            status, answer = ops_answers.pop(0)
            return status, {}, json.dumps(answer)
        fake.sent.append(body)
        return 200, {}, json.dumps({"ok": True, "result": {"message_id": len(fake.sent)}})

    fake.rsps.add_callback(
        responses.POST,
        f"{fake.API}/bot{token}/sendMessage",
        callback=callback,
        content_type="application/json",
    )
    return chats


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("status", "answer", "ops_wait"),
    [
        (
            403,
            {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member"},
            timedelta(minutes=15),
        ),
        (429, _too_many_requests(30), timedelta(seconds=30)),
        (502, {"ok": False, "error_code": 502}, timedelta(seconds=2)),
    ],
    ids=["403", "429", "502"],
)
def test_B1_an_admin_chat_failure_on_a_shared_bot_never_delays_the_location(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    settings: Any,
    status: int,
    answer: Any,
    ops_wait: timedelta,
) -> None:
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token=TOKEN_A, ops_chat_id=OPS_CHAT_ID)
    location = location_factory(bot_token=TOKEN_A)
    notice = _gap_notice(T0 - timedelta(minutes=10), T0)
    chats = _shared_bot(fake_telegram, TOKEN_A, [(status, answer)])
    state = io_loop.RelayState()

    # Only the admin chat fails.
    assert io_loop.run_iteration(FakeClock(T0), state) is True
    assert chats == [OPS_CHAT_ID]
    assert (_row(notice).status, _row(notice).next_attempt_at) == ("pending", T0 + ops_wait)

    # The location's OFF, queued a second later, goes out at once on the same bot.
    off = _queue(location, at=T0 + _seconds(1))
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert (_row(off).status, _row(off).sent_at) == ("sent", T0 + _seconds(1))
    assert chats == [OPS_CHAT_ID, DEFAULT_CHAT_ID]

    # The admin chat still waits out its own backoff, and only then gets the notice.
    due = T0 + ops_wait
    assert io_loop.run_iteration(FakeClock(due - _seconds(1)), state) is False
    assert io_loop.run_iteration(FakeClock(due), state) is True
    assert (_row(notice).status, _row(notice).sent_at) == ("sent", due)
    assert chats == [OPS_CHAT_ID, DEFAULT_CHAT_ID, OPS_CHAT_ID]


# INV-14 #1: a 429 on one bot never delays another (ALRT-06, D-14)


def _resume_detection(at: datetime) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": at, "web_started_at": None}
    )


def _beat(location: Any, first: datetime, last: datetime) -> None:
    at = first
    while at <= last:
        transitions.record_heartbeat(location.pk, at)
        at += timedelta(minutes=1)


@pytest.mark.django_db(transaction=True)
def test_INV14_429_on_bot_a_never_delays_b(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _resume_detection(T0 - timedelta(hours=1))
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    row_a = _queue(a)
    # B beats every minute until 10:05:10 and then goes silent: its OFF is due at 10:06:41.
    _beat(b, T0.replace(minute=0, second=10), T0.replace(minute=5, second=10))
    fake_telegram.fail(TOKEN_A, status=429, json_body=_too_many_requests(30))
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(TOKEN_B)
    state = io_loop.RelayState()

    # A cycle and a pass every second; bot A answered 429 retry_after=30 at T0.
    for second in range(31):
        now = T0 + _seconds(second)
        detection.run_cycle(now)
        io_loop.run_iteration(FakeClock(now), state)
        if second == 10:
            [off_b] = OutboxMessage.objects.filter(location=b)
            assert (off_b.recorded_at, off_b.status, off_b.sent_at) == (now, "sent", now)
        assert _calls_to(fake_telegram, TOKEN_A) == (1 if second < 30 else 2)

    assert fake_telegram.sent == [_body(OFF_EN, CHAT_B), _body(OFF_EN)]
    assert _row(row_a).status == "sent"


# INV-15 #1 and #2: Telegram blocked for 10 min, and a worker killed mid-delivery


def _blocked_until(fake: Any, token: str, clock: FakeClock, until: datetime) -> list[Any]:
    """One callback for ``token``: connections refused before ``until``, accepted from then.

    Returns the list of (time, text) of every accepted message.
    """
    delivered: list[Any] = []

    def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        if clock.now() < until:
            raise _refused(token)
        body = json.loads(request.body or b"{}")
        delivered.append((clock.now(), body["text"]))
        return 200, {}, json.dumps({"ok": True, "result": {"message_id": len(delivered)}})

    fake.rsps.add_callback(
        responses.POST,
        f"{fake.API}/bot{token}/sendMessage",
        callback=callback,
        content_type="application/json",
    )
    return delivered


@pytest.mark.django_db(transaction=True)
def test_INV15_telegram_blocked_10_min_off_then_on_once_with_event_times(
    location_factory: Callable[..., Any], fake_telegram: Any, settings: Any
) -> None:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    # In Kyiv (UTC+3): last heartbeat 10:02:00, power back 10:06:00, Telegram blocked
    # until 10:10:00.
    last_beat = datetime(2026, 10, 1, 7, 2, tzinfo=UTC)
    off_recorded = datetime(2026, 10, 1, 7, 3, 31, tzinfo=UTC)
    restored_at = datetime(2026, 10, 1, 7, 6, tzinfo=UTC)
    unblocked = datetime(2026, 10, 1, 7, 10, tzinfo=UTC)
    _resume_detection(last_beat - timedelta(hours=1))
    location = location_factory()
    _beat(location, last_beat - timedelta(minutes=7), last_beat)
    clock = FakeClock(off_recorded)
    delivered = _blocked_until(fake_telegram, TOKEN_A, clock, unblocked)
    state = io_loop.RelayState()

    # A detection cycle and a relay pass every 5 s from 10:03:31 to 10:12:01. From 10:06:00
    # the device beats every minute again.
    beats = [restored_at + timedelta(minutes=n) for n in range(7)]
    while clock.now() <= unblocked + timedelta(minutes=2):
        while beats and beats[0] <= clock.now():
            gate = transitions.record_heartbeat(location.pk, beats.pop(0))
            assert gate == ("restored" if len(beats) == 6 else "plain")
        detection.run_cycle(clock.now())
        io_loop.run_iteration(clock, state)
        clock.advance(seconds=5)

    assert [text for _, text in delivered] == [
        "🔴 10:02 <b>POWER OFF</b>\n⚡ Power was ON for: <b>7m</b>",
        "🟢 10:06 <b>POWER ON</b>\n⚡ Power was OFF for: <b>4m</b>",
    ]
    assert all(unblocked <= at <= unblocked + _seconds(60) for at, _ in delivered)
    rows = OutboxMessage.objects.filter(location=location).order_by("id")
    assert [(r.kind, r.status, r.recorded_at) for r in rows] == [
        ("power_off", "sent", off_recorded),
        ("power_on", "sent", restored_at),
    ]


@pytest.mark.django_db(transaction=True)
def test_INV15_worker_killed_after_commit_delivers_once(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _resume_detection(T0 - timedelta(hours=1))
    location = location_factory()
    _beat(location, T0.replace(minute=0, second=0), T0.replace(minute=5, second=0))
    # The OFF commits with its transition; the worker dies before any relay pass.
    assert detection.run_cycle(T0) == 1
    fake_telegram.accept(TOKEN_A)

    # The next worker starts with nothing in memory.
    state = io_loop.RelayState()
    clock = FakeClock(T0 + _seconds(20))
    assert io_loop.activate(state, clock) == 0
    for _ in range(3):
        io_loop.run_iteration(clock, state)
        clock.advance(seconds=1)

    assert fake_telegram.sent == [_body(OFF_EN)]
    [row] = OutboxMessage.objects.all()
    assert (row.status, row.attempts, row.sent_at) == ("sent", 1, T0 + _seconds(20))


@pytest.mark.django_db(transaction=True)
def test_INV15_killed_after_claim_is_uncertain_never_sent(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    row = _queue(location)
    # Claimed by a worker that died before the outcome was written: it may have been sent.
    OutboxMessage.objects.filter(pk=row.pk).update(status="sending", attempts=1)
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    clock = FakeClock(T0 + _seconds(60))

    assert io_loop.activate(state, clock) == 1
    for _ in range(3):
        io_loop.run_iteration(clock, state)
        clock.advance(seconds=1)

    assert (_row(row).status, _row(row).last_error) == ("uncertain", "interrupted")
    assert _calls_to(fake_telegram, TOKEN_A) == 0
    [notice] = OutboxMessage.objects.filter(channel="ops")
    assert (notice.kind, notice.payload, notice.status) == (
        "ops_uncertain",
        {"message_id": row.pk},
        "sent",
    )
    assert _calls_to(fake_telegram, OPS_BOT_TOKEN) == 1
