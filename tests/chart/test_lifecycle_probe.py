"""A degraded bot's chart post waits for a healthy getMe probe (quick task 261030-11v).

On 2026-10-29 Telegram timed out every call of the bots from 21:33 to 22:36 UTC. Each
ambiguous midnight ``sendPhoto`` was posted again on the step backoff, and Telegram had
delivered every one, so each channel got 6-7 copies of the same chart. Now:

- D-1, INV-16: the I/O loop keeps a per-bot "degraded" mark in memory
  (``RelayState.degraded``). A ``maybe_delivered`` or ``transient`` outcome of any of the
  bot's calls (alerts, ops notices, deletes, chart calls) sets it; an ``ok``, a permanent
  refusal or ``edit_target_missing`` clears it; a 429 and a request that never left the
  client change nothing. A fresh ``RelayState`` (a restart) has no degraded bot.
- D-1, INV-17: while the bot is degraded, a chart post (the one chart call that is not
  idempotent) is made only after a ``getMe`` probe answers ok. A failed probe defers the
  post like a failed step (``step_delay``, plus the bot hold of a bot-wide outcome) and
  sends no photo. The incident replay: refreshes time out before midnight, the midnight
  post waits for a healthy probe, then exactly one photo is posted, recorded and pinned.
- INV-14: a ``stop`` set while the probe is in flight sends no photo.

Every test runs ``io_loop.run_iteration(..., charts=True)`` and is
``django_db(transaction=True)``. Time comes only from the ``FakeClock``; Telegram is faked
at the HTTP boundary (``fake_telegram``); renders are real. ``_pass`` first moves the
detection cursor to the clock's now, as the worker's detection thread keeps it.
"""

import dataclasses
import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
import requests
import responses
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Greatest
from requests import PreparedRequest

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart import lifecycle, model
from powermon.chart.models import ChartMessage
from powermon.engine.models import SystemState
from powermon.telegram.client import SendResult
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# The incident's two days: the midnight between them is when today's post is due.
YESTERDAY = date(2026, 10, 1)
TODAY = date(2026, 10, 2)
SINCE = kyiv("2026-10-01 08:00")
NOON_05 = kyiv("2026-10-02 12:05")
LIFECYCLE_LOGGER = lifecycle.__name__
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
UNAUTHORIZED = {"ok": False, "error_code": 401, "description": "Unauthorized"}
GET_ME_OK = {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Fake bot"}}
TELEGRAM_KINDS = (
    "ok",
    "not_sent",
    "maybe_delivered",
    "rate_limited",
    "transient",
    "permanent",
    "edit_target_missing",
)


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _monitored(location_factory: Callable[..., Any], since: datetime = SINCE, **kw: Any) -> Any:
    """A location on since ``since``, with its open on piece (a monitored location)."""
    location = location_factory(**kw)
    monitor(location, since)
    return location


def _seed(
    location: Any, day: date, *, message_id: int, pinned: bool, rendered: datetime
) -> ChartMessage:
    """A record an earlier run left: posted by its location's bot, last drawn at ``rendered``."""
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=DEFAULT_CHAT_ID,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=rendered,
        created_at=rendered,
    )


def _accept(fake: Any, token: str, *methods: str) -> None:
    """Accept every call of these chart methods only; ``fail_method`` answers the others."""
    for method in methods:
        fake.answer_method(token, method, lambda: None)


def _detected(at: datetime) -> None:
    """Detection has completed its cycles up to ``at``: its cursor moves there, never back."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(at))
    )


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass, with detection caught up to the clock's now (``_detected``)."""
    _detected(clock.now())
    return io_loop.run_iteration(clock, state, charts=True)


def _run_until_idle(clock: FakeClock, state: io_loop.RelayState, limit: int = 20) -> int:
    """Run passes until one makes no call; return how many made a call."""
    for made in range(limit):
        if not _pass(clock, state):
            return made
    raise AssertionError(f"still making calls after {limit} passes")


def _requests(fake: Any, start: int = 0) -> list[str]:
    """The Bot API method of every request from index ``start`` on, failed ones too."""
    return [call.request.url.rsplit("/", 1)[1] for call in list(fake.calls)[start:]]


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


def _degraded(state: io_loop.RelayState) -> bool:
    return io_loop.bot_key(DEFAULT_BOT_TOKEN) in state.degraded


def _sec(n: float) -> timedelta:
    return timedelta(seconds=n)


# INV-17: the incident replay (the tracer)


def test_INV17_incident_replay_midnight_post_waits_for_a_healthy_probe(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = _monitored(location_factory)
    _seed(location, YESTERDAY, message_id=501, pinned=True, rendered=kyiv("2026-10-01 23:30"))
    # Telegram times out every edit and unpin of the bot, as from 21:33 UTC (registered
    # once, so the last answer repeats), and the first getMe probe; the post and the pin
    # go through. accept_chart is not used: it would accept the edits.
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "editMessageMedia", exc=requests.ReadTimeout())
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "unpinChatMessage", exc=requests.ReadTimeout())
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "getMe", exc=requests.ReadTimeout())
    fake_telegram.accept_probe(DEFAULT_BOT_TOKEN)
    _accept(fake_telegram, DEFAULT_BOT_TOKEN, "sendPhoto", "pinChatMessage")
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(kyiv("2026-10-01 23:50"))
    state = io_loop.RelayState()
    post_key = lifecycle.chart_key(location.pk, "post")

    # (A) Before midnight: the refresh times out, and the bot is marked degraded.
    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == ["editMessageMedia"]
    assert _degraded(state)

    # (B) Just after midnight today's post is due: the probe fails, so no photo is sent.
    t_b = kyiv("2026-10-02 00:00:30")
    clock.set(t_b)
    assert _pass(clock, state) is True
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 1
    assert state.not_before[post_key] == t_b + _sec(30)
    assert state.chart_failures[post_key] == 1
    [line] = [line for line in _lines(caplog) if "health probe" in line]
    assert "attempt 1" in line
    assert _degraded(state)
    # The idempotent steps (yesterday's unpin, its final edit) may run; still no photo.
    _run_until_idle(clock, state)
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 1

    # (C) After the step delay the probe answers ok: exactly one photo is posted.
    clock.set(t_b + _sec(30))
    assert _pass(clock, state) is True
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 2
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    today = ChartMessage.objects.get(local_date=TODAY, retired_at__isnull=True)
    assert today.message_id == 1001
    assert not _degraded(state)

    _run_until_idle(clock, state)
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    today.refresh_from_db()
    assert today.pinned is True
    assert any("chart probe for location" in r.getMessage() for r in caplog.records)


def test_INV17_healthy_bot_posts_without_a_probe(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    assert not _degraded(state)


def test_INV16_probe_5xx_holds_the_bot_and_defers_the_post(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "getMe", status=502, json_body=BAD_GATEWAY)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    state.degraded.add(io_loop.bot_key(DEFAULT_BOT_TOKEN))
    post_key = lifecycle.chart_key(location.pk, "post")

    assert _pass(clock, state) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    # A 5xx probe holds the whole bot as a chart 5xx would (2 s after 1 failure), and the
    # post waits its step delay on top.
    assert state.not_before[io_loop.bot_wide_key(DEFAULT_BOT_TOKEN)] == NOON_05 + _sec(2)
    assert state.not_before[post_key] == NOON_05 + _sec(2) + _sec(30)
    assert _degraded(state)


def test_INV16_probe_refused_clears_the_mark_and_the_next_post_needs_no_probe(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "getMe", status=401, json_body=UNAUTHORIZED)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    state.degraded.add(io_loop.bot_key(DEFAULT_BOT_TOKEN))
    post_key = lifecycle.chart_key(location.pk, "post")

    assert _pass(clock, state) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert state.not_before[post_key] == NOON_05 + _sec(30)
    # Telegram answered (a definite refusal): the bot is not degraded any more.
    assert not _degraded(state)

    clock.set(NOON_05 + _sec(30))
    assert _pass(clock, state) is True
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 1


def test_INV14_stop_set_during_the_probe_sends_no_photo(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    stop = threading.Event()

    def probe(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        stop.set()
        return 200, {}, json.dumps(GET_ME_OK)

    fake_telegram.rsps.add_callback(
        responses.POST,
        f"{fake_telegram.API}/bot{DEFAULT_BOT_TOKEN}/getMe",
        callback=probe,
        content_type="application/json",
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    state.degraded.add(io_loop.bot_key(DEFAULT_BOT_TOKEN))
    _detected(clock.now())

    assert io_loop.run_iteration(clock, state, stop, charts=True) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0


@pytest.mark.parametrize("initially_degraded", [False, True])
@pytest.mark.parametrize("kind", TELEGRAM_KINDS)
def test_INV16_note_bot_health_by_outcome(initially_degraded: bool, kind: Any) -> None:
    state = io_loop.RelayState()
    if initially_degraded:
        state.degraded.add(io_loop.bot_key(DEFAULT_BOT_TOKEN))

    io_loop.note_bot_health(state, DEFAULT_BOT_TOKEN, SendResult(kind))

    if kind in ("maybe_delivered", "transient"):
        expected = True
    elif kind in ("ok", "permanent", "edit_target_missing"):
        expected = False
    else:  # rate_limited, not_sent: Telegram said nothing about the bot's health
        expected = initially_degraded
    assert io_loop.is_degraded(state, DEFAULT_BOT_TOKEN) is expected
    # Another bot is never touched.
    assert not io_loop.is_degraded(state, "987654321:" + "B" * 35)


def test_INV16_a_restart_starts_with_every_bot_healthy() -> None:
    assert io_loop.RelayState().degraded == set()


def test_INV16_ambiguous_alert_makes_the_next_chart_post_probe(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    row = _queue_alert(location, NOON_05)
    fake_telegram.fail(DEFAULT_BOT_TOKEN, exc=requests.ReadTimeout())
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "getMe", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    row.refresh_from_db()
    # The alert stays uncertain and is never resent (ALRT-05); it only fed the mark.
    assert row.status == "uncertain"
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendMessage") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert _degraded(state)
    assert ChartMessage.objects.filter(local_date=model.local_today(NOON_05, KYIV)).count() == 0
