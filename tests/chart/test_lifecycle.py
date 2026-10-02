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
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
import requests
from chart_fixtures import KYIV, insert_pieces, kyiv, local_pieces, monitor, set_status
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    ChartCall,
    FakeClock,
)
from django.db import IntegrityError, OperationalError, connection, transaction
from PIL import Image
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart import lifecycle, render
from powermon.chart.model import Piece, Week
from powermon.chart.models import ChartMessage
from powermon.engine import transitions
from powermon.locations.models import Location
from powermon.worker import io_loop
from powermon.worker.lease import Lease

pytestmark = pytest.mark.django_db(transaction=True)

# Thu 2026-10-01 12:05 local (09:05 UTC); monitoring started at 08:00 local.
TODAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-01 12:05")
SINCE = kyiv("2026-10-01 08:00")
OTHER_CHAT_ID = -1007777777777
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
CHAT_NOT_FOUND = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
NO_PIN_RIGHTS = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: not enough rights to manage pinned messages in the chat",
}
NOT_MODIFIED = {
    "ok": False,
    "error_code": 400,
    "description": (
        "Bad Request: message is not modified: specified new message content and reply "
        "markup are exactly the same as a current content and reply markup of the message"
    ),
}
FLOOD_30 = {
    "ok": False,
    "error_code": 429,
    "description": "Too Many Requests: retry after 30",
    "parameters": {"retry_after": 30},
}
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
KICKED = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}
OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
LIFECYCLE_LOGGER = lifecycle.__name__
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B", OPS_BOT_TOKEN: "ops"}


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


def _requests(fake: Any, start: int = 0) -> list[tuple[str, str]]:
    """(bot label, Bot API method) of every request from index ``start`` on, in order."""
    out = []
    for call in list(fake.calls)[start:]:
        token, method = call.request.url.split("/bot", 1)[1].split("/", 1)
        out.append((BOTS[token], method))
    return out


def _seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


def _queue_alert(location: Any, at: datetime = NOON_05) -> OutboxMessage:
    """An OFF alert recorded (and so due) at ``at``, as a transition would queue it."""
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - _seconds(91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


def _refused(token: str, method: str) -> requests.ConnectionError:
    # A real connect-phase exception, which the client classifies as not_sent (connect_error);
    # its text carries the URL, and so the token (as tests/alerts/test_relay.py builds it).
    path = f"/bot{token}/{method}"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


def _my_backend_pid() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return int(cur.fetchone()[0])


def _seed(
    location: Any,
    *,
    day: date = TODAY,
    rendered: datetime = NOON_05,
    message_id: int = 1001,
    chat_id: int = DEFAULT_CHAT_ID,
    pinned: bool = True,
) -> ChartMessage:
    """A record as an earlier post left it (pinned by default)."""
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=chat_id,
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=rendered,
        created_at=rendered,
    )


def _spy_renders(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record the language and name of every render; the real renderer still runs."""
    seen: list[tuple[str, str]] = []
    real = render.render_png

    def spy(week: Week, *, lang: str, name: str) -> bytes:
        seen.append((lang, name))
        return real(week, lang=lang, name=name)

    monkeypatch.setattr(render, "render_png", spy)
    return seen


def _location(location_id: int, token: str = DEFAULT_BOT_TOKEN) -> lifecycle.ChartLocation:
    return lifecycle.ChartLocation(location_id, f"L{location_id}", "en", token, DEFAULT_CHAT_ID)


def _row(
    row_id: int,
    location_id: int,
    *,
    rendered: datetime,
    day: date = TODAY,
    pinned: bool = True,
    pin_failed_at: datetime | None = None,
) -> lifecycle.ChartRow:
    return lifecycle.ChartRow(
        id=row_id,
        location_id=location_id,
        local_date=day,
        chat_id=DEFAULT_CHAT_ID,
        message_id=1000 + row_id,
        pinned=pinned,
        pin_failed_at=pin_failed_at,
        last_rendered_at=rendered,
        finalized_at=None,
    )


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


# Refresh every 15 min from DB state, catch-up, D-03, D-14, INV-05, INV-17 #2 (D-05)


def test_CHRT02_refresh_is_due_15_minutes_after_the_last_render(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    clock = FakeClock(NOON_05)
    # The first edit takes 1 s: last_rendered_at is when Telegram answered.
    fake_telegram.answer_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", lambda: clock.advance(seconds=1)
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()
    _pass(clock, state)
    _pass(clock, state)
    assert _rows()[0].pinned is True

    clock.set(NOON_05 + timedelta(minutes=14, seconds=59))
    assert _pass(clock, state) is False
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 0

    clock.set(NOON_05 + timedelta(minutes=15))
    assert _pass(clock, state) is True

    [edit] = _chart_calls(fake_telegram, "editMessageMedia")
    assert (edit.fields["chat_id"], edit.fields["message_id"]) == (str(DEFAULT_CHAT_ID), "1001")
    assert json.loads(edit.fields["media"]) == {
        "type": "photo",
        "media": "attach://chart",
        "caption": "No outages today\nUpdated 12:20",
    }
    assert _png_size(edit.files["chart"]) == (1280, 1000)
    assert _rows()[0].last_rendered_at == NOON_05 + timedelta(minutes=15, seconds=1)


def test_refresh_not_modified_counts_as_rendered(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, rendered=NOON_05)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=NOT_MODIFIED
    )
    state = io_loop.RelayState()
    at = NOON_05 + timedelta(minutes=15)

    assert _pass(FakeClock(at), state) is True

    assert _rows()[0].last_rendered_at == at
    assert state.not_before == {}


def test_refresh_catches_up_once_after_downtime(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, rendered=NOON_05)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(kyiv("2026-10-01 14:30"))
    state = io_loop.RelayState()

    # Down from 12:05 to 14:30: one refresh now, not one per missed 15-min slot (INV-18).
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False
    clock.advance(minutes=14)
    assert _pass(clock, state) is False

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 1
    assert _rows()[0].last_rendered_at == kyiv("2026-10-01 14:30")


def test_D03_waiting_location_makes_no_chart_call_until_its_first_heartbeat(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    waiting = location_factory()
    gone = _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    Location.objects.filter(pk=gone.pk).update(deleted_at=SINCE)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    for _ in range(3):
        assert _pass(clock, state) is False
        clock.advance(minutes=20)
    assert len(fake_telegram.calls) == 0

    # The first heartbeat starts monitoring (MON-01); the next pass posts its chart.
    assert transitions.record_heartbeat(waiting.pk, clock.now()) == "started"
    assert _pass(clock, state) is True

    [photo] = _chart_calls(fake_telegram, "sendPhoto")
    assert photo.token == DEFAULT_BOT_TOKEN
    assert [row.location_id for row in _rows()] == [waiting.pk]
    assert fake_telegram.count(TOKEN_B, "sendPhoto") == 0


def test_INV05_chart_ignores_alerts_enabled_and_maintenance(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory(alerts_enabled=False, maintenance=True)
    # In maintenance the live piece is not monitored (INV-04); the status stays on.
    set_status(location, "on", at=SINCE)
    insert_pieces(location, [Piece("not_monitored", SINCE, None, None)])
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    _pass(clock, state)
    _pass(clock, state)
    clock.advance(minutes=15)
    _pass(clock, state)

    assert [call.method for call in fake_telegram.chart_calls] == [
        "sendPhoto",
        "pinChatMessage",
        "editMessageMedia",
    ]
    [row] = _rows()
    assert (row.pinned, row.last_rendered_at) == (True, clock.now())


def test_INV17_2_a_second_run_the_same_day_posts_nothing(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(kyiv("2026-10-02 00:00:05"))
    _pass(clock, io_loop.RelayState())
    _pass(clock, io_loop.RelayState())
    [row] = _rows()
    assert (row.local_date, row.pinned) == (date(2026, 10, 2), True)

    # A worker restart at 00:00:40: fresh relay state, the record is in the database.
    clock.set(kyiv("2026-10-02 00:00:40"))
    restarted = io_loop.RelayState()
    for _ in range(3):
        assert _pass(clock, restarted) is False
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1

    # The database refuses a second active record for the day...
    with pytest.raises(IntegrityError), transaction.atomic():
        _seed(location, day=date(2026, 10, 2), message_id=2002)
    # ...while a retired one does not block its replacement.
    ChartMessage.objects.filter(pk=row.pk).update(retired_at=clock.now(), pinned=False)
    _seed(location, day=date(2026, 10, 2), message_id=2002)
    assert ChartMessage.objects.filter(location=location, retired_at__isnull=True).count() == 1


def test_D14_refresh_uses_the_current_name_and_language(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    rendered = _spy_renders(monkeypatch)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    _pass(clock, state)
    _pass(clock, state)

    Location.objects.filter(pk=location.pk).update(name="Дача", language="uk")
    clock.advance(minutes=15)
    _pass(clock, state)

    [edit] = _chart_calls(fake_telegram, "editMessageMedia")
    caption = json.loads(edit.fields["media"])["caption"]
    assert caption == "Сьогодні відключень не було\nОновлено о 12:20"
    assert rendered == [("en", "Test location"), ("uk", "Дача")]


def test_INV03_1_caption_matches_the_outage(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = location_factory()
    # Heartbeats until 10:00, the next one at 12:00: off 10:00-12:00 (INV-03 #1).
    insert_pieces(
        location,
        local_pieces(
            [
                ("on", "2026-10-01 08:00", "2026-10-01 10:00"),
                ("off", "2026-10-01 10:00", "2026-10-01 12:00"),
                ("on", "2026-10-01 12:00", None),
            ]
        ),
    )
    set_status(location, "on", at=kyiv("2026-10-01 12:00"))
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)

    _pass(FakeClock(NOON_05), io_loop.RelayState())

    [photo] = _chart_calls(fake_telegram, "sendPhoto")
    assert photo.fields["caption"] == "Today off: 2h · 1 outage\nUpdated 12:05"


def test_refresh_order_oldest_first(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    low = _monitored(location_factory)
    high = _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    assert low.pk < high.pk
    _seed(low, rendered=kyiv("2026-10-01 12:06"))
    _seed(high, rendered=kyiv("2026-10-01 12:05"), message_id=2001, chat_id=CHAT_B)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(kyiv("2026-10-01 12:30"))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False

    assert [call.token for call in _chart_calls(fake_telegram, "editMessageMedia")] == [
        TOKEN_B,
        DEFAULT_BOT_TOKEN,
    ]


def test_failed_pin_is_retried_after_the_next_refresh(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "pinChatMessage", status=400, json_body=NO_PIN_RIGHTS
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    _pass(clock, state)
    _pass(clock, state)
    assert _rows()[0].pin_failed_at == NOON_05

    for minutes in (5, 14):
        clock.set(NOON_05 + timedelta(minutes=minutes))
        assert _pass(clock, state) is False
    # 12:20: the refresh comes first (the pin is not due before a new render)...
    clock.set(NOON_05 + timedelta(minutes=15))
    assert _pass(clock, state) is True
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "pinChatMessage") == 1
    # ...and the next pass tries the pin again (D-07).
    assert _pass(clock, state) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "pinChatMessage") == 2
    [row] = _rows()
    assert (row.pinned, row.pin_failed_at) == (True, None)


# Pure planner ordering (CHRT-02, D-02)


def test_plan_equal_render_times_refresh_the_lower_location_id_first() -> None:
    a = _location(1)
    b = _location(2)
    rows = [_row(10, 2, rendered=NOON_05), _row(11, 1, rendered=NOON_05)]
    now = NOON_05 + timedelta(minutes=15)

    action = lifecycle.plan([a, b], rows, today=TODAY, now=now, not_before={})

    assert action == lifecycle.Action("refresh", a, rows[1])


def test_plan_midnight_steps_beat_any_refresh() -> None:
    # Location 1's refresh has waited longest; location 2 still has to post today.
    a = _location(1)
    b = _location(2)
    rows = [_row(10, 1, rendered=NOON_05 - timedelta(hours=2))]

    action = lifecycle.plan([a, b], rows, today=TODAY, now=NOON_05, not_before={})

    assert action == lifecycle.Action("post", b)


def test_plan_nothing_due() -> None:
    a = _location(1)
    rows = [_row(10, 1, rendered=NOON_05)]

    assert lifecycle.plan([a], rows, today=TODAY, now=NOON_05, not_before={}) is None
    assert lifecycle.plan([], [], today=TODAY, now=NOON_05, not_before={}) is None


def test_plan_skips_a_bot_or_channel_that_is_backing_off() -> None:
    later = NOON_05 + timedelta(minutes=1)
    a = _location(1)
    b = _location(2, token=TOKEN_B)
    unpinned = _row(10, 1, rendered=NOON_05 - timedelta(minutes=20), pinned=False)

    # A's bot is held (a 429 or 5xx, by an alert or a chart): none of its steps goes.
    held = {io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): later}
    assert lifecycle.plan([a, b], [unpinned], today=TODAY, now=NOON_05, not_before=held) == (
        lifecycle.Action("post", b)
    )
    # A's channel backs off (the alert relay's chat_key): no pin, no refresh, no post.
    channel = {io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID): later}
    assert lifecycle.plan([a], [unpinned], today=TODAY, now=NOON_05, not_before=channel) is None
    assert lifecycle.plan([a], [], today=TODAY, now=NOON_05, not_before=channel) is None
    # At the key's own time the step is due again.
    assert lifecycle.plan([a], [], today=TODAY, now=later, not_before=channel) == (
        lifecycle.Action("post", a)
    )


# Chart work and alerts: one call per pass after alerts, chart-only keys, step backoff,
# the lease fence, render errors (D-02, D-05, D-06, INV-14, C1)


def test_INV14_one_chart_call_per_pass_after_alerts_and_ops(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    a = _monitored(location_factory)
    b = _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    _queue_alert(a)
    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_PIN_RESTORED, payload={}, recorded_at=NOON_05, location_id=a.pk
        )
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    passes = []
    for _ in range(4):
        start = len(fake_telegram.calls)
        assert _pass(clock, state) is True
        passes.append(_requests(fake_telegram, start))

    assert passes == [
        [("A", "sendMessage"), ("ops", "sendMessage"), ("A", "sendPhoto")],
        [("A", "pinChatMessage")],
        [("B", "sendPhoto")],
        [("B", "pinChatMessage")],
    ]


def test_chart_failure_never_holds_the_channels_alerts(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    a = _monitored(location_factory)
    _seed(a, rendered=NOON_05)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=CHAT_NOT_FOUND
    )
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05 + timedelta(minutes=15))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    # Only the refresh's own key: neither the channel nor the bot waits for it.
    assert state.not_before == {
        lifecycle.chart_key(a.pk, "refresh"): clock.now() + timedelta(minutes=15)
    }
    off = _queue_alert(a, at=clock.now())
    assert _pass(clock, state) is True
    assert fake_telegram.sent == [{"chat_id": DEFAULT_CHAT_ID, "text": OFF_EN, "parse_mode": "HTML"}]
    assert OutboxMessage.objects.get(pk=off.pk).status == "sent"


def test_chart_429_backs_off_the_bot(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    a = _monitored(location_factory)
    clock = FakeClock(NOON_05)
    # Telegram answers the post after 3 s with a 429; every wait counts from the answer.
    fake_telegram.answer_method(
        DEFAULT_BOT_TOKEN,
        "sendPhoto",
        lambda: clock.advance(seconds=3),
        status=429,
        json_body=FLOOD_30,
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    answer = NOON_05 + _seconds(3)
    assert state.not_before == {
        io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): answer + _seconds(30),
        lifecycle.chart_key(a.pk, "post"): answer + _seconds(30 + 30),
    }
    # The bot's alerts wait for the 429 like an alert's own 429 (ALRT-06), no longer.
    off = _queue_alert(a, at=answer)
    clock.set(answer + _seconds(29))
    assert _pass(clock, state) is False
    clock.set(answer + _seconds(30))
    assert _pass(clock, state) is True
    assert OutboxMessage.objects.get(pk=off.pk).status == "sent"
    # The post itself waits for its own key.
    clock.set(answer + _seconds(59))
    assert _pass(clock, state) is False
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    clock.set(answer + _seconds(60))
    assert _pass(clock, state) is True
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 2
    assert len(_rows()) == 1


def test_bot_wide_failure_lets_other_steps_go_first(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    a = _monitored(location_factory)
    row = _seed(a, rendered=kyiv("2026-10-01 11:50"), pinned=False)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "pinChatMessage", status=502, json_body=BAD_GATEWAY
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    # The pin goes before the refresh that is due too (D-02), and the bot answers 502.
    assert _pass(clock, state) is True
    answer = NOON_05
    pin_key = lifecycle.chart_key(a.pk, "pin", row.pk)
    assert state.not_before == {
        io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): answer + _seconds(2),
        pin_key: answer + _seconds(2 + 30),
    }
    clock.set(answer + _seconds(1))
    assert _pass(clock, state) is False
    # Once the bot's hold ends, the refresh goes; the failed pin waits for its own key.
    clock.set(answer + _seconds(2))
    assert _pass(clock, state) is True
    clock.set(answer + _seconds(31))
    assert _pass(clock, state) is False
    clock.set(answer + _seconds(32))
    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == [
        ("A", "pinChatMessage"),
        ("A", "editMessageMedia"),
        ("A", "pinChatMessage"),
    ]
    assert ChartMessage.objects.get(pk=row.pk).pinned is True
    assert pin_key not in state.chart_failures


def test_step_backoff_grows_and_is_capped(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    a = _monitored(location_factory)
    waits = [30, 60, 120, 240, 480, 900, 900]
    for _ in waits:
        fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "sendPhoto", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    key = lifecycle.chart_key(a.pk, "post")

    for attempt, wait in enumerate(waits, start=1):
        answered = clock.now()
        assert _pass(clock, state) is True
        # An ambiguous post: maybe delivered, never recorded, posted again after the wait.
        assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == attempt
        assert _rows() == []
        assert state.chart_failures[key] == attempt
        assert state.not_before == {key: answered + _seconds(wait)}
        due = answered + _seconds(wait)
        clock.set(due - _seconds(1))
        assert _pass(clock, state) is False
        clock.set(due)

    assert _pass(clock, state) is True
    [row] = _rows()
    assert row.message_id == 1001
    assert key not in state.chart_failures
    warnings = [r.getMessage() for r in caplog.records if r.name == LIFECYCLE_LOGGER]
    assert len(warnings) == len(waits)
    assert all(str(a.pk) in line for line in warnings)
    assert "attempt 7" in warnings[-1]


def test_refused_post_holds_the_bot_briefly(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    a = _monitored(location_factory)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "sendPhoto", exc=_refused(DEFAULT_BOT_TOKEN, "sendPhoto")
    )
    state = io_loop.RelayState()

    assert _pass(FakeClock(NOON_05), state) is True

    assert _rows() == []
    assert state.not_before == {
        io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): NOON_05 + _seconds(2),
        lifecycle.chart_key(a.pk, "post"): NOON_05 + _seconds(2 + 30),
    }
    assert io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID) not in state.not_before


def test_step_delay() -> None:
    assert lifecycle.STEP_RETRY == _seconds(30)
    assert lifecycle.STEP_RETRY_MAX == lifecycle.REFRESH_EVERY == timedelta(minutes=15)
    assert lifecycle.step_delay(1) == _seconds(30)
    assert lifecycle.step_delay(2) == _seconds(60)
    assert lifecycle.step_delay(5) == _seconds(480)
    assert lifecycle.step_delay(6) == lifecycle.step_delay(50) == _seconds(900)
    assert lifecycle.step_delay(10**6) == _seconds(900)
    for bad in (0, -1):
        with pytest.raises(ValueError, match="failure"):
            lifecycle.step_delay(bad)


def test_chart_waits_for_the_relays_backoff(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    a = _monitored(location_factory)
    _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    _queue_alert(a)
    # The alert relay finds A's channel refusing the bot (403): that channel waits 15 min.
    fake_telegram.fail(DEFAULT_BOT_TOKEN, status=403, json_body=KICKED)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert state.not_before[io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID)] == (
        NOON_05 + timedelta(minutes=15)
    )
    assert _requests(fake_telegram) == [("A", "sendMessage"), ("B", "sendPhoto")]
    # While A's channel backs off, B's pin goes and A still gets nothing.
    clock.set(NOON_05 + timedelta(minutes=14, seconds=59))
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert [(row.chat_id, row.pinned) for row in _rows()] == [(CHAT_B, True)]


def test_stale_lease_makes_no_chart_call(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    # A session that holds no worker lock: this test's own connection (C1).
    state = io_loop.RelayState(lease_pid=_my_backend_pid())

    assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == 0

    lease = Lease(connection.settings_dict)
    try:
        assert lease.ensure_held().state == "held"
        state.lease_pid = lease.pid
        assert _pass(clock, state) is True
    finally:
        lease.close()
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    assert len(_rows()) == 1


def test_render_error_backs_off_that_step_only(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _monitored(location_factory, name="Broken")
    _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    real = render.render_png

    def failing(week: Week, *, lang: str, name: str) -> bytes:
        if name == "Broken":
            raise RuntimeError(f"cannot draw for bot {DEFAULT_BOT_TOKEN}")
        return real(week, lang=lang, name=name)

    monkeypatch.setattr(render, "render_png", failing)
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is False

    assert len(fake_telegram.calls) == 0
    assert state.not_before == {lifecycle.chart_key(a.pk, "post"): NOON_05 + timedelta(minutes=15)}
    lines = [
        r for r in caplog.records if r.name == LIFECYCLE_LOGGER and r.levelno >= logging.WARNING
    ]
    assert len(lines) == 1
    assert "RuntimeError" in lines[0].getMessage()
    assert str(a.pk) in lines[0].getMessage()
    assert lines[0].exc_info is None
    assert DEFAULT_BOT_TOKEN.split(":", 1)[1] not in caplog.text
    # The other location's chart work goes on.
    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == [("B", "sendPhoto")]


def test_stop_set_makes_no_chart_call(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    stop = threading.Event()
    stop.set()

    assert io_loop.run_iteration(FakeClock(NOON_05), io_loop.RelayState(), stop, charts=True) is False

    assert len(fake_telegram.calls) == 0


def test_stop_during_the_render_makes_no_call(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    stop = threading.Event()
    real = render.render_png

    def render_then_sigterm(week: Week, *, lang: str, name: str) -> bytes:
        png = real(week, lang=lang, name=name)
        stop.set()
        return png

    monkeypatch.setattr(render, "render_png", render_then_sigterm)

    assert lifecycle.run_step(FakeClock(NOON_05), io_loop.RelayState(), stop) is False

    assert len(fake_telegram.calls) == 0
    assert _rows() == []


def test_unwritable_record_backs_off_the_post(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)

    def refused(*args: Any) -> None:
        raise OperationalError("could not extend file: No space left on device")

    # The photo is accepted, but the database refuses the record (e.g. a full disk).
    monkeypatch.setattr(lifecycle, "_record_post", refused)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    key = lifecycle.chart_key(a.pk, "post")

    assert _pass(clock, state) is True

    assert _rows() == []
    assert state.not_before == {key: NOON_05 + _seconds(30)}
    [line] = [r.getMessage() for r in caplog.records if r.name == LIFECYCLE_LOGGER]
    assert "1001" in line and "OperationalError" in line
    assert "No space" not in line
    # No post every pass while the record cannot be written: the step backoff applies.
    clock.set(NOON_05 + _seconds(29))
    assert _pass(clock, state) is False
    clock.set(NOON_05 + _seconds(30))
    assert _pass(clock, state) is True
    assert state.not_before == {key: NOON_05 + _seconds(30 + 60)}
    monkeypatch.undo()
    clock.set(NOON_05 + _seconds(90))
    assert _pass(clock, state) is True
    assert [row.message_id for row in _rows()] == [1003]
    assert key not in state.chart_failures


def test_web_process_never_imports_pillow() -> None:
    script = (
        "import sys, django; django.setup(); "
        "import powermon.models, powermon.urls, powermon.chart.models; "
        "print('PIL' in sys.modules, 'powermon.chart.render' in sys.modules, "
        "'powermon.chart.lifecycle' in sys.modules)"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": os.environ["DJANGO_SETTINGS_MODULE"]}

    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True, env=env
    )

    assert result.stdout.split() == ["False", "False", "False"]


def test_chart_state_and_logs_hold_no_token(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _monitored(location_factory)
    _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.fail_method(TOKEN_B, "sendPhoto", exc=_refused(TOKEN_B, "sendPhoto"))
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    for _ in range(3):
        _pass(clock, state)

    assert _requests(fake_telegram) == [
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
        ("B", "sendPhoto"),
    ]
    rows = repr(list(ChartMessage.objects.values()))
    locations, _ = lifecycle.read_snapshot(TODAY)
    assert len(locations) == 2
    texts = (caplog.text, rows, repr(state), repr(locations), str(_rows()[0]))
    for token in (DEFAULT_BOT_TOKEN, TOKEN_B):
        prefix, secret = token.split(":", 1)
        for text in texts:
            assert secret not in text
            assert f"{prefix}:" not in text
        for record in caplog.records:
            assert secret not in record.getMessage()
    assert "render_ms=" in caplog.text and "call_ms=" in caplog.text
