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

Relay cases call ``io_loop.run_iteration``, which calls ``close_old_connections()``, so
they are ``django_db(transaction=True)``. Time comes only from the ``FakeClock`` passed in;
Telegram is faked at the HTTP boundary (``fake_telegram``). Expected local times are
Europe/Kyiv, UTC+3 on 2026-10-01.
"""

import dataclasses
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, OPS_BOT_TOKEN, OPS_CHAT_ID, FakeClock
from django.db import transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
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
    """An uncertain subscriber row and its queued ops notice, as ``ops.mark_uncertain`` leaves them."""
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
    assert state.not_before == {io_loop.bot_key(OPS_BOT_TOKEN): T0 + _seconds(30)}
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
def test_an_ops_notice_that_cannot_be_rendered_backs_off_alone(
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
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, "render_error")
    assert row.next_attempt_at == T0 + io_loop.PERMANENT_BACKOFF
    assert _row(alert).status == "sent"
    assert _bots(fake_telegram) == ["A"]
    # The ops bot itself is fine, so it is not backed off.
    assert io_loop.bot_key(OPS_BOT_TOKEN) not in state.not_before
    lines = [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER]
    assert lines == [f"relay: cannot render ops notice {broken.pk}"]


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
    assert stored.expires_at == T0 + outbox.MAX_AGE
    assert stored.payload == {"start_us": 1, "end_us": 2}
    assert outbox.CHANNEL_OPS == "ops"
    assert set(outbox.OPS_KINDS) == {
        "ops_gap",
        "ops_all_silent_start",
        "ops_all_silent_end",
        "ops_expired",
        "ops_uncertain",
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
        ops.render_text(outbox.KIND_OPS_UNCERTAIN, [1], None, now=T0)  # type: ignore[arg-type]
