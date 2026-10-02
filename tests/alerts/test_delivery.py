"""Delivery health of a location's subscriber alerts (OPS-03, D-10, D-12).

- D-10: the first permanent refusal (400/401/403/404) of a subscriber alert send opens the
  location's ``delivery_failing`` incident and queues one ``ops_delivery_failing`` notice,
  in the transaction that writes the row's outcome. The next successful subscriber send
  closes it with one ``ops_delivery_restored`` notice. Ops rows never open it.
- INV-16 #1 (relay part, docs/v1-lessons.md): after a 403 the alert gets exactly one
  attempt in the next 15 minutes, and the admin gets one notice.
- INV-20 #1 / D-12 cases are added with the recorded test-message success and the
  worker's lift of its in-memory hold.

The relay runs ``close_old_connections()``, so every test that runs ``run_iteration`` is
``django_db(transaction=True)``. Time comes only from the ``FakeClock`` passed in;
Telegram is faked at the HTTP boundary (``fake_telegram``), and ``ops_settings``
configures the admin chat, whose bot is accepted wherever ops rows must be delivered.
"""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, OPS_BOT_TOKEN, OPS_CHAT_ID, FakeClock
from django.db import transaction

from powermon.alerts import delivery, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.worker import io_loop

TOKEN_A = DEFAULT_BOT_TOKEN
# The OFF alert is recorded (and so due) at 10:06:31 UTC, 13:06:31 in Kyiv.
T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
# An admin-typed name with HTML in it: every notice escapes it (Telegram HTML).
NAME = "Office <1> & Co"
ESCAPED = "Office &lt;1&gt; &amp; Co"
KICKED = {
    "ok": False,
    "error_code": 403,
    "description": "Forbidden: bot was kicked from the channel chat",
}
FAILING_403 = (
    f"🚫 Alerts for {ESCAPED} are failing (Telegram: http_403). They stay queued and are "
    "retried every 15 min until they expire after 6h. Check that the bot is an admin of the "
    "channel, then send a test message from the admin panel."
)
RESTORED = f"✅ Alerts for {ESCAPED} are delivered again."
# The OFF alert sent 15 min after it was recorded states its event time (ALRT-04): the
# outage started 91 s before the OFF was recorded, at 13:05 Kyiv time.
LATE_OFF = "🔴 13:05 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    """Event times in the expected texts are Kyiv times, whatever the env says."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    return settings


def _min(n: float) -> timedelta:
    return timedelta(minutes=n)


def _queue(location: Any, kind: str = outbox.KIND_POWER_OFF, at: datetime = T0) -> OutboxMessage:
    """One alert recorded (and so due) at ``at``, as a transition would queue it."""
    if kind == outbox.KIND_POWER_OFF:
        payload = {"was_on_us": 300_000_000}
    else:
        payload = {"was_off_us": 3_300_000_000}
    with transaction.atomic():
        return outbox.enqueue(
            kind,
            location.pk,
            event_at=at - timedelta(seconds=91),
            recorded_at=at,
            payload=payload,
        )


def _row(message: OutboxMessage) -> OutboxMessage:
    return OutboxMessage.objects.get(pk=message.pk)


def _body(text: str, chat_id: int = DEFAULT_CHAT_ID) -> dict[str, Any]:
    return {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}


def _calls_to(fake: Any, token: str) -> int:
    return len([call for call in fake.calls if f"/bot{token}/" in call.request.url])


def _ops_rows(kind: str) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS, kind=kind)
    return list(rows.order_by("id"))


def _incidents(location: Any) -> list[tuple[datetime, datetime | None, Any]]:
    rows = OpsIncident.objects.filter(kind=delivery.KIND_DELIVERY_FAILING, location=location)
    return [(row.started_at, row.ended_at, row.details) for row in rows.order_by("id")]


def _refused_once(location_factory: Callable[..., Any], fake_telegram: Any) -> tuple[Any, Any]:
    """INV-16 #1 setup: an OFF alert queued at T0 that the location's bot gets a 403 for."""
    location = location_factory(name=NAME)
    off = _queue(location)
    fake_telegram.fail(TOKEN_A, status=403, json_body=KICKED)
    fake_telegram.accept(OPS_BOT_TOKEN)
    return location, off


# INV-16 #1 (relay part), D-10: one attempt, one incident, one notice


@pytest.mark.django_db(transaction=True)
def test_INV16_1_403_opens_failing_once_and_notifies_the_admin(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location, off = _refused_once(location_factory, fake_telegram)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    # One send to the location's bot; the row waits 15 minutes.
    assert _calls_to(fake_telegram, TOKEN_A) == 1
    row = _row(off)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "http_403")
    assert row.next_attempt_at == T0 + _min(15)
    # The incident is open with the status only (integers, never Telegram's text).
    assert _incidents(location) == [(T0, None, {"http_status": 403})]
    # One notice, delivered to the admin chat in the same pass, after the heads.
    [notice] = _ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)
    assert (notice.payload, notice.location_id, notice.recorded_at, notice.status) == (
        {"http_status": 403},
        location.pk,
        T0,
        "sent",
    )
    assert fake_telegram.sent == [_body(FAILING_403, OPS_CHAT_ID)]
    channel = io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID)
    assert state.failing == {location.pk: channel}
    assert state.not_before == {channel: T0 + _min(15)}

    # Exactly one attempt in the next 15 minutes, and still one notice.
    for minutes in (1, 5, 14):
        assert io_loop.run_iteration(FakeClock(T0 + _min(minutes)), state) is False
    assert _calls_to(fake_telegram, TOKEN_A) == 1
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)) == 1
    assert _incidents(location) == [(T0, None, {"http_status": 403})]


@pytest.mark.django_db(transaction=True)
def test_next_successful_send_closes_failing_with_one_notice(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location, off = _refused_once(location_factory, fake_telegram)
    state = io_loop.RelayState()
    assert io_loop.run_iteration(FakeClock(T0), state) is True
    # The bot is back in the channel: the next sendMessage is accepted.
    fake_telegram.accept(TOKEN_A)
    restored_at = T0 + _min(15)

    assert io_loop.run_iteration(FakeClock(restored_at), state) is True

    assert _row(off).status == "sent"
    assert _incidents(location) == [(T0, restored_at, {"http_status": 403})]
    [restored] = _ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)
    assert (restored.payload, restored.location_id, restored.recorded_at, restored.status) == (
        {},
        location.pk,
        restored_at,
        "sent",
    )
    # The alert goes out late, with its event time; then the admin hears it recovered.
    assert fake_telegram.sent[-2:] == [_body(LATE_OFF), _body(RESTORED, OPS_CHAT_ID)]
    assert state.failing == {}

    # Later sends find no open incident: no second recovery notice.
    on = _queue(location, outbox.KIND_POWER_ON, at=restored_at + _min(1))
    assert io_loop.run_iteration(FakeClock(restored_at + _min(1)), state) is True
    assert _row(on).status == "sent"
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)) == 1
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)) == 1


@pytest.mark.django_db(transaction=True)
def test_ops_row_permanent_never_opens_delivery_failing(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    # An ops notice about the location, refused on the admin chat.
    with transaction.atomic():
        notice = outbox.enqueue_ops(
            outbox.KIND_OPS_PIN_RESTORED, payload={}, recorded_at=T0, location_id=location.pk
        )
    fake_telegram.fail(OPS_BOT_TOKEN, status=403, json_body=KICKED)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    assert (_row(notice).status, _row(notice).last_error) == ("pending", "http_403")
    assert not OpsIncident.objects.filter(kind=delivery.KIND_DELIVERY_FAILING).exists()
    assert _ops_rows(outbox.KIND_OPS_DELIVERY_FAILING) == []
    assert state.failing == {}
