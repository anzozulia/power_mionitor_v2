"""An alert too old to matter is never sent, and the admin is told once (ALRT-03, D-07, D-08).

INV-15 #3: an alert still undeliverable at its maximum age is marked expired and never
sent, and the admin gets one ops notice. The maximum age is ``ALERT_MAX_AGE_HOURS``
(default 6), counted from ``recorded_at`` (D-07): ``expires_at = recorded_at +
timedelta(hours=CFG.alert_max_age_hours)``, set when the row is queued. Every relay pass
starts with expiry, before any head is sent, so a row is expired at exactly
``expires_at`` and still tried one microsecond before.

D-08: each expired subscriber alert queues exactly one ``ops_expired`` notice in the same
transaction; the location's next alert (the ON after an expired OFF) still goes out, with
its event time if it is late; an expired ops row is only logged, because a notice about a
notice would loop on a broken admin chat.

The relay cases call ``io_loop.run_iteration``, which calls ``close_old_connections()``, so
they are ``django_db(transaction=True)``. Time comes only from the ``FakeClock``; Telegram
is faked at the HTTP boundary. Local times are Europe/Kyiv (UTC+3 on 2026-10-01).
"""

import dataclasses
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, OPS_BOT_TOKEN, OPS_CHAT_ID, FakeClock
from django.db import OperationalError, transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.worker import io_loop

TOKEN_A = DEFAULT_BOT_TOKEN
# An OFF recorded at 07:03:31Z for an outage that started at 07:02:00Z (10:02 in Kyiv).
T = datetime(2026, 10, 1, 7, 3, 31, tzinfo=UTC)
EVENT = T - timedelta(seconds=91)
SIX_H = timedelta(hours=6)
PAYLOADS = {"power_off": {"was_on_us": 300_000_000}, "power_on": {"was_off_us": 3_300_000_000}}
EXPIRED_OFF = (
    "⌛ OFF alert for Test location (event 10:02) expired undelivered after 6h "
    "and will not be sent."
)
RELAY_LOGGER = "powermon.worker.io_loop"


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ and max age, set so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(
        settings.CFG, display_tz="Europe/Kyiv", alert_max_age_hours=6
    )
    return settings


def _queue(
    location: Any,
    kind: str = "power_off",
    *,
    at: datetime = T,
    event_at: datetime | None = None,
) -> OutboxMessage:
    """Queue one alert recorded (and so due) at ``at``, as its transition would."""
    with transaction.atomic():
        return outbox.enqueue(
            kind,
            location.pk,
            event_at=at - timedelta(seconds=91) if event_at is None else event_at,
            recorded_at=at,
            payload=PAYLOADS[kind],
        )


def _gap_notice(at: datetime = T) -> OutboxMessage:
    payload = {
        "start_us": ops.instant_us(at - timedelta(minutes=10)),
        "end_us": ops.instant_us(at),
    }
    with transaction.atomic():
        return outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload=payload, recorded_at=at)


def _row(message: OutboxMessage) -> OutboxMessage:
    return OutboxMessage.objects.get(pk=message.pk)


def _ops_rows() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(channel="ops").order_by("id"))


def _calls_to(fake: Any, token: str) -> int:
    return len([call for call in fake.calls if f"/bot{token}/" in call.request.url])


def _refused(token: str) -> requests.ConnectionError:
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


def _texts(fake: Any) -> list[str]:
    return [body["text"] for body in fake.sent]


# expires_at comes from ALERT_MAX_AGE_HOURS (D-07)


@pytest.mark.django_db
def test_expires_at_follows_alert_max_age_hours(
    location_factory: Callable[..., Any], settings: Any
) -> None:
    location = location_factory()
    # Microseconds in recorded_at: the age is added exactly, not rounded.
    at = T + timedelta(microseconds=123_456)

    default_alert = _queue(location, at=at)
    default_notice = _gap_notice(at)
    settings.CFG = dataclasses.replace(settings.CFG, alert_max_age_hours=2)
    short_alert = _queue(location, at=at)
    short_notice = _gap_notice(at)

    assert [_row(r).expires_at for r in (default_alert, default_notice)] == [at + SIX_H] * 2
    assert [_row(r).expires_at for r in (short_alert, short_notice)] == [
        at + timedelta(hours=2)
    ] * 2
    assert not hasattr(outbox, "MAX_AGE")


# INV-15 #3: expired, never sent, one notice (D-08)


@pytest.mark.django_db(transaction=True)
def test_INV15_expired_alert_never_sent_one_ops_notice(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    # Bot A's connections are refused for the whole 6 h; the ops bot works.
    fake_telegram.fail(TOKEN_A, exc=_refused(TOKEN_A))
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    for minutes in range(0, 360, 10):
        io_loop.run_iteration(FakeClock(T + timedelta(minutes=minutes)), state)
    tries = _calls_to(fake_telegram, TOKEN_A)
    assert (tries, _row(off).status, _ops_rows()) == (36, "pending", [])

    # The first pass at the maximum age expires the OFF before trying it again.
    assert io_loop.run_iteration(FakeClock(T + SIX_H), state) is True

    row = _row(off)
    assert (row.status, row.last_error, row.sent_at) == ("expired", "expired", None)
    assert _calls_to(fake_telegram, TOKEN_A) == tries
    [notice] = _ops_rows()
    assert (notice.kind, notice.payload, notice.location_id) == (
        "ops_expired",
        {"message_id": off.pk},
        location.pk,
    )
    assert (notice.status, notice.recorded_at) == ("sent", T + SIX_H)
    assert fake_telegram.sent == [
        {"chat_id": OPS_CHAT_ID, "text": EXPIRED_OFF, "parse_mode": "HTML"}
    ]
    for minutes in (1, 10, 60, 600):
        assert (
            io_loop.run_iteration(FakeClock(T + SIX_H + timedelta(minutes=minutes)), state) is False
        )
    assert _calls_to(fake_telegram, TOKEN_A) == tries
    assert len(_ops_rows()) == 1
    assert _row(off).status == "expired"


@pytest.mark.django_db(transaction=True)
def test_expiry_boundary(location_factory: Callable[..., Any], fake_telegram: Any) -> None:
    first = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    # One microsecond before its expires_at the row is still tried, and sent.
    assert io_loop.run_iteration(FakeClock(first.expires_at - timedelta(microseconds=1)), state)
    assert _row(first).status == "sent"

    # Recorded at the same instant, so the same expires_at: at exactly that time it expires.
    second = _queue(location_factory())
    assert second.expires_at == first.expires_at
    assert io_loop.run_iteration(FakeClock(second.expires_at), state) is False
    assert (_row(second).status, _row(second).last_error) == ("expired", "expired")
    assert len(fake_telegram.calls) == 1


@pytest.mark.django_db(transaction=True)
def test_D08_on_after_an_expired_off_is_delivered_with_its_event_time(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    fake_telegram.fail(TOKEN_A, exc=_refused(TOKEN_A))
    fake_telegram.accept(TOKEN_A)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()
    assert io_loop.run_iteration(FakeClock(T), state) is True
    assert io_loop.run_iteration(FakeClock(T + SIX_H), state) is True
    assert _row(off).status == "expired"

    # Power comes back at 16:13 local; the ON goes out 10 min later, so it is late.
    restored = T + SIX_H + timedelta(minutes=10)
    on = _queue(location, "power_on", at=restored, event_at=restored)
    assert io_loop.run_iteration(FakeClock(restored + timedelta(minutes=10)), state) is True

    assert _row(on).status == "sent"
    assert _texts(fake_telegram) == [
        EXPIRED_OFF,
        "🟢 16:13 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>",
    ]
    assert io_loop.run_iteration(FakeClock(restored + timedelta(minutes=11)), state) is False
    assert _calls_to(fake_telegram, TOKEN_A) == 2
    assert len(_ops_rows()) == 1


@pytest.mark.django_db(transaction=True)
def test_D08_expired_ops_row_is_only_logged(
    fake_telegram: Any, ops_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    notice = _gap_notice()
    fake_telegram.accept(OPS_BOT_TOKEN)
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)

    assert io_loop.run_iteration(FakeClock(notice.expires_at), io_loop.RelayState()) is False

    assert (_row(notice).status, _row(notice).last_error) == ("expired", "expired")
    # No notice about the notice: that would loop on a broken admin chat.
    assert [r.pk for r in _ops_rows()] == [notice.pk]
    assert [r.getMessage() for r in caplog.records if r.name == RELAY_LOGGER] == [
        f"ops notice {notice.pk} expired undelivered; it is not resent"
    ]
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_expiry_skips_sending_and_finished_rows(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    statuses = ("pending", "sending", "sent", "uncertain", "expired", "dropped")
    rows = {
        status: _queue(location, at=T + timedelta(seconds=i)) for i, status in enumerate(statuses)
    }
    for status, row in rows.items():
        OutboxMessage.objects.filter(pk=row.pk).update(status=status)
    notice = _gap_notice()
    later = T + timedelta(hours=40)
    fresh = _queue(location, at=later - timedelta(hours=1))

    expired = outbox.expire_due(later)

    # Only pending rows whose expires_at has come; a sending row is the relay's own.
    assert expired == [
        outbox.RowRef(rows["pending"].pk, "subscriber", location.pk),
        outbox.RowRef(notice.pk, "ops", None),
    ]
    assert {s: _row(r).status for s, r in rows.items()} == {
        s: "expired" if s == "pending" else s for s in statuses
    }
    assert _row(rows["sending"]).last_error == ""
    assert _row(fresh).status == "pending"
    assert outbox.expire_due(later) == []


@pytest.mark.django_db(transaction=True)
def test_an_expired_head_unblocks_the_next_alert_in_the_same_pass(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    off = _queue(location)
    on = _queue(location, "power_on", at=T + SIX_H - timedelta(minutes=1))
    fake_telegram.accept(TOKEN_A)

    assert io_loop.run_iteration(FakeClock(T + SIX_H), io_loop.RelayState()) is True

    assert (_row(off).status, _row(on).status) == ("expired", "sent")
    # Recorded 60 s before it was sent: not late, so no prefix.
    assert _texts(fake_telegram) == ["🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>"]


@pytest.mark.django_db(transaction=True)
def test_a_database_error_in_expiry_ends_the_pass_before_any_send(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing may be sent before expiry ran: the error ends the pass and reaches the I/O
    # thread, which logs it once per outage by class name (run_worker's DbOutageLog).
    off = _queue(location_factory())
    fake_telegram.accept(TOKEN_A)

    def broken(now: datetime) -> list[outbox.RowRef]:
        raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(outbox, "expire_due", broken)

    with pytest.raises(OperationalError):
        io_loop.run_iteration(FakeClock(T), io_loop.RelayState())

    assert len(fake_telegram.calls) == 0
    assert (_row(off).status, _row(off).attempts) == ("pending", 0)
