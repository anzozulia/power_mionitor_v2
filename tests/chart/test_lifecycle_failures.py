"""Today's chart through deletion, ambiguous posts, database errors and pin refusals.

- INV-17 #3, D-06: a chart deleted in the channel ("message to edit / pin not found") is
  retired, and exactly one replacement is posted, recorded and pinned; the retired record
  is never called again.
- D-06: an ambiguous sendPhoto writes no record and is posted again after the step delay;
  only a photo Telegram answered with its message id is recorded, pinned or edited.
- WR-04 analogue: a post Telegram accepted whose record cannot be written is kept in
  ``RelayState.chart_posted`` and written first by the next chart step, so it is never
  posted twice (Wave 3 audit, fix A).
- INV-17 #1, D-07: a bot that can post but not pin keeps the chart recorded and refreshed
  every 15 min, retries the pin after each refresh, and tells the admin exactly once when
  pinning starts failing and once when it works again, through the ``chart_pin_failed``
  incident; the channel's alerts are never held.
- The chart's backoff maps stay bounded: a row's step keys are dropped once that record
  no longer needs the step (Wave 3 audit, fix B).

Every test runs ``io_loop.run_iteration(..., charts=True)`` and is
``django_db(transaction=True)``. Time comes only from the ``FakeClock``; Telegram is faked
at the HTTP boundary (``fake_telegram``); renders are real.
"""

import dataclasses
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
import requests
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, OPS_BOT_TOKEN, FakeClock
from django.db import OperationalError, transaction

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle, model
from powermon.chart.models import ChartMessage
from powermon.locations.models import Location
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# Fri 2026-10-02 12:05 local is "now"; the location has been monitored since 10-01 08:00.
TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-02 12:05")
SINCE = kyiv("2026-10-01 08:00")
# The ops_incident kind of a refused pin (lifecycle.KIND_CHART_PIN_FAILED).
PIN_INCIDENT = "chart_pin_failed"
LIFECYCLE_LOGGER = lifecycle.__name__
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
BOTS = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B", OPS_BOT_TOKEN: "ops"}
EDIT_GONE = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: message to edit not found",
}
PIN_GONE = {"ok": False, "error_code": 400, "description": "Bad Request: message to pin not found"}
NO_PIN_RIGHTS = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: not enough rights to manage pinned messages in the chat",
}
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
DISK_FULL = "could not extend file: No space left on device"


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _monitored(location_factory: Callable[..., Any], since: datetime = SINCE, **kw: Any) -> Any:
    """A location on since ``since``, with its open on piece (a monitored location)."""
    location = location_factory(**kw)
    monitor(location, since)
    return location


def _seed(location: Any, day: date, *, message_id: int, pinned: bool) -> ChartMessage:
    """A record an earlier run left: posted 15 min before the end of ``day``."""
    at = model.next_midnight(day, KYIV) - timedelta(minutes=15)
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=DEFAULT_CHAT_ID,
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=at,
        created_at=at,
    )


def _accept(fake: Any, token: str, *methods: str) -> None:
    """Accept every call of these chart methods only; ``fail_method`` answers the others."""
    for method in methods:
        fake.answer_method(token, method, lambda: None)


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    return io_loop.run_iteration(clock, state, charts=True)


def _run_until_idle(clock: FakeClock, state: io_loop.RelayState, limit: int = 20) -> int:
    """Run passes until one makes no call; return how many made a call."""
    for made in range(limit):
        if not _pass(clock, state):
            return made
    raise AssertionError(f"still making calls after {limit} passes")


def _requests(fake: Any) -> list[tuple[str, str]]:
    """(bot label, Bot API method) of every request, failed ones too, in order."""
    out = []
    for call in fake.calls:
        token, method = call.request.url.split("/bot", 1)[1].split("/", 1)
        out.append((BOTS[token], method))
    return out


def _chart(fake: Any) -> list[tuple[str, int | None]]:
    """(method, message id) of every accepted chart call, in order."""
    out = []
    for call in fake.chart_calls:
        message_id = call.fields.get("message_id")
        out.append((call.method, None if message_id is None else int(message_id)))
    return out


def _today() -> list[ChartMessage]:
    return list(ChartMessage.objects.filter(local_date=TODAY).order_by("id"))


def _active_today(day: date = TODAY) -> ChartMessage:
    """The day's one active (not retired) record."""
    return ChartMessage.objects.get(local_date=day, retired_at__isnull=True)


def _ops_rows(kind: str) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS, kind=kind)
    return list(rows.order_by("id"))


def _incidents(location: Any) -> list[tuple[datetime, datetime | None]]:
    rows = OpsIncident.objects.filter(kind=PIN_INCIDENT, location=location).order_by("id")
    return [(row.started_at, row.ended_at) for row in rows]


def _queue_alert(location: Any, at: datetime) -> OutboxMessage:
    """An OFF alert recorded (and so due) at ``at``, as a transition would queue it."""
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - timedelta(seconds=91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LIFECYCLE_LOGGER and r.levelno >= logging.WARNING
    ]


def _chart_keys(keys: Any) -> set[str]:
    return {key for key in keys if key.startswith("chart:")}


def _sec(n: float) -> timedelta:
    return timedelta(seconds=n)


def _min(n: float) -> timedelta:
    return timedelta(minutes=n)


# INV-17 #3, D-06: a deleted chart is replaced exactly once


def test_INV17_3_deleted_chart_is_replaced_once(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    # The first edit finds the chart deleted in the channel; later edits are accepted.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=EDIT_GONE
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    assert _run_until_idle(clock, state) == 2
    [first] = _today()
    assert (first.message_id, first.pinned) == (1001, True)

    clock.set(NOON_05 + _min(15))
    assert _pass(clock, state) is True
    first.refresh_from_db()
    assert (first.retired_at, first.pinned) == (clock.now(), False)
    # The next passes post exactly one replacement, record it and pin it.
    assert _run_until_idle(clock, state) == 2
    replacement = _active_today()
    assert (replacement.message_id, replacement.pinned) == (1002, True)
    # The retired record is never called again, over three more refresh cycles.
    for minutes in (30, 45, 60):
        clock.set(NOON_05 + _min(minutes))
        assert _run_until_idle(clock, state) == 1

    assert _chart(fake_telegram) == [
        ("sendPhoto", None),
        ("pinChatMessage", 1001),
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
        ("editMessageMedia", 1002),
        ("editMessageMedia", 1002),
        ("editMessageMedia", 1002),
    ]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 2
    # The one refused edit named 1001 (D-06: today's chart, so it is replaced).
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 4
    assert [(r.message_id, r.retired_at is None) for r in _today()] == [(1001, False), (1002, True)]


def test_pin_target_gone_is_replaced_once(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "pinChatMessage", status=400, json_body=PIN_GONE)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _run_until_idle(clock, state) == 4

    assert _requests(fake_telegram) == [
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
    ]
    assert _chart(fake_telegram) == [
        ("sendPhoto", None),
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
    ]
    first, replacement = _today()
    assert (first.message_id, first.retired_at, first.pinned) == (1001, NOON_05, False)
    assert (replacement.message_id, replacement.retired_at, replacement.pinned) == (
        1002,
        None,
        True,
    )
    assert state.chart_failures == {}


# D-06: an ambiguous post is posted again; only a known message is recorded


def test_D06_ambiguous_post_is_posted_again(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = _monitored(location_factory)
    # The first sendPhoto may have reached Telegram: the answer timed out.
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "sendPhoto", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    key = lifecycle.chart_key(location.pk, "post")

    assert _pass(clock, state) is True
    assert _today() == []
    assert state.not_before == {key: NOON_05 + _sec(30)}
    clock.set(NOON_05 + _sec(29))
    assert _pass(clock, state) is False
    clock.set(NOON_05 + _sec(30))
    assert _run_until_idle(clock, state) == 2

    # Only the answered post is recorded and pinned; nothing names the ambiguous one.
    [row] = _today()
    assert (row.message_id, row.pinned, row.last_rendered_at) == (1001, True, NOON_05 + _sec(30))
    assert _chart(fake_telegram) == [("sendPhoto", None), ("pinChatMessage", 1001)]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 2
    [line] = _lines(caplog)
    assert "posted again" in line and "attempt 1" in line
    assert state.chart_failures == {}


# WR-04 analogue: a post whose record cannot be written is kept, never posted twice


def test_post_kept_after_a_db_error_is_recorded_without_a_second_photo(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._record_post
    tries: list[int] = []

    def refused_once(*args: Any, **kwargs: Any) -> Any:
        tries.append(1)
        if len(tries) == 1:
            raise OperationalError(DISK_FULL)
        return real(*args, **kwargs)

    # The photo is accepted, but the database refuses its record once (e.g. a full disk).
    monkeypatch.setattr(lifecycle, "_record_post", refused_once)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    assert _today() == []
    assert state.chart_posted == {(location.pk, TODAY): (DEFAULT_CHAT_ID, 1001, NOON_05)}
    # The post is known, not failed: no backoff, no failure count.
    assert state.not_before == {}
    assert state.chart_failures == {}
    [line] = _lines(caplog)
    assert "1001" in line and "OperationalError" in line
    assert "No space" not in line
    # The next pass writes the kept post first, then pins it: no second photo.
    assert _pass(clock, state) is True
    assert state.chart_posted == {}
    [row] = _today()
    assert (row.message_id, row.chat_id, row.pinned) == (1001, DEFAULT_CHAT_ID, True)
    assert (row.last_rendered_at, row.created_at) == (NOON_05, NOON_05)
    assert _chart(fake_telegram) == [("sendPhoto", None), ("pinChatMessage", 1001)]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1


def test_kept_post_flush_error_makes_no_call(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._record_post
    tries: list[int] = []

    def refused_twice(*args: Any, **kwargs: Any) -> Any:
        tries.append(1)
        if len(tries) <= 2:
            raise OperationalError(DISK_FULL)
        return real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_post", refused_twice)
    caplog.set_level(logging.WARNING)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    kept = {(location.pk, TODAY): (DEFAULT_CHAT_ID, 1001, NOON_05)}

    assert _pass(clock, state) is True
    assert state.chart_posted == kept
    # The flush fails again: the step ends before any choice, with no Telegram call.
    clock.set(NOON_05 + _sec(40))
    assert _pass(clock, state) is False
    assert state.chart_posted == kept
    assert "chart step failed: OperationalError" in caplog.text
    assert len(fake_telegram.calls) == 1
    # Once the database writes again, the kept post is recorded and pinned.
    assert _pass(clock, state) is True

    assert state.chart_posted == {}
    assert _chart(fake_telegram) == [("sendPhoto", None), ("pinChatMessage", 1001)]
    [row] = _today()
    assert (row.message_id, row.pinned, row.last_rendered_at) == (1001, True, NOON_05)


def test_kept_post_of_an_earlier_day_is_recorded_for_that_day(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._record_post
    tries: list[int] = []

    def refused_once(*args: Any, **kwargs: Any) -> Any:
        tries.append(1)
        if len(tries) == 1:
            raise OperationalError(DISK_FULL)
        return real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_post", refused_once)
    late = kyiv("2026-10-01 23:59:50")
    clock = FakeClock(late)
    state = io_loop.RelayState()
    assert _pass(clock, state) is True
    assert state.chart_posted == {(location.pk, YESTERDAY): (DEFAULT_CHAT_ID, 1001, late)}

    # Midnight passes before the flush: the kept post is still yesterday's chart.
    clock.set(kyiv("2026-10-02 00:00:20"))
    assert _run_until_idle(clock, state) == 3

    yesterday = ChartMessage.objects.get(local_date=YESTERDAY)
    assert (yesterday.message_id, yesterday.pinned) == (1001, False)
    assert yesterday.finalized_at == clock.now()
    assert _chart(fake_telegram) == [
        ("sendPhoto", None),
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
        ("editMessageMedia", 1001),
    ]


def test_kept_post_of_a_deleted_location_is_dropped(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gone = _monitored(location_factory)
    other = _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    real = lifecycle._record_post
    tries: list[int] = []

    def refused_once(*args: Any, **kwargs: Any) -> Any:
        tries.append(1)
        if len(tries) == 1:
            raise OperationalError(DISK_FULL)
        return real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_post", refused_once)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    assert _pass(clock, state) is True
    assert list(state.chart_posted) == [(gone.pk, TODAY)]

    # The location row is deleted before the kept post is written: its record can never
    # be written, so the photo stays untracked and the other location's work goes on.
    Location.objects.filter(pk=gone.pk).delete()
    assert _pass(clock, state) is True

    assert state.chart_posted == {}
    assert [(row.location_id, row.message_id) for row in _today()] == [(other.pk, 1002)]
    assert "1001" in _lines(caplog)[-1] and "untracked" in _lines(caplog)[-1]


def test_pin_failure_notice_names_the_http_status() -> None:
    assert lifecycle._http_status("http_400") == 400
    assert lifecycle._http_status("http_403") == 403
    # A code with no plausible status falls back to 400 (the notice needs 100..599).
    for code in ("", "permanent", "http_", "http_99", "http_600", "http_4x0", "http_٤٠٠"):
        assert lifecycle._http_status(code) == 400


# INV-17 #1, D-07: the bot can post but not pin


def test_INV17_1_bot_can_post_but_not_pin(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = _monitored(location_factory)
    older = _seed(location, YESTERDAY, message_id=501, pinned=True)
    # The bot may not pin for the first three tries; then the admin grants the right.
    for _ in range(3):
        fake_telegram.fail_method(
            DEFAULT_BOT_TOKEN, "pinChatMessage", status=400, json_body=NO_PIN_RIGHTS
        )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    start = kyiv("2026-10-02 09:00")
    clock = FakeClock(start)
    state = io_loop.RelayState()
    channel = io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID)

    # 09:00: the chart is posted and recorded; the pin is refused.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    today_row = _active_today()
    assert (today_row.message_id, today_row.pinned, today_row.pin_failed_at) == (1001, False, start)
    assert _incidents(location) == [(start, None)]
    [failed] = _ops_rows(outbox.KIND_OPS_PIN_FAILED)
    assert (failed.payload, failed.location_id, failed.recorded_at) == (
        {"http_status": 400},
        location.pk,
        start,
    )
    # The channel's alerts are never held: an alert queued now goes out in its next pass,
    # with the ops notice; the older chart is still finalized and unpinned (D-02).
    off = _queue_alert(location, start)
    assert _pass(clock, state) is True
    assert OutboxMessage.objects.get(pk=off.pk).status == "sent"
    assert OutboxMessage.objects.get(pk=failed.pk).status == "sent"
    assert _run_until_idle(clock, state) == 1
    older.refresh_from_db()
    assert (older.finalized_at, older.pinned) == (start, False)
    assert channel not in state.not_before

    # 09:15 and 09:30: the refresh keeps its cadence, the pin is retried right after it
    # and refused again, with no second notice and still one open incident.
    for minutes in (15, 30):
        clock.set(start + _min(minutes))
        assert _run_until_idle(clock, state) == 2
        today_row.refresh_from_db()
        assert (today_row.last_rendered_at, today_row.pin_failed_at) == (clock.now(), clock.now())
        assert today_row.pinned is False
        assert len(_ops_rows(outbox.KIND_OPS_PIN_FAILED)) == 1
        assert _incidents(location) == [(start, None)]
        assert channel not in state.not_before

    # 09:45: the right was granted; after the refresh the pin goes through.
    restored_at = start + _min(45)
    clock.set(restored_at)
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    today_row.refresh_from_db()
    assert (today_row.pinned, today_row.pin_failed_at) == (True, None)
    assert _incidents(location) == [(start, restored_at)]
    [restored] = _ops_rows(outbox.KIND_OPS_PIN_RESTORED)
    assert (restored.payload, restored.location_id) == ({}, location.pk)
    assert _pass(clock, state) is True
    assert OutboxMessage.objects.get(pk=restored.pk).status == "sent"
    assert _pass(clock, state) is False

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "pinChatMessage") == 4
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 4
    assert len(_ops_rows(outbox.KIND_OPS_PIN_FAILED)) == 1
    assert channel not in state.not_before
    assert lifecycle.KIND_CHART_PIN_FAILED == PIN_INCIDENT


def test_pin_transient_error_does_not_open_an_incident(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "pinChatMessage", status=502, json_body=BAD_GATEWAY
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert _pass(clock, state) is True

    today_row = _active_today()
    assert state.not_before == {
        io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): NOON_05 + _sec(2),
        lifecycle.chart_key(location.pk, "pin", today_row.pk): NOON_05 + _sec(2 + 30),
    }
    assert (today_row.pinned, today_row.pin_failed_at) == (False, None)
    assert _incidents(location) == []
    assert _ops_rows(outbox.KIND_OPS_PIN_FAILED) == []


# The chart's backoff maps stay bounded (Wave 3 audit, fix B)


def test_INV17_1_chart_keys_stay_bounded_across_days(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory, since=kyiv("2026-10-01 00:00"))
    # The bot can post and edit but never pin (INV-17 #1), for days on end.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "pinChatMessage", status=400, json_body=NO_PIN_RIGHTS
    )
    _accept(fake_telegram, DEFAULT_BOT_TOKEN, "sendPhoto", "editMessageMedia")
    state = io_loop.RelayState()
    # Keys of the alert relay are never touched by the chart's pruning.
    other_chat = io_loop.chat_key("111:" + "X" * 35, -100111)
    admin = io_loop.ops_key(OPS_BOT_TOKEN)
    far = kyiv("2026-11-01 00:00")
    state.not_before.update({other_chat: far, admin: far})
    clock = FakeClock(kyiv("2026-10-02 00:05"))

    for day in range(2, 6):
        clock.set(kyiv(f"2026-10-0{day} 00:05"))
        # Post today's chart and fail to pin it; finalize yesterday's (from day 3 on).
        assert _run_until_idle(clock, state) == (2 if day == 2 else 3)
        clock.advance(minutes=15)
        # The refresh, then the pin retried and refused again.
        assert _run_until_idle(clock, state) == 2
        today_row = _active_today(date(2026, 10, day))
        pin_key = lifecycle.chart_key(location.pk, "pin", today_row.pk)
        # Only today's record's pin step is still in play: older records' keys are gone.
        assert set(state.chart_failures) == {pin_key}
        assert _chart_keys(state.not_before) == {pin_key}
        assert state.not_before[other_chat] == far and state.not_before[admin] == far

    assert ChartMessage.objects.filter(finalized_at__isnull=False).count() == 3
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "pinChatMessage") == 8
