"""A late alert states when the event happened (ALRT-04, D-05, D-06, D-07).

An alert sent more than 120 s after its ``recorded_at`` puts the event's local ``HH:MM``
before the bold status, with no preposition (D-05). The event time is the outage start
for OFF and the restore time for ON. When the event's local date is not the delivery's
local date the prefix is ``DD.MM HH:MM`` (D-06). Lateness is measured when the text is
rendered, right before the claim and send, from ``recorded_at``, never from the backdated
outage start (D-07, INV-15): an OFF is always recorded at least 90 s after its outage
start, so measuring from the start would mark every OFF late.

Local times are in the display TZ (``settings.CFG.display_tz``, Europe/Kyiv here), from
the stored UTC instant with ``zoneinfo``. The DST cases use the vectors computed in
02-RESEARCH.md: on 2026-10-25 Kyiv falls back at 01:00Z (04:00 EEST becomes 03:00 EET), so
03:30 local happens twice and both read "03:30"; on 2027-03-28 it springs forward at
01:00Z (03:00 EET becomes 04:00 EEST), so 02:59 is followed by 04:00.

The relay cases call ``io_loop.run_iteration``, which calls ``close_old_connections()``, so
they are ``django_db(transaction=True)``. Time comes only from the ``FakeClock``; the
delivered text is read from ``FakeTelegram.sent``.
"""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, OPS_BOT_TOKEN, FakeClock
from django.db import transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.worker import io_loop

TOKEN_A = DEFAULT_BOT_TOKEN
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
MIN_US = 60_000_000
# INV-15 #1's times on 2026-10-01 (UTC+3 in Kyiv): last heartbeat 10:02:00 local, the OFF
# recorded 91 s later, power back at 10:06:00 local.
EVENT_OFF = datetime(2026, 10, 1, 7, 2, tzinfo=UTC)
RECORDED_OFF = datetime(2026, 10, 1, 7, 3, 31, tzinfo=UTC)
RESTORED = datetime(2026, 10, 1, 7, 6, tzinfo=UTC)
WAS_ON = "\n⚡ Power was ON for: <b>5m</b>"
WAS_OFF = "\n⚡ Power was OFF for: <b>4m</b>"
OFF_EN = "🔴 <b>POWER OFF</b>" + WAS_ON


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    return settings


def _queue(location: Any, kind: str, *, event_at: datetime, recorded_at: datetime) -> OutboxMessage:
    """Queue one alert as its transition would: due at ``recorded_at``."""
    payload = {"was_on_us": 5 * MIN_US} if kind == "power_off" else {"was_off_us": 4 * MIN_US}
    with transaction.atomic():
        return outbox.enqueue(
            kind, location.pk, event_at=event_at, recorded_at=recorded_at, payload=payload
        )


def _off(location: Any, event_at: datetime) -> OutboxMessage:
    """An OFF recorded 91 s after its outage start (period 60 s + grace 30 s + a cycle)."""
    return _queue(
        location, "power_off", event_at=event_at, recorded_at=event_at + timedelta(seconds=91)
    )


def _on(location: Any, restored_at: datetime) -> OutboxMessage:
    """An ON: recorded at the heartbeat that restored power, which is also its event time."""
    return _queue(location, "power_on", event_at=restored_at, recorded_at=restored_at)


def _texts(fake: Any) -> list[str]:
    return [body["text"] for body in fake.sent]


@pytest.mark.django_db(transaction=True)
def test_ALRT04_alert_sent_within_120s_has_no_time(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Two OFFs recorded at the same instant. A's goes out exactly 120 s after it was
    # recorded; A's send takes 1 ms, so B's text is rendered 120.001 s after (strict >).
    a = location_factory(bot_token=TOKEN_A)
    b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    _queue(a, "power_off", event_at=EVENT_OFF, recorded_at=RECORDED_OFF)
    _queue(b, "power_off", event_at=EVENT_OFF, recorded_at=RECORDED_OFF)
    clock = FakeClock(RECORDED_OFF + timedelta(seconds=120))
    fake_telegram.answer(TOKEN_A, lambda: clock.advance(milliseconds=1))
    fake_telegram.accept(TOKEN_B)

    assert io_loop.run_iteration(clock, io_loop.RelayState()) is True

    assert _texts(fake_telegram) == [OFF_EN, "🔴 10:02 <b>POWER OFF</b>" + WAS_ON]
    assert io_loop.LATE_AFTER == timedelta(seconds=120)


@pytest.mark.django_db(transaction=True)
def test_ALRT04_late_on_states_the_restore_time(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    on = _on(location_factory(), RESTORED)
    fake_telegram.accept(TOKEN_A)
    sent_at = datetime(2026, 10, 1, 7, 10, 30, tzinfo=UTC)

    assert io_loop.run_iteration(FakeClock(sent_at), io_loop.RelayState()) is True

    assert _texts(fake_telegram) == ["🟢 10:06 <b>POWER ON</b>" + WAS_OFF]
    assert (OutboxMessage.objects.get(pk=on.pk).status, on.event_at) == ("sent", RESTORED)


@pytest.mark.django_db(transaction=True)
def test_ALRT04_late_off_states_the_outage_start_not_the_record_time(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Recorded 07:03:31Z, but the outage started at the last heartbeat, 10:02 local.
    _queue(location_factory(), "power_off", event_at=EVENT_OFF, recorded_at=RECORDED_OFF)
    fake_telegram.accept(TOKEN_A)

    assert io_loop.run_iteration(
        FakeClock(datetime(2026, 10, 1, 7, 10, 5, tzinfo=UTC)), io_loop.RelayState()
    )

    assert _texts(fake_telegram) == ["🔴 10:02 <b>POWER OFF</b>" + WAS_ON]


@pytest.mark.django_db(transaction=True)
def test_ALRT04_late_off_prefix_dst_2026_10_25(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # 20:58Z is 23:58 EEST on 24.10; delivered at 00:00Z, which is 03:00 EEST on 25.10.
    _queue(
        location_factory(),
        "power_off",
        event_at=datetime(2026, 10, 24, 20, 58, tzinfo=UTC),
        recorded_at=datetime(2026, 10, 24, 21, 0, tzinfo=UTC),
    )
    # 00:30Z is the first 03:30 (EEST, before the fall-back at 01:00Z), 01:30Z the second
    # (EET, after it). Each is sent 10 min after it was recorded.
    first = _off(location_factory(), datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    second = _off(location_factory(), datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    for at in (
        datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        first.recorded_at + timedelta(minutes=10),
        second.recorded_at + timedelta(minutes=10),
    ):
        assert io_loop.run_iteration(FakeClock(at), state) is True

    assert _texts(fake_telegram) == [
        "🔴 24.10 23:58 <b>POWER OFF</b>" + WAS_ON,
        "🔴 03:30 <b>POWER OFF</b>" + WAS_ON,
        "🔴 03:30 <b>POWER OFF</b>" + WAS_ON,
    ]


@pytest.mark.django_db(transaction=True)
def test_ALRT04_late_on_prefix_dst_2027_03_28(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # 01:00Z is 04:00 EEST, the first minute after the spring-forward gap; 00:59Z is
    # 02:59 EET, the last minute before it.
    _on(location_factory(), datetime(2027, 3, 28, 1, 0, tzinfo=UTC))
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(datetime(2027, 3, 28, 1, 10, tzinfo=UTC)), state)
    _on(location_factory(), datetime(2027, 3, 28, 0, 59, tzinfo=UTC))
    assert io_loop.run_iteration(FakeClock(datetime(2027, 3, 28, 1, 30, tzinfo=UTC)), state)

    assert _texts(fake_telegram) == [
        "🟢 04:00 <b>POWER ON</b>" + WAS_OFF,
        "🟢 02:59 <b>POWER ON</b>" + WAS_OFF,
    ]


@pytest.mark.django_db(transaction=True)
def test_ALRT04_date_prefix_when_local_date_differs(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # 21:59Z is 23:59 EET on 25.10 (after the fall-back); 22:05Z is 00:05 on 26.10.
    _off(location_factory(), datetime(2026, 10, 25, 21, 59, tzinfo=UTC))
    fake_telegram.accept(TOKEN_A)

    assert io_loop.run_iteration(
        FakeClock(datetime(2026, 10, 25, 22, 5, tzinfo=UTC)), io_loop.RelayState()
    )

    assert _texts(fake_telegram) == ["🔴 25.10 23:59 <b>POWER OFF</b>" + WAS_ON]


@pytest.mark.django_db(transaction=True)
def test_ALRT04_prefix_uses_the_current_display_tz(
    location_factory: Callable[..., Any], fake_telegram: Any, settings: Any
) -> None:
    # The display TZ is read at send time, like the text itself.
    _queue(location_factory(), "power_off", event_at=EVENT_OFF, recorded_at=RECORDED_OFF)
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="UTC")
    fake_telegram.accept(TOKEN_A)

    assert io_loop.run_iteration(
        FakeClock(datetime(2026, 10, 1, 7, 10, tzinfo=UTC)), io_loop.RelayState()
    )

    assert _texts(fake_telegram) == ["🔴 07:02 <b>POWER OFF</b>" + WAS_ON]


@pytest.mark.django_db(transaction=True)
def test_ALRT04_a_late_ops_notice_gets_no_alert_prefix(
    fake_telegram: Any, ops_settings: Any
) -> None:
    # Ops texts carry their own times (D-11); the late prefix is for subscriber alerts only.
    payload = {
        "start_us": ops.instant_us(datetime(2026, 10, 1, 7, 0, 12, tzinfo=UTC)),
        "end_us": ops.instant_us(datetime(2026, 10, 1, 7, 10, 40, tzinfo=UTC)),
    }
    with transaction.atomic():
        outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload=payload, recorded_at=RECORDED_OFF)
    fake_telegram.accept(OPS_BOT_TOKEN)

    assert io_loop.run_iteration(
        FakeClock(RECORDED_OFF + timedelta(minutes=10)), io_loop.RelayState()
    )

    assert _texts(fake_telegram) == [
        "⏸ Monitoring gap 01.10 10:00:12 – 10:10:40 (10m 28s). "
        "Recorded as not monitored; no subscriber alerts were sent for it."
    ]
