"""The chart message lifecycle in the worker's I/O pass (D-01 to D-05, INV-14, INV-17).

A monitored location's chat gets today's chart posted silently (D-01), and the record
(location, local date, the chat it went to, the message id) is written as soon as
Telegram accepts the photo, before any pin (D-02, INV-17). Chart work is the last step
of ``io_loop.run_iteration`` and runs only with ``charts=True``, at most one call per
pass, after every alert and the ops head (D-05, INV-14); the Phase 2 callers, which do
not pass the flag, make no chart call.

``run_iteration`` calls ``close_old_connections()``, so every test that runs it is
``django_db(transaction=True)``. Time comes only from the ``FakeClock`` passed in; a fake
call can move it forward while the request is in flight (``answer_method``). Telegram is
faked at the HTTP boundary (``fake_telegram``). Renders are real (Pillow), so each test
keeps to a handful of them. The display time zone is Europe/Kyiv (UTC+3 until
2026-10-25 01:00 UTC).
"""

import dataclasses
import io
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, ChartCall, FakeClock
from PIL import Image

from powermon.chart import lifecycle
from powermon.chart.models import ChartMessage
from powermon.locations.models import Location
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# Thu 2026-10-01 12:05 local (09:05 UTC); monitoring started at 08:00 local.
NOON_05 = kyiv("2026-10-01 12:05")
SINCE = kyiv("2026-10-01 08:00")
OTHER_CHAT_ID = -1007777777777
CHAT_NOT_FOUND = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
NO_PIN_RIGHTS = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: not enough rights to manage pinned messages in the chat",
}


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _monitored(location_factory: Callable[..., Any], since: datetime = SINCE, **kw: Any) -> Any:
    """A location on since ``since``, with its open on piece (a monitored location)."""
    location = location_factory(**kw)
    monitor(location, since)
    return location


def _chart_calls(fake: Any, method: str) -> list[ChartCall]:
    return [call for call in fake.chart_calls if call.method == method]


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    return io_loop.run_iteration(clock, state, charts=True)


def _rows() -> list[ChartMessage]:
    return list(ChartMessage.objects.order_by("id"))


def _png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        return image.size


# Post and record (D-01, D-02 steps 1-2, INV-17)


def test_monitored_location_gets_todays_chart_posted_and_recorded(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    photos = _chart_calls(fake_telegram, "sendPhoto")
    assert len(photos) == 1
    assert photos[0].fields == {
        "chat_id": str(DEFAULT_CHAT_ID),
        "caption": "No outages today\nUpdated 12:05",
        "disable_notification": "true",
    }
    assert _png_size(photos[0].files["photo"]) == (1280, 1000)
    [row] = _rows()
    assert (row.location_id, row.local_date.isoformat(), row.chat_id, row.message_id) == (
        location.pk,
        "2026-10-01",
        DEFAULT_CHAT_ID,
        1001,
    )
    assert row.pinned is False
    assert (row.pin_failed_at, row.finalized_at, row.retired_at) == (None, None, None)
    assert row.last_rendered_at == NOON_05
    assert row.created_at == NOON_05

    # The record is there: the next pass posts nothing more (INV-17).
    _pass(clock, state)
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    assert len(_rows()) == 1


def test_without_the_charts_flag_no_chart_call_is_made(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # The Phase 2 callers pass no flag (Pitfall 4).
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(NOON_05), state) is False

    assert len(fake_telegram.calls) == 0
    assert _rows() == []


def test_refused_post_writes_no_record_and_backs_off_the_step(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "sendPhoto", status=400, json_body=CHAT_NOT_FOUND)
    state = io_loop.RelayState()

    assert _pass(FakeClock(NOON_05), state) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    assert _rows() == []
    assert lifecycle.chart_key(location.pk, "post") in state.not_before


# Pin after the record (D-01, D-02 step 3, D-04, D-07, INV-17)


def test_monitored_location_gets_todays_chart_posted_recorded_and_pinned(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    [posted] = _rows()
    assert (posted.message_id, posted.pinned) == (1001, False)

    assert _pass(clock, state) is True
    pins = _chart_calls(fake_telegram, "pinChatMessage")
    assert [pin.fields for pin in pins] == [
        {"chat_id": DEFAULT_CHAT_ID, "message_id": 1001, "disable_notification": True}
    ]
    [row] = _rows()
    assert (row.pinned, row.pin_failed_at) == (True, None)

    # Nothing is due any more: no call at all.
    calls = len(fake_telegram.calls)
    assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == calls


def test_the_record_exists_before_the_pin(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    recorded_when_pinned: list[bool] = []

    def check() -> None:
        recorded_when_pinned.append(ChartMessage.objects.filter(message_id=1001).exists())

    # Registered first, so it answers the first pin; accept_chart answers the rest.
    fake_telegram.answer_method(DEFAULT_BOT_TOKEN, "pinChatMessage", check)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    _pass(clock, state)
    _pass(clock, state)

    assert recorded_when_pinned == [True]
    assert _rows()[0].pinned is True


def test_pin_uses_the_recorded_chat(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    _pass(clock, state)

    # The location moves to another chat after the post (D-04: the record's chat wins).
    Location.objects.filter(pk=location.pk).update(chat_id=OTHER_CHAT_ID)
    _pass(clock, state)

    [pin] = _chart_calls(fake_telegram, "pinChatMessage")
    assert (pin.fields["chat_id"], pin.fields["message_id"]) == (DEFAULT_CHAT_ID, 1001)


def test_permanent_pin_failure_waits_for_the_next_refresh(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    clock = FakeClock(NOON_05)
    # The pin takes 2 s and is refused: the bot may post but not pin (INV-17 #1).
    fake_telegram.answer_method(
        DEFAULT_BOT_TOKEN,
        "pinChatMessage",
        lambda: clock.advance(seconds=2),
        status=400,
        json_body=NO_PIN_RIGHTS,
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()
    _pass(clock, state)

    assert _pass(clock, state) is True

    [row] = _rows()
    assert row.pinned is False
    assert row.pin_failed_at == NOON_05 + timedelta(seconds=2)
    assert io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID) not in state.not_before
    assert io_loop.bot_wide_key(DEFAULT_BOT_TOKEN) not in state.not_before
    # No retry before the next render (the refresh 15 min after the post, Task 3).
    for minutes in (1, 5, 14):
        clock.set(NOON_05 + timedelta(minutes=minutes))
        _pass(clock, state)
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "pinChatMessage") == 1
    assert ChartMessage.objects.get(location=location).pinned is False
