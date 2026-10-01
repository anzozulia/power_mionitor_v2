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

``run_iteration`` calls ``close_old_connections()``, so every test that runs it is
``django_db(transaction=True)``. Time is driven only through the ``now`` argument.
Telegram is faked at the HTTP boundary (``fake_telegram``).
"""

import hashlib
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID
from django.db import transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.worker import io_loop

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
    on = _queue(location, "power_on", at=T0 + timedelta(minutes=53))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    now = T0 + timedelta(hours=1)

    # Both rows are due, but only the location's oldest row goes out in a pass.
    assert io_loop.run_iteration(now, state) is True

    assert fake_telegram.sent == [_body(OFF_EN)]
    sent = _row(off)
    assert (sent.status, sent.sent_at, sent.attempts, sent.last_error) == ("sent", now, 1, "")
    assert _row(on).status == "pending"

    assert io_loop.run_iteration(now + _seconds(1), state) is True

    assert fake_telegram.sent == [_body(OFF_EN), _body(ON_EN)]
    assert (_row(on).status, _row(on).sent_at) == ("sent", now + _seconds(1))
    assert io_loop.run_iteration(now + _seconds(2), state) is False
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

    assert io_loop.run_iteration(T0, io_loop.RelayState()) is True

    assert fake_telegram.sent == [_body(OFF_UK)]


# Ambiguous sends are never resent (INV-16)


@pytest.mark.django_db(transaction=True)
def test_read_timeout_marks_uncertain_and_never_resends(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    off = _queue(location, "power_off")
    on = _queue(location, "power_on", at=T0 + timedelta(minutes=53))
    # The first call (the OFF) reaches Telegram, then the answer times out; later calls succeed.
    fake_telegram.fail(TOKEN_A, exc=requests.ReadTimeout("read timed out"))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    now = T0 + timedelta(hours=1)

    assert io_loop.run_iteration(now, state) is True

    row = _row(off)
    assert (row.status, row.last_error, row.attempts, row.sent_at) == (
        "uncertain",
        "read_timeout",
        1,
        None,
    )
    # The uncertain OFF no longer holds the line: the ON is the location's head now.
    assert io_loop.run_iteration(now + _seconds(1), state) is True
    assert fake_telegram.sent == [_body(ON_EN)]
    assert _row(on).status == "sent"
    for minutes in (1, 10, 60):
        assert io_loop.run_iteration(now + timedelta(minutes=minutes), state) is False
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
    on = _queue(location, "power_on", at=T0 + timedelta(minutes=53))
    OutboxMessage.objects.filter(pk=interrupted.pk).update(status="sending", attempts=1)
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    now = T0 + timedelta(hours=1)

    assert io_loop.run_iteration(now, state) is False
    assert len(fake_telegram.calls) == 0

    assert outbox.recover_interrupted() == 1
    assert io_loop.run_iteration(now, state) is True
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

    assert io_loop.run_iteration(T0, state) is True

    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "connect_timeout")
    assert row.next_attempt_at == T0 + _seconds(2)
    # Not due yet: nothing is sent, and nothing sleeps.
    assert io_loop.run_iteration(T0 + _seconds(1), state) is False
    assert len(fake_telegram.calls) == 1
    due = row.next_attempt_at
    # 2 ** attempts: 4, 8, 16, then 30 instead of 32.
    for attempts, delay in [(2, 4), (3, 8), (4, 16), (5, 30)]:
        assert io_loop.run_iteration(due, state) is True
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

    assert io_loop.run_iteration(T0, state) is True

    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "http_502")
    assert row.next_attempt_at == T0 + _seconds(2)
    assert io_loop.run_iteration(T0 + _seconds(2), state) is True
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
    assert io_loop.run_iteration(T0, state) is True
    assert time.monotonic() - started < 1.0

    waiting = _row(row_a)
    assert (waiting.status, waiting.last_error) == ("pending", "429")
    assert waiting.next_attempt_at >= T0 + _seconds(30)
    assert state.not_before[io_loop.bot_key(TOKEN_A)] >= T0 + _seconds(30)
    assert _row(row_b).status == "sent"
    assert fake_telegram.sent == [_body(OFF_EN, CHAT_B)]
    for offset in (1, 15, 29):
        assert io_loop.run_iteration(T0 + _seconds(offset), state) is False
    assert _calls_to(fake_telegram, TOKEN_A) == 1

    assert io_loop.run_iteration(T0 + _seconds(30), state) is True
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

    assert io_loop.run_iteration(T0, state) is True

    row = _row(off)
    assert (row.status, row.last_error) == ("pending", "429")
    assert row.next_attempt_at == T0 + _seconds(io_loop.MAX_RETRY_AFTER_S)
    assert io_loop.MAX_RETRY_AFTER_S == 3600


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

    assert io_loop.run_iteration(T0, state) is True

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
    assert io_loop.run_iteration(T0 + timedelta(minutes=14, seconds=59), state) is False
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

    assert io_loop.run_iteration(T0, state) is True

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

    assert io_loop.run_iteration(T0, state) is True

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

    assert io_loop.run_iteration(T0, io_loop.RelayState()) is False

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

    assert io_loop.run_iteration(T0, io_loop.RelayState()) is False

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

    assert io_loop.run_iteration(T0, io_loop.RelayState()) is True

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
    assert io_loop.run_iteration(T0, state) is False
    # Queued, but not due until T0.
    off = _queue(location_factory())
    assert io_loop.run_iteration(T0 - _seconds(1), state) is False

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

    assert outbox.recover_interrupted() == 1

    row = _row(interrupted)
    assert (row.status, row.last_error, row.attempts) == ("uncertain", "interrupted", 1)
    assert _row(waiting).status == "pending"
    assert _row(finished).status == "sent"
    # Nothing left to recover.
    assert outbox.recover_interrupted() == 0
