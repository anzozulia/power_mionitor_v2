"""Ops notices reach the admin chat after subscriber alerts, once (OPS-01, ALRT-05, ALRT-06).

Scenarios:
- INV-16 #2 with D-11 #5: an ambiguous subscriber send (the request went out, then the
  answer timed out) is never resent, and the admin gets exactly one "may not have been
  delivered" notice, queued in the same transaction that marks the row uncertain.
- INV-20 #2 (D-11, D-14): every pass drains the ops queue after every due subscriber head,
  one ops row per pass, with the ops bot's own backoff, so a broken or rate-limited admin
  chat never delays a subscriber alert.
- INV-20 / D-09: the only destination is the env-configured OPS_CHAT_ID, sent with
  OPS_BOT_TOKEN. With no ops chat configured no Telegram request is made for an ops row.
- OPS-08 / D-10: an ops payload holds integers only. The text and the location name are
  read and rendered at send time, and the name is HTML-escaped.
- B3 (wave 2 audit): with no ops chat, a database error while a notice is rendered is
  never swallowed. It reaches the caller, so ``mark_uncertain`` and activation fail and are
  retried instead of committing nothing while reporting success. Any other render error
  is still one WARNING, and the caller's transaction commits.
- CHRT-05 / INV-17 #1 (Phase 3 D-07): the "cannot pin today's chart" and "pinning works
  again" notices travel the same outbox path to the ops chat, with an integer payload and
  the location name read at send time.

Relay cases call ``io_loop.run_iteration``, which calls ``close_old_connections()``, so
they are ``django_db(transaction=True)``. Time comes only from the ``FakeClock`` passed in;
Telegram is faked at the HTTP boundary (``fake_telegram``). Expected local times are
Europe/Kyiv, UTC+3 on 2026-10-01.
"""

import ast
import dataclasses
import logging
import pathlib
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    OPS_CHAT_ID,
    Actor,
    FakeClock,
)
from django.db import DatabaseError, InterfaceError, OperationalError, connection, transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

import powermon
from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.worker import io_loop

TOKEN_A = DEFAULT_BOT_TOKEN
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
KYIV = "Europe/Kyiv"
# An OFF transition recorded at 10:06:31 UTC for an outage that started at 10:05:00 UTC.
T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
UNCERTAIN_OFF = (
    "❓ OFF alert for Test location (event 13:05) may not have been delivered "
    "(Telegram timed out after the request was sent). It will not be resent; "
    "please check the channel."
)
OPS_LOGGER = "powermon.alerts.ops"
RELAY_LOGGER = "powermon.worker.io_loop"
GAP_SAMPLE = "⏸ Monitoring gap 01.10 10:00:12 – 10:10:40 (10m 28s)"
# D-07 (03-CONTEXT Specific Ideas): the bot posted today's chart but cannot pin it.
PIN_FAILED = (
    "📌 Can't pin today's chart for Test location (Telegram: http_400). "
    "The chart is still posted and refreshed; pinning is retried every 15 min, "
    "or at each chart update if it updates less often. "
    "Check that the bot may pin messages in the chat."
)
PIN_RESTORED = "📌 Pinning works again for Test location."
# F-04 (quick task 261008-vdk): Telegram refused today's chart post or update for good.
CHART_FAILING = (
    "🖼 Can't post or update the weekly chart for Test location (Telegram: http_403). "
    "Subscribers see no chart, or an old one. It is retried every 15 min. "
    "Check that the bot is an admin of the channel and may post photos."
)


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


@pytest.fixture
def no_ops_chat(settings: Any) -> Any:
    """``settings.CFG`` with no ops chat, whatever the env file says (D-09)."""
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token="", ops_chat_id=None)
    return settings


def _seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


def _queue(location: Any, *, at: datetime = T0) -> OutboxMessage:
    """Queue one OFF alert recorded (and so due) at ``at``, as a transition would."""
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - _seconds(91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


def _uncertain_notice(location: Any, *, at: datetime = T0) -> OutboxMessage:
    """An uncertain subscriber row and its queued ops notice, as ops.mark_uncertain leaves them."""
    with transaction.atomic():
        alert = outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - _seconds(91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )
        OutboxMessage.objects.filter(pk=alert.pk).update(
            status="uncertain", attempts=1, last_error="read_timeout"
        )
        return outbox.enqueue_ops(
            outbox.KIND_OPS_UNCERTAIN,
            payload={"message_id": alert.pk},
            recorded_at=at,
            location_id=location.pk,
        )


def _row(message: OutboxMessage) -> OutboxMessage:
    return OutboxMessage.objects.get(pk=message.pk)


def _ops_rows() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(channel="ops").order_by("id"))


def _body(text: str, chat_id: int) -> dict[str, Any]:
    return {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}


def _bots(fake: Any) -> list[str]:
    """Which bot each request went to, in order: "A", "B" or "ops"."""
    names = {TOKEN_A: "A", TOKEN_B: "B", OPS_BOT_TOKEN: "ops"}
    found = []
    for call in fake.calls:
        found.extend(name for token, name in names.items() if f"/bot{token}/" in call.request.url)
    return found


def _ops_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == OPS_LOGGER]


def _gap_payload() -> dict[str, int]:
    """The D-11 gap sample: 10:00:12 - 10:10:40 in Kyiv on 01.10."""
    return {
        "start_us": ops.instant_us(datetime(2026, 10, 1, 7, 0, 12, tzinfo=UTC)),
        "end_us": ops.instant_us(datetime(2026, 10, 1, 7, 10, 40, tzinfo=UTC)),
    }


def _refused(token: str) -> requests.ConnectionError:
    # A real connect-phase exception carries the URL, and so the token, in its text (P-12).
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


def _too_many_requests(retry_after: int) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": 429,
        "description": f"Too Many Requests: retry after {retry_after}",
        "parameters": {"retry_after": retry_after},
    }


# An ambiguous send: never resent, one notice (INV-16 #2, D-11 #5, D-13)


@pytest.mark.django_db(transaction=True)
def test_INV16_uncertain_sends_one_ops_notice(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    # Bot A's request reaches Telegram, then the answer times out; later calls would succeed.
    fake_telegram.fail(TOKEN_A, exc=requests.ReadTimeout("read timed out"))
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(off)
    assert (row.status, row.last_error, row.attempts) == ("uncertain", "read_timeout", 1)
    [notice] = _ops_rows()
    assert (notice.kind, notice.payload, notice.location_id) == (
        "ops_uncertain",
        {"message_id": off.pk},
        location.pk,
    )
    # The notice went out in the same pass, after the subscriber head, to the ops chat only.
    assert _bots(fake_telegram) == ["A", "ops"]
    assert fake_telegram.sent == [_body(UNCERTAIN_OFF, OPS_CHAT_ID)]
    assert (notice.status, notice.sent_at) == ("sent", T0)

    for seconds in (1, 60, 3600):
        assert io_loop.run_iteration(FakeClock(T0 + _seconds(seconds)), state) is False
    assert _bots(fake_telegram) == ["A", "ops"]
    assert len(_ops_rows()) == 1
    assert _row(off).status == "uncertain"


@pytest.mark.django_db(transaction=True)
def test_ops_notice_escapes_the_location_name(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    _queue(location_factory(name="<b>A&B</b>"))
    fake_telegram.fail(TOKEN_A, exc=requests.ReadTimeout("read timed out"))
    fake_telegram.accept(OPS_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is True

    [sent] = fake_telegram.sent
    assert "OFF alert for &lt;b&gt;A&amp;B&lt;/b&gt; (event 13:05)" in sent["text"]
    assert "<b>A&B</b>" not in sent["text"]
    assert sent["parse_mode"] == "HTML"


@pytest.mark.django_db(transaction=True)
def test_an_uncertain_ops_row_is_only_logged(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A notice about a notice would loop on a broken admin chat (the D-08 rule).
    notice = _uncertain_notice(location_factory())
    fake_telegram.fail(OPS_BOT_TOKEN, exc=requests.ReadTimeout("read timed out"))
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    assert (_row(notice).status, _row(notice).last_error) == ("uncertain", "read_timeout")
    assert [r.pk for r in _ops_rows()] == [notice.pk]
    lines = [r.getMessage() for r in caplog.records if r.name == OPS_LOGGER]
    assert lines == [f"ops notice {notice.pk} may not have been delivered; it is not resent"]
    for seconds in (1, 60):
        assert io_loop.run_iteration(FakeClock(T0 + _seconds(seconds)), state) is False
    assert _bots(fake_telegram) == ["ops"]


@pytest.mark.django_db(transaction=True)
def test_mark_uncertain_changes_only_a_sending_row(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    location = location_factory()
    pending = _queue(location)

    # Not claimed: nothing changes and no notice is queued.
    assert ops.mark_uncertain(pending.pk, "read_timeout", T0) is False
    assert ops.mark_uncertain(pending.pk + 1000, "read_timeout", T0) is False
    assert _row(pending).status == "pending"
    assert _ops_rows() == []

    assert outbox.claim(pending.pk) is True
    assert ops.mark_uncertain(pending.pk, "read_timeout", T0) is True
    # Already uncertain: a second outcome cannot queue a second notice.
    assert ops.mark_uncertain(pending.pk, "read_timeout", T0 + _seconds(1)) is False
    [notice] = _ops_rows()
    assert (notice.payload, notice.recorded_at, notice.next_attempt_at) == (
        {"message_id": pending.pk},
        T0,
        T0,
    )


# Drain order: ops after subscribers, one ops row per pass (INV-20 #2, D-11, ALRT-06)


@pytest.mark.django_db(transaction=True)
def test_ops_notices_drain_after_subscriber_alerts(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    # The notices are older than the alerts, and still go after them.
    first = _uncertain_notice(a)
    second = _uncertain_notice(b)
    alert_a = _queue(a)
    alert_b = _queue(b)
    for token in (TOKEN_A, TOKEN_B, OPS_BOT_TOKEN):
        fake_telegram.accept(token)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    assert _bots(fake_telegram) == ["A", "B", "ops"]
    assert [_row(r).status for r in (alert_a, alert_b, first, second)] == [
        "sent",
        "sent",
        "sent",
        "pending",
    ]
    assert fake_telegram.sent[:2] == [_body(OFF_EN, DEFAULT_CHAT_ID), _body(OFF_EN, CHAT_B)]
    assert fake_telegram.sent[2]["chat_id"] == OPS_CHAT_ID

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert _bots(fake_telegram) == ["A", "B", "ops", "ops"]
    assert _row(second).status == "sent"
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(2)), state) is False


@pytest.mark.django_db(transaction=True)
def test_ops_bot_backoff_is_its_own(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    notice = _uncertain_notice(location)
    fake_telegram.fail(OPS_BOT_TOKEN, status=429, json_body=_too_many_requests(30))
    fake_telegram.accept(OPS_BOT_TOKEN)
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    # Only the ops bot waits; the location's bot has no backoff at all.
    assert state.not_before == {io_loop.ops_key(OPS_BOT_TOKEN): T0 + _seconds(30)}
    waiting = _row(notice)
    assert (waiting.status, waiting.last_error) == ("pending", "429")
    assert waiting.next_attempt_at == T0 + _seconds(30)

    # A subscriber alert queued while the ops bot waits goes out in the next pass.
    alert = _queue(location, at=T0 + _seconds(1))
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert (_row(alert).status, _row(alert).sent_at) == ("sent", T0 + _seconds(1))
    for seconds in (2, 15, 29):
        assert io_loop.run_iteration(FakeClock(T0 + _seconds(seconds)), state) is False
    assert _bots(fake_telegram) == ["ops", "A"]

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(30)), state) is True
    assert _row(notice).status == "sent"
    assert _bots(fake_telegram) == ["ops", "A", "ops"]


@pytest.mark.django_db(transaction=True)
def test_a_bot_shared_by_a_location_and_the_ops_chat_backs_off_once(
    location_factory: Callable[..., Any], fake_telegram: Any, settings: Any
) -> None:
    # Telegram limits each bot, not each chat: when the admin reuses a location's bot for the
    # ops chat, that location's 429 also holds back a due ops notice, and nothing else.
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token=TOKEN_A, ops_chat_id=OPS_CHAT_ID)
    with transaction.atomic():
        notice = outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload=_gap_payload(), recorded_at=T0)
    alert = _queue(location_factory(bot_token=TOKEN_A))
    fake_telegram.fail(TOKEN_A, status=429, json_body=_too_many_requests(30))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    assert len(fake_telegram.calls) == 1
    assert (_row(notice).status, _row(notice).attempts) == ("pending", 0)
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(29)), state) is False

    assert io_loop.run_iteration(FakeClock(T0 + _seconds(30)), state) is True
    assert (_row(alert).status, _row(notice).status) == ("sent", "sent")
    assert [body["chat_id"] for body in fake_telegram.sent] == [DEFAULT_CHAT_ID, OPS_CHAT_ID]


@pytest.mark.django_db(transaction=True)
def test_an_ops_row_that_cannot_be_claimed_is_not_sent(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = _uncertain_notice(location_factory())
    fake_telegram.accept(OPS_BOT_TOKEN)
    monkeypatch.setattr(outbox, "claim", lambda message_id: False)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is False

    assert len(fake_telegram.calls) == 0
    assert (_row(notice).status, _row(notice).attempts) == ("pending", 0)


@pytest.mark.django_db(transaction=True)
def test_no_ops_request_when_the_ops_chat_is_not_configured(
    location_factory: Callable[..., Any], fake_telegram: Any, no_ops_chat: Any
) -> None:
    # A row queued while the chat was configured stays queued: no request can carry an
    # empty or missing token (OPS-08).
    notice = _uncertain_notice(location_factory())
    fake_telegram.accept(OPS_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is False

    assert len(fake_telegram.calls) == 0
    assert (_row(notice).status, _row(notice).attempts) == ("pending", 0)


@pytest.mark.django_db(transaction=True)
def test_a_stop_request_skips_the_ops_queue(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    notice = _uncertain_notice(location_factory())
    fake_telegram.accept(OPS_BOT_TOKEN)
    stop = threading.Event()
    stop.set()

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState(), stop) is False

    assert len(fake_telegram.calls) == 0
    assert _row(notice).status == "pending"


@pytest.mark.django_db(transaction=True)
def test_an_ops_notice_that_cannot_be_rendered_is_dropped_alone(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    with transaction.atomic():
        # The referenced subscriber row does not exist.
        broken = outbox.enqueue_ops(
            outbox.KIND_OPS_UNCERTAIN, payload={"message_id": 10**9}, recorded_at=T0
        )
    alert = _queue(location)
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    row = _row(broken)
    assert (row.status, row.attempts, row.last_error) == ("dropped", 0, "render_error")
    assert row.sent_at is None
    assert _row(alert).status == "sent"
    assert _bots(fake_telegram) == ["A"]
    # The ops bot itself is fine, so it is not backed off.
    assert state.not_before == {}
    lines = [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER]
    assert lines == [
        f"relay: ops notice {broken.pk} (ops_uncertain) cannot be rendered; it is dropped"
    ]


@pytest.mark.django_db(transaction=True)
def test_an_unexpected_ops_error_is_logged_by_class_only(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    notice = _uncertain_notice(location_factory())
    fake_telegram.accept(OPS_BOT_TOKEN)

    def claim(message_id: int) -> bool:
        raise RuntimeError(f"database exploded near /bot{OPS_BOT_TOKEN}/")

    monkeypatch.setattr(outbox, "claim", claim)
    caplog.set_level(logging.DEBUG)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is False

    assert _row(notice).status == "pending"
    lines = [r for r in caplog.records if r.name == RELAY_LOGGER]
    assert [r.getMessage() for r in lines] == ["relay failed for the ops chat: RuntimeError"]
    assert lines[0].exc_info is None
    assert OPS_BOT_TOKEN not in caplog.text
    assert len(fake_telegram.calls) == 0


# Chart pin notices (CHRT-05, INV-17 #1, D-07)


@pytest.mark.django_db(transaction=True)
def test_pin_failed_notice_reaches_the_ops_chat(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_PIN_FAILED,
            payload={"http_status": 400},
            recorded_at=T0,
            location_id=location.pk,
        )

    [notice] = _ops_rows()
    assert (notice.kind, notice.payload, notice.location_id, notice.status) == (
        "ops_pin_failed",
        {"http_status": 400},
        location.pk,
        "pending",
    )
    fake_telegram.accept(OPS_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is True

    # One message, with the ops bot, to the env-configured ops chat only.
    assert _bots(fake_telegram) == ["ops"]
    assert fake_telegram.sent == [_body(PIN_FAILED, OPS_CHAT_ID)]
    assert (_row(notice).status, _row(notice).sent_at) == ("sent", T0)


@pytest.mark.django_db(transaction=True)
def test_chart_failing_notice_reaches_the_ops_chat(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_CHART_FAILING,
            payload={"http_status": 403},
            recorded_at=T0,
            location_id=location.pk,
        )

    [notice] = _ops_rows()
    assert (notice.kind, notice.payload, notice.location_id, notice.status) == (
        "ops_chart_failing",
        {"http_status": 403},
        location.pk,
        "pending",
    )
    fake_telegram.accept(OPS_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is True

    # One message, with the ops bot, to the env-configured ops chat only.
    assert _bots(fake_telegram) == ["ops"]
    assert fake_telegram.sent == [_body(CHART_FAILING, OPS_CHAT_ID)]
    assert (_row(notice).status, _row(notice).sent_at) == ("sent", T0)


@pytest.mark.django_db
def test_enqueue_ops_refuses_a_pin_status_that_is_not_an_integer(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    payload: dict[str, Any] = {"http_status": "400"}

    with pytest.raises(TypeError, match="must be an integer"):
        outbox.enqueue_ops(
            outbox.KIND_OPS_PIN_FAILED, payload=payload, recorded_at=T0, location_id=location.pk
        )

    assert not OutboxMessage.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_pin_restored_notice_reaches_the_ops_chat(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_PIN_RESTORED, payload={}, recorded_at=T0, location_id=location.pk
        )
    # A subscriber alert queued after the notice still goes first (INV-20).
    alert = _queue(location)
    [notice] = _ops_rows()
    assert (notice.kind, notice.payload, notice.location_id) == (
        "ops_pin_restored",
        {},
        location.pk,
    )
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(OPS_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(T0), io_loop.RelayState()) is True

    assert _bots(fake_telegram) == ["A", "ops"]
    assert fake_telegram.sent == [
        _body(OFF_EN, DEFAULT_CHAT_ID),
        _body(PIN_RESTORED, OPS_CHAT_ID),
    ]
    assert (_row(alert).status, _row(notice).status, _row(notice).sent_at) == ("sent", "sent", T0)


@pytest.mark.django_db
def test_pin_notice_is_logged_when_the_ops_chat_is_not_configured(
    location_factory: Callable[..., Any], no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    markup = location_factory(name="<b>A&B</b>", chat_id=CHAT_B)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_PIN_FAILED,
            payload={"http_status": 400},
            recorded_at=T0,
            location_id=location.pk,
        )
        ops.notify(outbox.KIND_OPS_PIN_RESTORED, payload={}, recorded_at=T0, location_id=markup.pk)

    # No row is written; each notice is one WARNING with the plain (unescaped) text (D-09).
    assert not OutboxMessage.objects.exists()
    records = [r for r in caplog.records if r.name == OPS_LOGGER]
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert [r.getMessage() for r in records] == [
        f"ops notice (ops chat not configured): {PIN_FAILED}",
        "ops notice (ops chat not configured): 📌 Pinning works again for <b>A&B</b>.",
    ]


@pytest.mark.django_db(transaction=True)
def test_unrenderable_pin_notice_is_dropped_not_stuck(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    with transaction.atomic():
        # An integer, so the payload check passes, but not an HTTP status: render raises.
        broken = outbox.enqueue_ops(
            outbox.KIND_OPS_PIN_FAILED,
            payload={"http_status": 1000},
            recorded_at=T0,
            location_id=location.pk,
        )
        later = outbox.enqueue_ops(
            outbox.KIND_OPS_PIN_RESTORED, payload={}, recorded_at=T0, location_id=location.pk
        )
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)

    assert io_loop.run_iteration(FakeClock(T0), state) is False

    row = _row(broken)
    assert (row.status, row.attempts, row.last_error) == ("dropped", 0, "render_error")
    assert len(fake_telegram.calls) == 0
    assert state.not_before == {}
    assert [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER] == [
        f"relay: ops notice {broken.pk} (ops_pin_failed) cannot be rendered; it is dropped"
    ]

    # The ops queue is not held: the next notice goes out in the next pass.
    assert io_loop.run_iteration(FakeClock(T0 + _seconds(1)), state) is True
    assert fake_telegram.sent == [_body(PIN_RESTORED, OPS_CHAT_ID)]
    assert _row(later).status == "sent"


# The outbox functions for the ops channel


@pytest.mark.django_db
def test_enqueue_ops_queues_a_due_ops_row() -> None:
    with transaction.atomic():
        row = outbox.enqueue_ops(
            outbox.KIND_OPS_GAP, payload={"start_us": 1, "end_us": 2}, recorded_at=T0
        )

    stored = _row(row)
    assert (stored.channel, stored.location_id, stored.kind, stored.status) == (
        "ops",
        None,
        "ops_gap",
        "pending",
    )
    assert (stored.event_at, stored.recorded_at, stored.next_attempt_at) == (T0, T0, T0)
    assert stored.expires_at == T0 + timedelta(hours=6)
    assert stored.payload == {"start_us": 1, "end_us": 2}
    assert outbox.CHANNEL_OPS == "ops"
    assert set(outbox.OPS_KINDS) == {
        "ops_gap",
        "ops_all_silent_start",
        "ops_all_silent_end",
        "ops_expired",
        "ops_uncertain",
        "ops_pin_failed",
        "ops_pin_restored",
        "ops_delivery_failing",
        "ops_delivery_restored",
        "ops_chart_failing",
        "ops_chart_restored",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["power_off", "ops_nope", ""])
def test_enqueue_ops_refuses_an_unknown_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="unknown ops notice kind"):
        outbox.enqueue_ops(kind, payload={"message_id": 1}, recorded_at=T0)

    assert not OutboxMessage.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [{"message_id": "1"}, {"message_id": True}, {"message_id": 1.0}, {"message_id": None}],
    ids=repr,
)
def test_enqueue_ops_refuses_a_payload_value_that_is_not_an_integer(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        outbox.enqueue_ops(outbox.KIND_OPS_UNCERTAIN, payload=payload, recorded_at=T0)

    assert not OutboxMessage.objects.exists()


@pytest.mark.django_db
def test_ops_head_is_the_oldest_open_ops_row(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    assert outbox.ops_head() is None
    _queue(location)
    with transaction.atomic():
        older = outbox.enqueue_ops(
            outbox.KIND_OPS_GAP, payload={"start_us": 1, "end_us": 2}, recorded_at=T0
        )
        newer = outbox.enqueue_ops(
            outbox.KIND_OPS_GAP, payload={"start_us": 3, "end_us": 4}, recorded_at=T0
        )
    _queue(location)

    head = outbox.ops_head()
    assert head is not None and head.pk == older.pk
    # A sending row still holds the line; a finished one does not.
    OutboxMessage.objects.filter(pk=older.pk).update(status="sending")
    assert outbox.ops_head().pk == older.pk  # type: ignore[union-attr]
    OutboxMessage.objects.filter(pk=older.pk).update(status="sent")
    assert outbox.ops_head().pk == newer.pk  # type: ignore[union-attr]
    OutboxMessage.objects.filter(pk=newer.pk).update(status="uncertain")
    assert outbox.ops_head() is None


# Rendering and instants


def test_instant_us_round_trips_an_aware_instant() -> None:
    at = datetime(2026, 10, 1, 7, 0, 12, 345678, tzinfo=UTC)

    us = ops.instant_us(at)

    assert us == 1_790_838_012_345_678
    assert ops.from_instant_us(us) == at
    assert ops.from_instant_us(0) == ops.EPOCH
    assert ops.from_instant_us(us).utcoffset() == timedelta(0)


def test_instant_us_refuses_a_naive_datetime_and_from_instant_us_a_non_integer() -> None:
    with pytest.raises(ValueError, match="naive"):
        ops.instant_us(datetime(2026, 10, 1, 7, 0))  # noqa: DTZ001
    for value in ("1", 1.5, True, None):
        with pytest.raises(TypeError):
            ops.from_instant_us(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ops.from_instant_us(10**20)


@pytest.mark.django_db
def test_render_text_refuses_an_unknown_kind_and_a_missing_row() -> None:
    with pytest.raises(ValueError, match="unknown ops notice kind"):
        ops.render_text("power_off", {"message_id": 1}, None, now=T0)
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_UNCERTAIN, {"message_id": 10**9}, None, now=T0)
    with pytest.raises(KeyError):
        ops.render_text(outbox.KIND_OPS_UNCERTAIN, {}, None, now=T0)
    with pytest.raises(TypeError):
        ops.render_text(outbox.KIND_OPS_UNCERTAIN, [1], None, now=T0)


# No ops chat: notices go to the log (D-09, INV-20)


@pytest.mark.django_db
def test_ops_notice_is_logged_when_the_ops_chat_is_not_configured(
    no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_GAP,
            payload=_gap_payload(),
            recorded_at=datetime(2026, 10, 1, 7, 10, 41, tzinfo=UTC),
        )

    assert not OutboxMessage.objects.exists()
    records = [r for r in caplog.records if r.name == OPS_LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        f"ops notice (ops chat not configured): {GAP_SAMPLE}. "
        "Recorded as not monitored; no subscriber alerts were sent for it."
    )


@pytest.mark.django_db
def test_notify_queues_the_notice_when_the_ops_chat_is_configured(
    ops_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=OPS_LOGGER)

    with transaction.atomic():
        ops.notify(outbox.KIND_OPS_GAP, payload=_gap_payload(), recorded_at=T0)

    [notice] = _ops_rows()
    assert (notice.kind, notice.payload, notice.status) == ("ops_gap", _gap_payload(), "pending")
    assert _ops_lines(caplog) == []


@pytest.mark.django_db
@pytest.mark.parametrize("configured", [True, False], ids=["configured", "not-configured"])
def test_notify_refuses_a_bad_kind_or_payload_in_both_modes(
    settings: Any, configured: bool
) -> None:
    token, chat = (OPS_BOT_TOKEN, OPS_CHAT_ID) if configured else ("", None)
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token=token, ops_chat_id=chat)
    bad_payload: dict[str, Any] = {"start_us": "x"}

    with pytest.raises(ValueError, match="unknown ops notice kind"):
        ops.notify("power_off", payload={"message_id": 1}, recorded_at=T0)
    with pytest.raises(TypeError, match="must be an integer"):
        ops.notify(outbox.KIND_OPS_GAP, payload=bad_payload, recorded_at=T0)

    assert not OutboxMessage.objects.exists()


@pytest.mark.django_db
def test_an_unrenderable_logged_notice_never_aborts_the_caller(
    location_factory: Callable[..., Any], no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    with transaction.atomic():
        # The referenced alert does not exist: the notice cannot be rendered.
        ops.notify(outbox.KIND_OPS_UNCERTAIN, payload={"message_id": 10**9}, recorded_at=T0)
        # The caller's transaction is still usable and commits.
        alert = outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=T0,
            recorded_at=T0,
            payload={"was_on_us": 1},
        )

    assert OutboxMessage.objects.filter(pk=alert.pk).exists()
    assert _ops_lines(caplog) == [
        "ops notice ops_uncertain (ops chat not configured) could not be rendered: LookupError"
    ]


@pytest.mark.django_db(transaction=True)
def test_unconfigured_notice_logs_the_name_unescaped(
    location_factory: Callable[..., Any], no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    off = _queue(location_factory(name="<b>A&B</b>"))
    assert outbox.claim(off.pk) is True
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    assert ops.mark_uncertain(off.pk, "read_timeout", T0) is True

    assert _ops_rows() == []
    [line] = _ops_lines(caplog)
    # A log line is plain text, not Telegram HTML.
    assert "OFF alert for <b>A&B</b> (event 13:05) may not have been delivered" in line
    assert "&lt;" not in line and "&amp;" not in line


# Sends interrupted by a stopped worker (D-11 #5, D-13, INV-15)


@pytest.mark.django_db(transaction=True)
def test_recover_interrupted_notifies_once_per_subscriber_row(
    location_factory: Callable[..., Any], ops_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    alert = _queue(location)
    notice = _uncertain_notice(location)
    # Both were claimed when the previous worker stopped.
    OutboxMessage.objects.filter(pk__in=[alert.pk, notice.pk]).update(status="sending", attempts=1)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    assert ops.recover_interrupted(T0) == 2

    for row in (alert, notice):
        assert (_row(row).status, _row(row).last_error) == ("uncertain", "interrupted")
    new = [r for r in _ops_rows() if r.pk != notice.pk]
    assert [(r.kind, r.payload, r.location_id, r.recorded_at) for r in new] == [
        ("ops_uncertain", {"message_id": alert.pk}, location.pk, T0)
    ]
    text = ops.render_text(new[0].kind, new[0].payload, new[0].location_id, now=T0)
    assert "OFF alert for Test location (event 13:05)" in text
    assert "(the worker stopped while sending it)" in text
    # The interrupted ops row is only logged: no notice about a notice.
    assert _ops_lines(caplog) == [
        f"ops notice {notice.pk} was interrupted while sending; it is not resent"
    ]

    assert ops.recover_interrupted(T0 + _seconds(1)) == 0
    assert len(_ops_rows()) == 2


@pytest.mark.django_db(transaction=True)
def test_io_activation_turns_interrupted_sends_into_one_notice(
    location_factory: Callable[..., Any], no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # The I/O thread runs this once per lease generation (02-05).
    off = _queue(location_factory())
    OutboxMessage.objects.filter(pk=off.pk).update(status="sending", attempts=1)
    caplog.set_level(logging.INFO)

    assert io_loop.activate(io_loop.RelayState(), FakeClock(T0 + timedelta(hours=1))) == 1

    assert (_row(off).status, _row(off).last_error) == ("uncertain", "interrupted")
    assert _ops_rows() == []
    [line] = _ops_lines(caplog)
    assert line.startswith("ops notice (ops chat not configured): ❓ OFF alert for Test location")
    assert "(the worker stopped while sending it)" in line


# A database error is never hidden from the caller (wave 2 audit B3)

# The connectivity errors the worker loops count as a DB outage, and their base class.
DB_ERRORS = [OperationalError, InterfaceError, DatabaseError]


def _drop_this_session() -> None:
    """End this thread's DB session from another one, as a DB restart would.

    Called inside a transaction: the next statement on this connection fails, and so does
    the rollback to any savepoint.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        row = cur.fetchone()
    assert row is not None
    pid = row[0]

    def terminate() -> bool:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
            found = cur.fetchone()
        return bool(found and found[0])

    killer = Actor(terminate)
    killer.start()
    killer.join(10)
    assert killer.exc is None
    assert killer.result is True


def _claimed(location: Any) -> OutboxMessage:
    """An OFF alert claimed for sending, as the relay leaves it before the HTTP call."""
    off = _queue(location)
    assert outbox.claim(off.pk) is True
    return off


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("error", DB_ERRORS, ids=lambda error: error.__name__)
def test_B3_a_db_error_while_rendering_a_logged_notice_propagates(
    location_factory: Callable[..., Any],
    no_ops_chat: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: type[DatabaseError],
) -> None:
    location = location_factory()
    off = _claimed(location)

    def render(*args: Any, **kwargs: Any) -> str:
        raise error("the database went away while the notice was rendered")

    monkeypatch.setattr(ops, "render_text", render)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    with pytest.raises(error), transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_UNCERTAIN,
            payload={"message_id": off.pk},
            recorded_at=T0,
            location_id=location.pk,
        )
    with pytest.raises(error):
        ops.mark_uncertain(off.pk, "read_timeout", T0)

    # Not reported as marked: the row still holds its location's queue, for the relay's
    # error handling or activation to settle, and nothing was logged as if handled.
    assert (_row(off).status, _row(off).last_error) == ("sending", "")
    assert _ops_rows() == []
    assert _ops_lines(caplog) == []


@pytest.mark.django_db
def test_B3_a_malformed_payload_still_logs_one_warning_and_does_not_raise(
    location_factory: Callable[..., Any], no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    with transaction.atomic():
        # Integers, so the payload check passes, but far outside datetime's range: the
        # render raises ValueError.
        ops.notify(
            outbox.KIND_OPS_GAP, payload={"start_us": 10**20, "end_us": 10**20}, recorded_at=T0
        )
        # The caller's transaction is still usable and commits.
        alert = outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=T0,
            recorded_at=T0,
            payload={"was_on_us": 1},
        )

    assert OutboxMessage.objects.filter(pk=alert.pk).exists()
    assert _ops_lines(caplog) == [
        "ops notice ops_gap (ops chat not configured) could not be rendered: ValueError"
    ]


@pytest.mark.django_db(transaction=True)
def test_B3_a_non_db_render_error_keeps_mark_uncertain_committed(
    location_factory: Callable[..., Any],
    no_ops_chat: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    off = _claimed(location_factory())

    def render(*args: Any, **kwargs: Any) -> str:
        raise ValueError("malformed payload")

    monkeypatch.setattr(ops, "render_text", render)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    assert ops.mark_uncertain(off.pk, "read_timeout", T0) is True

    assert (_row(off).status, _row(off).last_error) == ("uncertain", "read_timeout")
    assert _ops_lines(caplog) == [
        "ops notice ops_uncertain (ops chat not configured) could not be rendered: ValueError"
    ]


@pytest.mark.django_db(transaction=True)
def test_B3_a_dropped_connection_inside_render_fails_mark_uncertain(
    location_factory: Callable[..., Any],
    no_ops_chat: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    off = _claimed(location_factory())
    real_render = ops.render_text

    def render(*args: Any, **kwargs: Any) -> str:
        _drop_this_session()
        return real_render(*args, **kwargs)

    monkeypatch.setattr(ops, "render_text", render)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    try:
        # The relay's _apply and its DatabaseError handling see the outage.
        with pytest.raises((OperationalError, InterfaceError)):
            ops.mark_uncertain(off.pk, "read_timeout", T0)
    finally:
        # Never leave this thread's dead session to the teardown flush.
        connection.close()

    assert (_row(off).status, _row(off).last_error) == ("sending", "")
    assert _ops_lines(caplog) == []


@pytest.mark.django_db(transaction=True)
def test_B3_a_dropped_connection_inside_render_fails_activation_so_it_is_retried(
    location_factory: Callable[..., Any],
    no_ops_chat: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    off = _queue(location_factory())
    OutboxMessage.objects.filter(pk=off.pk).update(status="sending", attempts=1)
    real_render = ops.render_text
    dropped: list[bool] = []

    def render(*args: Any, **kwargs: Any) -> str:
        if not dropped:
            dropped.append(True)
            _drop_this_session()
        return real_render(*args, **kwargs)

    monkeypatch.setattr(ops, "render_text", render)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)
    state = io_loop.RelayState()
    clock = FakeClock(T0 + timedelta(hours=1))

    try:
        # The I/O thread keeps this generation unactivated and calls activate again.
        with pytest.raises((OperationalError, InterfaceError)):
            io_loop.activate(state, clock)
        assert _ops_lines(caplog) == []
        assert io_loop.activate(state, clock) == 1
    finally:
        connection.close()

    assert (_row(off).status, _row(off).last_error) == ("uncertain", "interrupted")
    # Exactly one notice, from the activation that committed.
    [line] = _ops_lines(caplog)
    assert line.startswith("ops notice (ops chat not configured): ❓ OFF alert for Test location")


@pytest.mark.django_db(transaction=True)
def test_B3_a_render_error_after_the_connection_dropped_is_not_swallowed(
    location_factory: Callable[..., Any],
    no_ops_chat: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The savepoint cannot be rolled back on a dead connection, so Django marks the caller's
    # whole transaction for rollback: swallowing the error would commit nothing silently.
    off = _claimed(location_factory())

    def render(*args: Any, **kwargs: Any) -> str:
        _drop_this_session()
        raise ValueError("malformed payload")

    monkeypatch.setattr(ops, "render_text", render)
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    try:
        with pytest.raises(DatabaseError):
            ops.mark_uncertain(off.pk, "read_timeout", T0)
    finally:
        connection.close()

    assert (_row(off).status, _row(off).last_error) == ("sending", "")
    assert _ops_lines(caplog) == []


# A broken admin chat never delays subscribers (INV-20 #2, ALRT-06)


@pytest.mark.django_db(transaction=True)
def test_INV20_broken_admin_chat_never_delays_subscribers(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    with transaction.atomic():
        notice = outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload=_gap_payload(), recorded_at=T0)
    # The admin chat refuses connections twice, then the ops bot is kicked (403).
    fake_telegram.fail(OPS_BOT_TOKEN, exc=_refused(OPS_BOT_TOKEN))
    fake_telegram.fail(OPS_BOT_TOKEN, exc=_refused(OPS_BOT_TOKEN))
    kicked = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}
    fake_telegram.fail(OPS_BOT_TOKEN, status=403, json_body=kicked)
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(TOKEN_B)
    state = io_loop.RelayState()
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)
    alerts: list[OutboxMessage] = []
    waits: list[timedelta] = []

    for second in range(21):
        clock = FakeClock(T0 + _seconds(second))
        if second % 3 == 0:
            # A new subscriber alert every 3 s, alternating between the two locations.
            alerts.append(_queue(a if second % 6 == 0 else b, at=clock.now()))
        attempts = _row(notice).attempts

        io_loop.run_iteration(clock, state)

        # Every alert went out in the first pass after it was due.
        assert [(_row(r).status, _row(r).sent_at) for r in alerts] == [
            ("sent", r.recorded_at) for r in alerts
        ]
        row = _row(notice)
        assert row.status == "pending"
        if row.attempts != attempts:
            waits.append(row.next_attempt_at - clock.now())

    # The ops row backs off on its own: 2 s, 4 s, then 15 min after the 403.
    assert waits == [_seconds(2), _seconds(4), timedelta(minutes=15)]
    assert _row(notice).next_attempt_at == T0 + _seconds(6) + timedelta(minutes=15)
    assert set(state.not_before) == {io_loop.ops_key(OPS_BOT_TOKEN)}
    assert _bots(fake_telegram).count("ops") == 3
    assert len(fake_telegram.sent) == len(alerts) == 7
    assert [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER] == [
        "relay: permanent error http_403 for the ops chat; the ops bot backs off for 15 min"
    ]


# No hard-coded destination anywhere in the code (INV-20, D-09)

TOKEN_SHAPE = re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{30,}")
CHAT_ID_SHAPE = re.compile(r"-100[0-9]{7,}")


def _destination_literals(source: str, filename: str) -> list[str]:
    """Literals that could be a Telegram destination: a bot token or a channel ID.

    A string counts only if it is the whole value, so UI copy such as "like
    -1001234567890." inside a longer sentence is allowed.
    """
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if TOKEN_SHAPE.fullmatch(node.value) or CHAT_ID_SHAPE.fullmatch(node.value):
                found.append(f"{filename}:{node.lineno}")
        elif (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) is int
            and CHAT_ID_SHAPE.fullmatch(f"-{node.operand.value}")
        ):
            found.append(f"{filename}:{node.lineno}")
    return found


def test_INV20_no_hard_coded_chat_in_the_code() -> None:
    root = pathlib.Path(powermon.__file__).parent
    sources = sorted(root.rglob("*.py"))
    # The whole package, migrations included.
    assert len(sources) > 30
    assert any(path.parent.name == "migrations" for path in sources)

    offenders = [
        hit
        for path in sources
        for hit in _destination_literals(
            path.read_text(encoding="utf-8"), str(path.relative_to(root))
        )
    ]

    assert offenders == []


def test_the_hard_coded_chat_scan_catches_planted_literals() -> None:
    # The scan must be able to fail (Pitfall 13).
    token = "123456789:" + "A" * 35
    planted = (
        f'TOKEN = "{token}"\n'
        "CHAT = -1001234567890\n"
        'CHAT_TEXT = "-1001234567890"\n'
        'HELP = "like -1001234567890."\n'
        "POSITIVE = 1001234567890\n"
        "SHORT = -100123\n"
    )

    assert _destination_literals(planted, "planted.py") == [
        "planted.py:1",
        "planted.py:2",
        "planted.py:3",
    ]


# An incident's details hold integers only (OPS-08, Phase 4 D-10)


@pytest.mark.django_db
def test_open_incident_stores_integer_details(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    details = {"http_status": 400, "migrate_to_chat_id": -1009999999999}

    with transaction.atomic():
        with_details = ops.open_incident(
            "delivery_failing", T0, location_id=location.pk, details=details
        )
        without = ops.open_incident("all_silent", T0)

    assert with_details is not None and without is not None
    assert OpsIncident.objects.get(pk=with_details).details == details
    assert OpsIncident.objects.get(pk=without).details == {}
    # A second open incident of the kind and location: no id, the first one's details stay.
    assert ops.open_incident("delivery_failing", T0, location_id=location.pk, details={}) is None
    assert OpsIncident.objects.get(pk=with_details).details == details


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["400", True, 400.0, None, [400]], ids=repr)
def test_open_incident_refuses_a_detail_that_is_not_an_integer(
    location_factory: Callable[..., Any], value: Any
) -> None:
    location = location_factory()

    with pytest.raises(TypeError, match="must be an integer"):
        ops.open_incident(
            "delivery_failing", T0, location_id=location.pk, details={"http_status": value}
        )

    assert not OpsIncident.objects.exists()
