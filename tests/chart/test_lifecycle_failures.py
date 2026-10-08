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
- INV-20, F-04 (quick task 261008-vdk): a chart post, refresh or redraw Telegram refuses
  for good opens the location's ``chart_failing`` incident with exactly one
  ``ops_chart_failing`` notice, and the next one that works closes it with exactly one
  ``ops_chart_restored`` notice; transient, rate-limited, ambiguous and "message to edit
  not found" outcomes, and a deleted location, open nothing.
- INV-19: a pin that may have taken effect while the record says "not pinned" (an
  ambiguous answer, an unwritten outcome) never leaves an orphaned pin: every older record
  gets exactly one unpin by its stored message id before it leaves the lifecycle (Wave 4
  audit, fix 2).

Every test runs ``io_loop.run_iteration(..., charts=True)`` and is
``django_db(transaction=True)``. Time comes only from the ``FakeClock``; Telegram is faked
at the HTTP boundary (``fake_telegram``); renders are real. A day's final edit waits for
the detection cursor (INV-03), which the worker's detection thread keeps within a cycle of
now, so ``_pass`` first moves the cursor to the clock's now.
"""

import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
import requests
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, OPS_BOT_TOKEN, FakeClock
from django.db import OperationalError, transaction
from django.db.models import Value
from django.db.models.functions import Greatest

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle, model
from powermon.chart.models import ChartMessage
from powermon.engine.models import SystemState
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.telegram.client import SendResult
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# Fri 2026-10-02 12:05 local is "now"; the location has been monitored since 10-01 08:00.
TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-02 12:05")
SINCE = kyiv("2026-10-01 08:00")
# The ops_incident kind of a refused pin (lifecycle.KIND_CHART_PIN_FAILED).
PIN_INCIDENT = "chart_pin_failed"
# The ops_incident kind of a refused chart post or update (lifecycle.KIND_CHART_FAILING, F-04).
CHART_INCIDENT = "chart_failing"
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
NO_PHOTO_RIGHTS = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: not enough rights to send photos to the chat",
}
KICKED = {
    "ok": False,
    "error_code": 403,
    "description": "Forbidden: bot was kicked from the channel chat",
}
TOO_MANY = {
    "ok": False,
    "error_code": 429,
    "description": "Too Many Requests: retry after 7",
    "parameters": {"retry_after": 7},
}
# The ops notices of a refused chart post or update (F-04).
CHART_NOTICES = ("ops_chart_failing", "ops_chart_restored")
DISK_FULL = "could not extend file: No space left on device"
# The key of the bot that posted a kept chart photo (D-08): the default location's bot.
BOT_A = io_loop.bot_key(DEFAULT_BOT_TOKEN)


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
    """A record an earlier run left: posted by its location's bot 15 min before ``day`` ends."""
    at = model.next_midnight(day, KYIV) - timedelta(minutes=15)
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=DEFAULT_CHAT_ID,
        bot_key=io_loop.bot_key(location.bot_token),
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
    """One I/O pass, with the detection cursor moved to the clock's now (never back)."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
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


def _unpins(fake: Any) -> list[dict[str, Any]]:
    """The body of every unpinChatMessage request, failed ones too, in order."""
    return [
        json.loads(call.request.body)
        for call in fake.calls
        if call.request.url.endswith("/unpinChatMessage")
    ]


def _in_play(record: ChartMessage, today: date = TODAY) -> bool:
    """True while the lifecycle still reads the record (it may still make a call on it)."""
    _, rows = lifecycle.read_snapshot(today)
    return record.pk in {row.id for row in rows}


def _today() -> list[ChartMessage]:
    return list(ChartMessage.objects.filter(local_date=TODAY).order_by("id"))


def _active_today(day: date = TODAY) -> ChartMessage:
    """The day's one active (not retired) record."""
    return ChartMessage.objects.get(local_date=day, retired_at__isnull=True)


def _ops_rows(kind: str) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS, kind=kind)
    return list(rows.order_by("id"))


def _incidents(location: Any, kind: str = PIN_INCIDENT) -> list[tuple[datetime, datetime | None]]:
    rows = OpsIncident.objects.filter(kind=kind, location=location).order_by("id")
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
    assert state.chart_posted == {(location.pk, TODAY): (DEFAULT_CHAT_ID, 1001, NOON_05, BOT_A)}
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
    assert row.bot_key == BOT_A
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
    kept = {(location.pk, TODAY): (DEFAULT_CHAT_ID, 1001, NOON_05, BOT_A)}

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
    assert state.chart_posted == {(location.pk, YESTERDAY): (DEFAULT_CHAT_ID, 1001, late, BOT_A)}

    # Midnight passes before the flush: the kept post is still yesterday's chart. It gets
    # its one unpin (INV-19) at once; its final edit waits until detection has settled
    # past yesterday's end (INV-03).
    clock.set(kyiv("2026-10-02 00:00:20"))
    assert _run_until_idle(clock, state) == 3
    yesterday = ChartMessage.objects.get(local_date=YESTERDAY)
    assert (yesterday.message_id, yesterday.pinned, yesterday.finalized_at) == (1001, False, None)
    # 00:00 + 90 s (period + grace) + the lapse threshold (15 s).
    clock.set(kyiv("2026-10-02 00:01:45"))
    assert _run_until_idle(clock, state) == 1

    yesterday.refresh_from_db()
    assert yesterday.finalized_at == clock.now()
    assert _chart(fake_telegram) == [
        ("sendPhoto", None),
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
        ("unpinChatMessage", 1001),
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
    # The flow ends with exactly one pinned chart, today's; the older one was unpinned once.
    pinned = ChartMessage.objects.filter(pinned=True).values_list("message_id", flat=True)
    assert list(pinned) == [1001]
    assert _unpins(fake_telegram) == [{"chat_id": DEFAULT_CHAT_ID, "message_id": 501}]
    assert not _in_play(older)


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


# INV-20, F-04: a chart post or update refused for good gives one notice, and one on recovery


def _today_record(location: Any, rendered: datetime, **kw: Any) -> ChartMessage:
    """Today's pinned record, rendered at ``rendered``, as an earlier pass left it."""
    return ChartMessage.objects.create(
        location=location,
        local_date=TODAY,
        chat_id=DEFAULT_CHAT_ID,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=900,
        pinned=True,
        last_rendered_at=rendered,
        created_at=rendered,
        **kw,
    )


def _chart_notices() -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS, kind__in=CHART_NOTICES)
    return list(rows.order_by("id"))


def test_INV20_chart_post_refused_gives_one_failing_and_one_restored(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = _monitored(location_factory)
    # The bot may not post photos for the first three tries; then the admin grants it.
    for _ in range(3):
        fake_telegram.fail_method(
            DEFAULT_BOT_TOKEN, "sendPhoto", status=400, json_body=NO_PHOTO_RIGHTS
        )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    start = kyiv("2026-10-02 09:00")
    clock = FakeClock(start)
    state = io_loop.RelayState()
    channel = io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID)

    # 09:00, 09:15, 09:30: the post is refused each time; one incident, one notice.
    for minutes in (0, 15, 30):
        clock.set(start + _min(minutes))
        _run_until_idle(clock, state)
        assert _incidents(location, CHART_INCIDENT) == [(start, None)]
        [failing] = _ops_rows(outbox.KIND_OPS_CHART_FAILING)
        assert (failing.payload, failing.location_id, failing.recorded_at) == (
            {"http_status": 400},
            location.pk,
            start,
        )
        assert _ops_rows(outbox.KIND_OPS_CHART_RESTORED) == []
        assert ChartMessage.objects.filter(location=location).count() == 0
        assert channel not in state.not_before
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 3

    # 09:45: the right was granted; the post is accepted, recorded and pinned.
    restored_at = start + _min(45)
    clock.set(restored_at)
    _run_until_idle(clock, state)
    today_row = _active_today()
    assert (today_row.message_id, today_row.pinned) == (1001, True)
    assert _incidents(location, CHART_INCIDENT) == [(start, restored_at)]
    [restored] = _ops_rows(outbox.KIND_OPS_CHART_RESTORED)
    assert (restored.payload, restored.location_id, restored.recorded_at) == (
        {},
        location.pk,
        restored_at,
    )
    assert len(_ops_rows(outbox.KIND_OPS_CHART_FAILING)) == 1
    assert OutboxMessage.objects.get(pk=restored.pk).status == "sent"
    assert channel not in state.not_before
    assert lifecycle.KIND_CHART_FAILING == CHART_INCIDENT
    # The pin incident is a different one: never opened here.
    assert _incidents(location) == []


@pytest.mark.parametrize("step", ["refresh", "redraw"])
def test_INV20_chart_refresh_refused_opens_once_and_closes_on_the_next_refresh(
    step: str, location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    if step == "refresh":
        location = _monitored(location_factory)
        # Rendered before the 12:00 slot: a regular refresh is due at 12:05.
        record = _today_record(location, kyiv("2026-10-02 11:50"))
    else:
        # Hourly updates: no slot is due before 13:00, so only the removal's redraw is.
        location = _monitored(location_factory, chart_refresh_min=60)
        record = _today_record(location, NOON_05, redraw_requested_at=NOON_05)
    for _ in range(2):
        fake_telegram.fail_method(
            DEFAULT_BOT_TOKEN, "editMessageMedia", status=403, json_body=KICKED
        )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    for minutes in (0, 15):
        clock.set(NOON_05 + _min(minutes))
        _run_until_idle(clock, state)
        assert _incidents(location, CHART_INCIDENT) == [(NOON_05, None)]
        [failing] = _ops_rows(outbox.KIND_OPS_CHART_FAILING)
        assert (failing.payload, failing.location_id) == ({"http_status": 403}, location.pk)
        assert _ops_rows(outbox.KIND_OPS_CHART_RESTORED) == []
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 2

    restored_at = NOON_05 + _min(30)
    clock.set(restored_at)
    _run_until_idle(clock, state)
    assert _incidents(location, CHART_INCIDENT) == [(NOON_05, restored_at)]
    [restored] = _ops_rows(outbox.KIND_OPS_CHART_RESTORED)
    assert (restored.payload, restored.location_id) == ({}, location.pk)
    assert len(_ops_rows(outbox.KIND_OPS_CHART_FAILING)) == 1
    assert _chart(fake_telegram) == [("editMessageMedia", 900)]
    record.refresh_from_db()
    assert record.redraw_requested_at is None
    assert record.retired_at is None


@pytest.mark.parametrize("case", ["post_502", "post_429", "refresh_timeout", "refresh_target_gone"])
def test_chart_transient_or_missing_target_opens_no_chart_incident(
    case: str, location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    location = _monitored(location_factory)
    record = None
    if case == "post_502":
        fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "sendPhoto", status=502, json_body=BAD_GATEWAY)
    elif case == "post_429":
        fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "sendPhoto", status=429, json_body=TOO_MANY)
    else:
        record = _today_record(location, kyiv("2026-10-02 11:50"))
        if case == "refresh_timeout":
            fake_telegram.fail_method(
                DEFAULT_BOT_TOKEN, "editMessageMedia", exc=requests.ReadTimeout()
            )
        else:
            fake_telegram.fail_method(
                DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=EDIT_GONE
            )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    assert _incidents(location, CHART_INCIDENT) == []
    assert _chart_notices() == []
    if case == "refresh_target_gone":
        # "message to edit not found" still retires the record (INV-17 #3).
        assert record is not None
        record.refresh_from_db()
        assert record.retired_at == NOON_05


def test_chart_refused_for_a_deleted_location_opens_nothing(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    location = _monitored(location_factory)
    at = NOON_05
    assert actions.delete_location(location.pk, at) is True
    chart_location = lifecycle.ChartLocation(
        location.pk,
        location.name,
        location.language,
        location.bot_token,
        location.chat_id,
        timedelta(0),
    )

    lifecycle._chart_refused(chart_location, SendResult("permanent", code="http_403"), at)

    assert _incidents(location, CHART_INCIDENT) == []
    assert _chart_notices() == []


def test_chart_failing_close_error_does_not_repost(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._chart_working
    tries: list[int] = []

    def refused_once(*args: Any, **kwargs: Any) -> Any:
        tries.append(1)
        if len(tries) == 1:
            raise OperationalError(DISK_FULL)
        return real(*args, **kwargs)

    # The post is accepted and recorded, but closing the incident raises once.
    monkeypatch.setattr(lifecycle, "_chart_working", refused_once)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    _run_until_idle(clock, state)
    clock.set(NOON_05 + _min(1))
    _run_until_idle(clock, state)

    assert tries
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    [row] = ChartMessage.objects.filter(location=location, local_date=TODAY)
    assert (row.message_id, row.pinned) == (1001, True)
    assert state.chart_posted == {}


# The chart's backoff maps stay bounded (Wave 3 audit, fix B)


def test_INV17_1_chart_keys_stay_bounded_across_days(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory, since=kyiv("2026-10-01 00:00"))
    # The bot can post and edit but never pin or unpin (INV-17 #1), for days on end.
    for method in ("pinChatMessage", "unpinChatMessage"):
        fake_telegram.fail_method(DEFAULT_BOT_TOKEN, method, status=400, json_body=NO_PIN_RIGHTS)
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
        # Post today's chart and fail to pin it; from day 3 on, finalize yesterday's and
        # make its one unpin attempt (INV-19), refused and so done (best effort).
        assert _run_until_idle(clock, state) == (2 if day == 2 else 4)
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
    # One unpin attempt per older record, never repeated.
    assert [body["message_id"] for body in _unpins(fake_telegram)] == [1001, 1002, 1003]


# INV-19: a pin that may have taken effect never leaves an orphan (Wave 4 audit, fix 2)


def _pin_answered_just_before_midnight(clock: FakeClock, state: io_loop.RelayState) -> Any:
    """Post today's (10-01's) chart at 23:59:45, then make its pin call at 23:59:50."""
    clock.set(kyiv("2026-10-01 23:59:45"))
    assert _pass(clock, state) is True
    clock.set(kyiv("2026-10-01 23:59:50"))
    assert _pass(clock, state) is True
    record = ChartMessage.objects.get(local_date=YESTERDAY)
    # The record does not know the pin took effect.
    assert (record.message_id, record.pinned) == (1001, False)
    return record


def _after_midnight_unpinned_once(
    fake: Any, clock: FakeClock, state: io_loop.RelayState, record: ChartMessage
) -> None:
    """After midnight the older record gets exactly one unpin, then leaves the lifecycle."""
    # Today's chart is posted and pinned, and the older one is unpinned by its stored
    # chat and message id, before its pin was ever retried (its day has not settled yet).
    clock.set(kyiv("2026-10-02 00:00:05"))
    assert _run_until_idle(clock, state) == 3
    assert _unpins(fake) == [{"chat_id": DEFAULT_CHAT_ID, "message_id": 1001}]
    # Its final edit follows once its day has settled; then it is done.
    clock.set(kyiv("2026-10-02 00:01:50"))
    assert _run_until_idle(clock, state) == 1
    for minutes in (5, 10, 14):
        clock.set(kyiv("2026-10-02 00:00") + _min(minutes))
        assert _pass(clock, state) is False
    record.refresh_from_db()
    assert record.pinned is False
    assert record.finalized_at == kyiv("2026-10-02 00:01:50")
    assert not _in_play(record)
    assert fake.count(DEFAULT_BOT_TOKEN, "unpinChatMessage") == 1
    # At most one pinned chart: today's.
    pinned = ChartMessage.objects.filter(pinned=True).values_list("local_date", "message_id")
    assert list(pinned) == [(TODAY, 1002)]


def test_INV19_ambiguous_pin_before_midnight_is_unpinned_once(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    # The pin's answer times out after the request was sent: it may have taken effect.
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "pinChatMessage", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(kyiv("2026-10-01 23:59:45"))
    state = io_loop.RelayState()

    record = _pin_answered_just_before_midnight(clock, state)
    _after_midnight_unpinned_once(fake_telegram, clock, state, record)

    assert _chart(fake_telegram) == [
        ("sendPhoto", None),
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
        ("unpinChatMessage", 1001),
        ("editMessageMedia", 1001),
    ]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "pinChatMessage") == 2


def test_INV19_pin_whose_record_was_not_written_is_unpinned_once(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._pinned
    tries: list[int] = []

    def unwritten_once(*args: Any, **kwargs: Any) -> Any:
        tries.append(1)
        if len(tries) == 1:
            raise OperationalError(DISK_FULL)
        return real(*args, **kwargs)

    # Telegram pins the chart, but the database refuses the record's UPDATE once.
    monkeypatch.setattr(lifecycle, "_pinned", unwritten_once)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(kyiv("2026-10-01 23:59:45"))
    state = io_loop.RelayState()

    record = _pin_answered_just_before_midnight(clock, state)
    assert "not written" in _lines(caplog)[0]
    _after_midnight_unpinned_once(fake_telegram, clock, state, record)

    assert _chart(fake_telegram) == [
        ("sendPhoto", None),
        ("pinChatMessage", 1001),
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
        ("unpinChatMessage", 1001),
        ("editMessageMedia", 1001),
    ]


def test_INV19_pinned_chart_is_unpinned_exactly_once(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    older = _seed(location, YESTERDAY, message_id=501, pinned=True)
    ChartMessage.objects.create(
        location=location,
        local_date=TODAY,
        chat_id=DEFAULT_CHAT_ID,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=900,
        pinned=True,
        last_rendered_at=kyiv("2026-10-02 00:00:02"),
        created_at=kyiv("2026-10-02 00:00:02"),
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(kyiv("2026-10-02 00:00:05"))
    state = io_loop.RelayState()

    # The unpin goes at once; the final edit once the day has settled; nothing after.
    assert _run_until_idle(clock, state) == 1
    for at in ("00:01:50", "00:05", "00:10", "00:14:59"):
        clock.set(kyiv(f"2026-10-02 {at}"))
        _run_until_idle(clock, state)

    assert _chart(fake_telegram) == [("unpinChatMessage", 501), ("editMessageMedia", 501)]
    older.refresh_from_db()
    assert (older.pinned, older.finalized_at) == (False, kyiv("2026-10-02 00:01:50"))
    assert not _in_play(older)
    pinned = ChartMessage.objects.filter(pinned=True).values_list("message_id", flat=True)
    assert list(pinned) == [900]


def test_INV19_a_chart_pinned_again_after_its_unpin_owes_a_new_one(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # The wall clock stepped back across midnight: a chart already unpinned is today's
    # again, so it is pinned again, and then it must be unpinned again after midnight.
    location = _monitored(location_factory)
    record = ChartMessage.objects.create(
        location=location,
        local_date=TODAY,
        chat_id=DEFAULT_CHAT_ID,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=900,
        pinned=False,
        last_rendered_at=NOON_05,
        unpinned_at=NOON_05,
        created_at=NOON_05,
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    record.refresh_from_db()
    assert (record.pinned, record.unpinned_at) == (True, None)
    clock.set(kyiv("2026-10-03 00:00:05"))
    assert _run_until_idle(clock, state) == 3

    assert _chart(fake_telegram) == [
        ("pinChatMessage", 900),
        ("sendPhoto", None),
        ("pinChatMessage", 1001),
        ("unpinChatMessage", 900),
    ]
    record.refresh_from_db()
    assert (record.pinned, record.unpinned_at) == (False, kyiv("2026-10-03 00:00:05"))
