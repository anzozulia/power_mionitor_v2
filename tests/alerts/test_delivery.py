"""Delivery health of a location's subscriber alerts (OPS-03, D-10, D-12).

- D-10: the first permanent refusal (400/401/403/404) of a subscriber alert send opens the
  location's ``delivery_failing`` incident and queues one ``ops_delivery_failing`` notice,
  in the transaction that writes the row's outcome. The next successful subscriber send
  closes it with one ``ops_delivery_restored`` notice. Ops rows never open it.
- INV-16 #1 (relay part, docs/v1-lessons.md): after a 403 the alert gets exactly one
  attempt in the next 15 minutes, and the admin gets one notice. After a recorded test
  message success the badge's incident closes with one recovery notice and the queued
  alerts go out in the next pass, with their event times.
- D-12: ``delivery.record_test_success`` closes the incident and makes the location's
  pending alerts due in one transaction; the worker lifts its in-memory 15-minute hold of
  that channel in its next pass, because the incident it holds for is no longer open. A
  429's bot-wide hold is never lifted.
- INV-20 #1: an hour of 403 answers with several alerts queued gives exactly one failing
  and, after the next success, exactly one recovered notice.
- D-10: only a permanent refusal of a subscriber alert opens the incident (never a 5xx, a
  refused connection, a 429, an ambiguous send or a chart call), and a
  ``migrate_to_chat_id`` from Telegram is reported to the admin, never applied (PITFALLS
  6e).

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
import requests
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, OPS_BOT_TOKEN, OPS_CHAT_ID, FakeClock
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import delivery, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations.models import Location
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


# D-12: a recorded success releases the queue in the next pass

# The ON recorded at T0 + 1 min (event 91 s earlier, 13:06 in Kyiv) and sent 4 min later.
LATE_ON = "🟢 13:06 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>"


@pytest.mark.django_db(transaction=True)
def test_D12_recorded_test_success_releases_the_queue_in_the_next_pass(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location, off = _refused_once(location_factory, fake_telegram)
    on = _queue(location, outbox.KIND_POWER_ON, at=T0 + _min(1))
    state = io_loop.RelayState()
    assert io_loop.run_iteration(FakeClock(T0), state) is True
    channel = io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID)
    assert state.failing == {location.pk: channel}
    # The bot is back in the channel, and the admin's test message went through.
    fake_telegram.accept(TOKEN_A)
    tested_at = T0 + _min(5)

    assert delivery.record_test_success(location.pk, tested_at) is True

    # One transaction: the incident closed with one recovery notice, the alerts due now.
    assert _incidents(location) == [(T0, tested_at, {"http_status": 403})]
    [restored] = _ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)
    assert (restored.payload, restored.location_id, restored.recorded_at) == (
        {},
        location.pk,
        tested_at,
    )
    assert _row(off).next_attempt_at == tested_at
    # A row that was already due keeps its time.
    assert _row(on).next_attempt_at == T0 + _min(1)

    # The very next pass lifts the 15-minute hold and sends the OFF with its event time.
    assert io_loop.run_iteration(FakeClock(tested_at), state) is True
    assert _row(off).status == "sent"
    assert state.failing == {}
    assert channel not in state.not_before
    assert fake_telegram.sent[-2:] == [_body(LATE_OFF), _body(RESTORED, OPS_CHAT_ID)]
    # The following pass sends the ON, also with its event time; no second notice.
    assert io_loop.run_iteration(FakeClock(tested_at), state) is True
    assert _row(on).status == "sent"
    assert fake_telegram.sent[-1] == _body(LATE_ON)
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)) == 1
    assert _calls_to(fake_telegram, TOKEN_A) == 3


@pytest.mark.django_db(transaction=True)
def test_no_success_keeps_the_hold(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location, off = _refused_once(location_factory, fake_telegram)
    state = io_loop.RelayState()
    assert io_loop.run_iteration(FakeClock(T0), state) is True
    # The bot would be accepted now, but no success was recorded: the hold stays.
    fake_telegram.accept(TOKEN_A)
    channel = io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID)

    for minutes in range(1, 15):
        assert io_loop.run_iteration(FakeClock(T0 + _min(minutes)), state) is False

    assert _calls_to(fake_telegram, TOKEN_A) == 1
    assert _row(off).status == "pending"
    assert state.failing == {location.pk: channel}
    assert state.not_before == {channel: T0 + _min(15)}
    assert _incidents(location) == [(T0, None, {"http_status": 403})]


def _too_many_requests(retry_after: int) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": 429,
        "description": f"Too Many Requests: retry after {retry_after}",
        "parameters": {"retry_after": retry_after},
    }


@pytest.mark.django_db(transaction=True)
def test_lift_never_releases_a_429_hold(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    # A 403 opens the incident; the retry 15 min later gets a 429 with retry_after 600.
    fake_telegram.fail(TOKEN_A, status=403, json_body=KICKED)
    fake_telegram.fail(TOKEN_A, status=429, json_body=_too_many_requests(600))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    channel = io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID)
    bot = io_loop.bot_wide_key(TOKEN_A)
    assert io_loop.run_iteration(FakeClock(T0), state) is True
    assert io_loop.run_iteration(FakeClock(T0 + _min(15)), state) is True
    limited_until = T0 + _min(25)
    assert state.not_before == {channel: limited_until, bot: limited_until}

    # A success is recorded: the incident closes and the row is due at once.
    assert delivery.record_test_success(location.pk, T0 + _min(16)) is True
    assert _row(off).next_attempt_at == T0 + _min(16)

    # The lift drops the channel's hold, never the bot's 429: the send waits for it.
    assert io_loop.run_iteration(FakeClock(T0 + _min(16)), state) is False
    assert state.failing == {}
    assert state.not_before == {bot: limited_until}
    assert io_loop.run_iteration(FakeClock(limited_until - timedelta(seconds=1)), state) is False
    assert _calls_to(fake_telegram, TOKEN_A) == 2
    assert io_loop.run_iteration(FakeClock(limited_until), state) is True
    assert (_row(off).status, _row(off).sent_at) == ("sent", limited_until)
    assert _calls_to(fake_telegram, TOKEN_A) == 3


# INV-20 #1: an hour of refusals, one notice each way


@pytest.mark.django_db(transaction=True)
def test_INV20_1_an_hour_of_403_gives_one_failing_and_one_recovered(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location, first_off = _refused_once(location_factory, fake_telegram)
    on = _queue(location, outbox.KIND_POWER_ON, at=T0 + _min(2))
    second_off = _queue(location, outbox.KIND_POWER_OFF, at=T0 + _min(3))
    state = io_loop.RelayState()
    tried_at: list[int] = []

    # 403 for 60 minutes, one pass a minute: the head is tried every 15 minutes.
    for minute in range(61):
        before = _calls_to(fake_telegram, TOKEN_A)
        io_loop.run_iteration(FakeClock(T0 + _min(minute)), state)
        if _calls_to(fake_telegram, TOKEN_A) > before:
            tried_at.append(minute)

    assert tried_at == [0, 15, 30, 45, 60]
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)) == 1
    assert _ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED) == []
    assert _incidents(location) == [(T0, None, {"http_status": 403})]

    # The bot is re-added: the next retry (minute 75) succeeds, and the queue drains.
    fake_telegram.accept(TOKEN_A)
    for minute in range(61, 80):
        io_loop.run_iteration(FakeClock(T0 + _min(minute)), state)

    assert [_row(row).status for row in (first_off, on, second_off)] == ["sent"] * 3
    assert _row(first_off).sent_at == T0 + _min(75)
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)) == 1
    [restored] = _ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)
    assert (restored.recorded_at, restored.status) == (T0 + _min(75), "sent")
    assert _incidents(location) == [(T0, T0 + _min(75), {"http_status": 403})]


# D-10: migrate_to_chat_id is reported, never applied (PITFALLS 6e)

MIGRATED_CHAT_ID = -1009999999999
UPGRADED = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: group chat was upgraded to a supergroup chat",
    "parameters": {"migrate_to_chat_id": MIGRATED_CHAT_ID},
}


@pytest.mark.django_db(transaction=True)
def test_migrate_to_chat_id_is_reported_never_applied(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory(name=NAME)
    off = _queue(location)
    fake_telegram.fail(TOKEN_A, status=400, json_body=UPGRADED)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(T0), state) is True

    details = {"http_status": 400, "migrate_to_chat_id": MIGRATED_CHAT_ID}
    assert _incidents(location) == [(T0, None, details)]
    [notice] = _ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)
    assert (notice.payload, notice.status) == (details, "sent")
    text = fake_telegram.sent[-1]["text"]
    assert text.startswith(f"🚫 Alerts for {ESCAPED} are failing (Telegram: http_400). ")
    assert text.endswith(
        f" The group became a supergroup; its new chat ID is {MIGRATED_CHAT_ID}. "
        "Update the location's chat ID."
    )
    # The chat is never changed for the admin: the alert waits for the old channel.
    assert Location.objects.get(pk=location.pk).chat_id == DEFAULT_CHAT_ID
    assert (_row(off).status, _row(off).location_id) == ("pending", location.pk)
    assert state.failing == {location.pk: io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID)}
    assert [call for call in fake_telegram.sent if call["chat_id"] == MIGRATED_CHAT_ID] == []


# D-10: transient errors, 429, ambiguous sends and chart calls never open the incident

TOKEN_502 = "222222222:" + "B" * 35
TOKEN_REFUSED = "333333333:" + "D" * 35
TOKEN_429 = "444444444:" + "E" * 35
TOKEN_TIMEOUT = "666666666:" + "F" * 35
TOKEN_CHART = "777777777:" + "G" * 35
NO_PIN = {
    "ok": False,
    "error_code": 403,
    "description": "Forbidden: not enough rights to pin a message",
}


def _refused(token: str) -> requests.ConnectionError:
    # A real connect-phase exception carries the URL, and so the token, in its text (P-12).
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


def _monitored(location_factory: Callable[..., Any], since: datetime, **kw: Any) -> Any:
    """A location on since ``since``, with its open on piece: the chart posts for it."""
    location = location_factory(**kw)
    LocationState.objects.filter(location=location).update(
        status="on",
        on_since=since,
        last_heartbeat_at=since,
        state_version=F("state_version") + 1,
    )
    PowerInterval.objects.create(location=location, state="on", start_at=since)
    return location


def _chart_pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass with chart work, the detection cursor moved to the clock's now."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
    return io_loop.run_iteration(clock, state, charts=True)


@pytest.mark.django_db(transaction=True)
def test_transient_429_maybe_and_chart_outcomes_never_open_failing(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    answers: dict[str, dict[str, Any]] = {
        TOKEN_502: {"status": 502, "json_body": {"ok": False, "error_code": 502}},
        TOKEN_REFUSED: {"exc": _refused(TOKEN_REFUSED)},
        TOKEN_429: {"status": 429, "json_body": _too_many_requests(30)},
        TOKEN_TIMEOUT: {"exc": requests.ReadTimeout("read timed out")},
    }
    alerts = {}
    for n, (token, answer) in enumerate(answers.items()):
        location = location_factory(bot_token=token, chat_id=-1002000000000 - n)
        alerts[token] = _queue(location)
        fake_telegram.fail(token, **answer)
    # A monitored location whose bot may post its chart but not pin it.
    charted = _monitored(location_factory, T0 - timedelta(hours=2), bot_token=TOKEN_CHART)
    fake_telegram.fail_method(TOKEN_CHART, "pinChatMessage", status=403, json_body=NO_PIN)
    fake_telegram.accept_chart(TOKEN_CHART)
    state = io_loop.RelayState()
    clock = FakeClock(T0)

    # Pass 1: the four sends fail, and the chart is posted; pass 2: the pin is refused.
    assert _chart_pass(clock, state) is True
    assert _chart_pass(clock, state) is True

    assert {token: _row(row).last_error for token, row in alerts.items()} == {
        TOKEN_502: "http_502",
        TOKEN_REFUSED: "connect_error",
        TOKEN_429: "429",
        TOKEN_TIMEOUT: "read_timeout",
    }
    assert _row(alerts[TOKEN_TIMEOUT]).status == "uncertain"
    assert fake_telegram.count(TOKEN_CHART, "pinChatMessage") == 1
    # The refused pin has its own incident (Phase 3 D-07); none is a delivery failure.
    pin_failed = OpsIncident.objects.filter(kind=lifecycle.KIND_CHART_PIN_FAILED)
    assert list(pin_failed.values_list("location_id", flat=True)) == [charted.pk]
    assert not OpsIncident.objects.filter(kind=delivery.KIND_DELIVERY_FAILING).exists()
    assert state.failing == {}


# The delivery module's own functions


def test_http_status_reads_the_client_code() -> None:
    assert delivery.http_status("http_403") == 403
    assert delivery.http_status("http_404") == 404
    # A code with no plausible status stands for 400 (a notice needs one).
    for code in ("read_timeout", "", "http_", "http_99", "http_600", "http_4x0", "http_٤٠٠"):
        assert delivery.http_status(code) == 400


@pytest.mark.django_db
def test_failing_incidents_returns_only_open_delivery_failing_incidents(
    location_factory: Callable[..., Any],
) -> None:
    refused, migrated, recovered, pin_only, unasked = (location_factory() for _ in range(5))
    with transaction.atomic():
        delivery.open_failing(refused.pk, T0, 403)
        delivery.open_failing(migrated.pk, T0 + _min(1), 400, MIGRATED_CHAT_ID)
        delivery.open_failing(recovered.pk, T0, 403)
        delivery.close_failing(recovered.pk, T0 + _min(2))
        OpsIncident.objects.create(
            kind=lifecycle.KIND_CHART_PIN_FAILED, location=pin_only, started_at=T0
        )
        delivery.open_failing(unasked.pk, T0, 401)

    found = delivery.failing_incidents(
        [refused.pk, migrated.pk, recovered.pk, pin_only.pk, 999_999]
    )

    assert found == {
        refused.pk: delivery.Failing(T0, 403, None),
        migrated.pk: delivery.Failing(T0 + _min(1), 400, MIGRATED_CHAT_ID),
    }
    assert delivery.failing_incidents([]) == {}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("details", "expected_status"),
    [
        ({"http_status": "403"}, 400),
        ({"migrate_to_chat_id": True}, 400),
        ({}, 400),
        ({"http_status": True}, 400),
        ({"http_status": 700}, 400),
        ({"http_status": 403, "migrate_to_chat_id": "-100"}, 403),
        ([403], 400),
    ],
    ids=[
        "str-status",
        "bool-migrate",
        "empty",
        "bool-status",
        "out-of-range",
        "str-migrate",
        "list",
    ],
)
def test_failing_incidents_survive_malformed_details(
    location_factory: Callable[..., Any], details: Any, expected_status: int
) -> None:
    location = location_factory()
    # Written past open_incident's check, as a hand edit or a future bug could.
    OpsIncident.objects.create(
        kind=delivery.KIND_DELIVERY_FAILING, location=location, started_at=T0, details=details
    )

    assert delivery.failing_incidents([location.pk]) == {
        location.pk: delivery.Failing(T0, expected_status, None)
    }


@pytest.mark.django_db
def test_record_test_success_without_an_open_incident_still_makes_the_alerts_due(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    OutboxMessage.objects.filter(pk=off.pk).update(next_attempt_at=T0 + _min(15))

    assert delivery.record_test_success(location.pk, T0 + _min(1)) is False

    assert _row(off).next_attempt_at == T0 + _min(1)
    assert _ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED) == []


@pytest.mark.django_db
def test_record_test_success_refuses_a_naive_time_and_changes_nothing(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    OutboxMessage.objects.filter(pk=off.pk).update(next_attempt_at=T0 + _min(15))
    with transaction.atomic():
        delivery.open_failing(location.pk, T0, 403)

    with pytest.raises(ValueError, match="naive"):
        delivery.record_test_success(location.pk, datetime(2026, 10, 1, 10, 20))  # noqa: DTZ001

    assert _incidents(location) == [(T0, None, {"http_status": 403})]
    assert _row(off).next_attempt_at == T0 + _min(15)
    assert _ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED) == []
