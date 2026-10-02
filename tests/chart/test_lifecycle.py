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
from datetime import datetime
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, ChartCall, FakeClock
from PIL import Image

from powermon.chart import lifecycle
from powermon.chart.models import ChartMessage
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# Thu 2026-10-01 12:05 local (09:05 UTC); monitoring started at 08:00 local.
NOON_05 = kyiv("2026-10-01 12:05")
SINCE = kyiv("2026-10-01 08:00")
CHAT_NOT_FOUND = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}


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
