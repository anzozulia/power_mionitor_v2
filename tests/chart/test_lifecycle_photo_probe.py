"""A chart post waits for a successful photo re-upload while photo uploads fail (261103-b2z).

On 2026-11-03 from 00:43 UTC Telegram answered every photo upload of our bot
(``editMessageMedia``) with a 502 or a 30 s read timeout, while ``getMe``, ``sendMessage``
and the other methods answered in 0.1 s, so the getMe gate (261030-11v) would pass a blind
midnight ``sendPhoto``. Now:

- D-1, INV-16: the I/O loop keeps a per-bot photo-path mark in memory
  (``RelayState.photo_degraded``), fed only by photo-method outcomes (``sendPhoto``,
  ``editMessageMedia``): ``maybe_delivered`` or ``transient`` sets it, ``ok`` clears it,
  every other kind leaves it. It is independent of ``degraded``; a restart starts healthy.
- D-2, INV-17: while the bot is photo-degraded, a post is made only after a photo probe
  answers ok: one ``editMessageMedia`` that re-uploads the location's latest chart in its
  current chat and by its current bot with the picture it already shows. The probe writes
  nothing. A failed probe defers the post (``step_delay``, plus the bot hold of a bot-wide
  outcome) and sends no photo. With no candidate, or when Telegram says the candidate was
  deleted, the getMe probe runs instead; any other refusal defers the post.
- INV-14: a ``stop`` set while the photo probe is in flight sends no photo.

Every pass runs ``io_loop.run_iteration(..., charts=True)`` and is
``django_db(transaction=True)``. Time comes only from the ``FakeClock``; Telegram is faked
at the HTTP boundary (``fake_telegram``); renders are real unless a test wraps
``lifecycle.chart_content``.
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
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, parse_multipart
from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Greatest
from requests import PreparedRequest

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle, model
from powermon.chart.models import ChartMessage
from powermon.engine.models import SystemState
from powermon.telegram.client import SendResult
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

YESTERDAY = date(2026, 10, 1)
TODAY = date(2026, 10, 2)
SINCE = kyiv("2026-10-01 08:00")
NOON_05 = kyiv("2026-10-02 12:05")
LIFECYCLE_LOGGER = lifecycle.__name__
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
NOT_FOUND = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: message to edit not found",
}
OTHER_BOT = "987654321:" + "B" * 35
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
    location: Any,
    day: date,
    *,
    message_id: int,
    pinned: bool,
    rendered: datetime,
    finalized: datetime | None = None,
    unpinned: datetime | None = None,
    chat_id: int = DEFAULT_CHAT_ID,
    bot_key: str | None = None,
    retired: datetime | None = None,
    reset: datetime | None = None,
) -> ChartMessage:
    """A record an earlier run left; a None ``bot_key`` is its location's own bot."""
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=chat_id,
        bot_key=io_loop.bot_key(location.bot_token) if bot_key is None else bot_key,
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=rendered,
        finalized_at=finalized,
        unpinned_at=unpinned,
        retired_at=retired,
        history_reset_at=reset,
        created_at=rendered,
    )


def _finished_yesterday(location: Any) -> ChartMessage:
    """Yesterday's record, finalized and unpinned: at NOON_05 only today's post is due."""
    return _seed(
        location,
        YESTERDAY,
        message_id=501,
        pinned=False,
        rendered=kyiv("2026-10-02 00:02"),
        finalized=kyiv("2026-10-02 00:02"),
        unpinned=kyiv("2026-10-02 00:01"),
    )


def _row_fields(row: ChartMessage) -> tuple[Any, ...]:
    row.refresh_from_db()
    return (
        row.last_rendered_at,
        row.finalized_at,
        row.unpinned_at,
        row.redraw_requested_at,
        row.pinned,
        row.retired_at,
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


def _lines(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == LIFECYCLE_LOGGER and r.levelno == level
    ]


def _degraded(state: io_loop.RelayState) -> bool:
    return io_loop.bot_key(DEFAULT_BOT_TOKEN) in state.degraded


def _photo_degraded(state: io_loop.RelayState) -> bool:
    return io_loop.bot_key(DEFAULT_BOT_TOKEN) in state.photo_degraded


def _marked(*, photo: bool = True, bot: bool = True) -> io_loop.RelayState:
    state = io_loop.RelayState()
    if photo:
        state.photo_degraded.add(io_loop.bot_key(DEFAULT_BOT_TOKEN))
    if bot:
        state.degraded.add(io_loop.bot_key(DEFAULT_BOT_TOKEN))
    return state


def _caption(call: Any) -> str:
    """The caption of an accepted sendPhoto or editMessageMedia call."""
    if call.method == "editMessageMedia":
        return str(json.loads(call.fields["media"])["caption"])
    return str(call.fields["caption"])


def _today_message_id() -> int:
    return int(ChartMessage.objects.get(local_date=TODAY, retired_at__isnull=True).message_id)


def _sec(n: float) -> timedelta:
    return timedelta(seconds=n)


Recorder = list[tuple[date, datetime, bool]]


def _record_renders(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Wrap ``lifecycle.chart_content``: record (day, now, live) of every render."""
    original = lifecycle.chart_content
    seen: Recorder = []

    def recorder(
        location: Any, day: date, now: datetime, *, live: bool, tz: str
    ) -> tuple[bytes, str]:
        seen.append((day, now, live))
        return original(location, day, now, live=live, tz=tz)

    monkeypatch.setattr(lifecycle, "chart_content", recorder)
    return seen


# D-1: the photo mark


@pytest.mark.parametrize("initially", [False, True])
@pytest.mark.parametrize("kind", TELEGRAM_KINDS)
def test_INV16_note_photo_health_by_outcome(initially: bool, kind: Any) -> None:
    state = _marked(bot=False) if initially else io_loop.RelayState()

    io_loop.note_photo_health(state, DEFAULT_BOT_TOKEN, SendResult(kind))

    if kind in ("maybe_delivered", "transient"):
        expected = True
    elif kind == "ok":
        expected = False
    else:  # a 400/401/403, a 429 or an unsent request proves nothing about uploads
        expected = initially
    assert io_loop.is_photo_degraded(state, DEFAULT_BOT_TOKEN) is expected
    assert state.degraded == set()
    assert not io_loop.is_photo_degraded(state, OTHER_BOT)


def test_INV16_photo_mark_is_independent_of_the_degraded_mark() -> None:
    state = _marked()
    io_loop.note_bot_health(state, DEFAULT_BOT_TOKEN, SendResult("ok"))
    assert not _degraded(state)
    assert _photo_degraded(state)

    fresh = io_loop.RelayState()
    io_loop.note_photo_health(fresh, DEFAULT_BOT_TOKEN, SendResult("maybe_delivered"))
    assert _photo_degraded(fresh)
    assert not _degraded(fresh)

    # A restart starts healthy.
    assert io_loop.RelayState().photo_degraded == set()


# D-2: the post gate


def test_INV17_photo_probe_ok_reuploads_the_latest_chart_then_posts_once(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = _monitored(location_factory)
    yesterday = _finished_yesterday(location)
    before = _row_fields(yesterday)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = _marked()

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["editMessageMedia", "sendPhoto"]
    [probe] = [c for c in fake_telegram.chart_calls if c.method == "editMessageMedia"]
    assert probe.fields["chat_id"] == str(DEFAULT_CHAT_ID)
    assert probe.fields["message_id"] == "501"
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 0
    assert _today_message_id() == 1001
    assert not _degraded(state)
    assert not _photo_degraded(state)
    # The probe writes nothing.
    assert _row_fields(yesterday) == before
    assert OpsIncident.objects.count() == 0
    [line] = [
        line
        for line in _lines(caplog, logging.INFO)
        if line.startswith(f"chart photo probe for location {location.pk}: ok")
    ]
    assert "render_ms=" in line
    assert "call_ms=" in line


@pytest.mark.parametrize("failure", ["502", "timeout"])
def test_INV17_photo_probe_failure_defers_the_post_and_sends_no_photo(
    failure: str,
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    yesterday = _finished_yesterday(location)
    before = _row_fields(yesterday)
    if failure == "502":
        fake_telegram.fail_method(
            DEFAULT_BOT_TOKEN, "editMessageMedia", status=502, json_body=BAD_GATEWAY
        )
    else:
        fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "editMessageMedia", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = _marked()
    post_key = lifecycle.chart_key(location.pk, "post")
    bot_wide = io_loop.bot_wide_key(DEFAULT_BOT_TOKEN)

    assert _pass(clock, state) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 1
    assert state.chart_failures[post_key] == 1
    if failure == "502":
        assert state.not_before[bot_wide] == NOON_05 + _sec(2)
        assert state.not_before[post_key] == NOON_05 + _sec(32)
        delay = 32
    else:
        assert bot_wide not in state.not_before
        assert state.not_before[post_key] == NOON_05 + _sec(30)
        delay = 30
    [line] = [
        line for line in _lines(caplog, logging.WARNING) if "photo uploads are failing" in line
    ]
    assert "attempt 1" in line
    assert f"in {delay} s" in line
    assert _photo_degraded(state)
    assert _row_fields(yesterday) == before


def test_INV17_photo_probe_refused_falls_back_to_the_get_me_probe(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    yesterday = _finished_yesterday(location)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=NOT_FOUND
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = _marked()

    assert _pass(clock, state) is True

    # Telegram refused the candidate: the getMe probe decides, in the same step.
    assert _requests(fake_telegram) == ["editMessageMedia", "getMe", "sendPhoto"]
    assert _today_message_id() == 1001
    # The ok sendPhoto proves uploads work again: both marks are clear.
    assert not _photo_degraded(state)
    assert not _degraded(state)
    yesterday.refresh_from_db()
    assert yesterday.retired_at is None
    assert OpsIncident.objects.count() == 0


def test_INV17_photo_probe_refused_and_get_me_failing_sends_no_photo(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = _monitored(location_factory)
    _finished_yesterday(location)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=NOT_FOUND
    )
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "getMe", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = _marked()
    post_key = lifecycle.chart_key(location.pk, "post")

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["editMessageMedia", "getMe"]
    assert state.not_before[post_key] == NOON_05 + _sec(30)
    assert any("health probe" in line for line in _lines(caplog, logging.WARNING))
    assert _photo_degraded(state)
    assert _degraded(state)


@pytest.mark.parametrize(
    ("status", "description"),
    [(400, "Bad Request: IMAGE_PROCESS_FAILED"), (403, "Forbidden: bot is not a member")],
)
def test_INV17_photo_probe_refused_otherwise_defers_without_get_me(
    status: int,
    description: str,
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Only "message to edit not found" falls back to getMe. Any other refusal does not show
    # that uploads work, so the post waits and no photo is sent (verifier finding).
    location = _monitored(location_factory)
    yesterday = _finished_yesterday(location)
    before = _row_fields(yesterday)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN,
        "editMessageMedia",
        status=status,
        json_body={"ok": False, "error_code": status, "description": description},
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = _marked()
    post_key = lifecycle.chart_key(location.pk, "post")

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["editMessageMedia"]
    assert state.not_before[post_key] == NOON_05 + _sec(30)
    [line] = [
        line
        for line in _lines(caplog, logging.WARNING)
        if "Telegram refused the photo probe" in line
    ]
    assert "attempt 1" in line
    assert _photo_degraded(state)
    assert _row_fields(yesterday) == before


@pytest.mark.parametrize("also_degraded", [True, False])
def test_INV17_no_candidate_falls_back_to_the_get_me_probe(
    also_degraded: bool, location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = _marked(bot=also_degraded)

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["getMe", "sendPhoto"]
    assert not _photo_degraded(state)


def test_INV17_degraded_but_not_photo_degraded_uses_the_get_me_probe(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _finished_yesterday(location)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = _marked(photo=False)

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["getMe", "sendPhoto"]


def test_INV17_healthy_bot_posts_without_any_probe(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _finished_yesterday(location)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["sendPhoto"]


def test_INV14_stop_set_during_the_photo_probe_sends_no_photo(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _finished_yesterday(location)
    stop = threading.Event()

    def probe(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        stop.set()
        return 200, {}, json.dumps({"ok": True, "result": {"message_id": 501}})

    fake_telegram.rsps.add_callback(
        responses.POST,
        f"{fake_telegram.API}/bot{DEFAULT_BOT_TOKEN}/editMessageMedia",
        callback=probe,
        content_type="application/json",
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = _marked()
    _detected(clock.now())

    assert io_loop.run_iteration(clock, state, stop, charts=True) is True

    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0


def test_INV17_photo_probe_render_error_waits_15_min_and_calls_nothing(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _monitored(location_factory)
    _finished_yesterday(location)
    original = lifecycle.chart_content

    def broken(
        location: Any, day: date, now: datetime, *, live: bool, tz: str
    ) -> tuple[bytes, str]:
        if day == YESTERDAY:
            raise RuntimeError("boom")
        return original(location, day, now, live=live, tz=tz)

    monkeypatch.setattr(lifecycle, "chart_content", broken)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = _marked()
    post_key = lifecycle.chart_key(location.pk, "post")

    assert _pass(clock, state) is False

    assert list(fake_telegram.calls) == []
    assert state.not_before[post_key] == NOON_05 + io_loop.PERMANENT_BACKOFF
    [line] = [line for line in _lines(caplog, logging.ERROR) if "photo probe render failed" in line]
    assert "RuntimeError" in line
    assert "boom" not in line
    assert _photo_degraded(state)


# D-2: the candidate and its picture


def test_INV17_probe_candidate_is_the_latest_record_of_the_current_chat_and_bot(
    location_factory: Callable[..., Any],
) -> None:
    location = _monitored(location_factory)
    rendered = kyiv("2026-09-28 12:00")
    _seed(location, date(2026, 9, 28), message_id=301, pinned=False, rendered=rendered)
    _seed(location, date(2026, 9, 29), message_id=401, pinned=False, rendered=rendered)
    _seed(
        location,
        date(2026, 9, 30),
        message_id=601,
        pinned=False,
        rendered=rendered,
        chat_id=DEFAULT_CHAT_ID - 1,
    )
    _seed(
        location, YESTERDAY, message_id=602, pinned=False, rendered=rendered, bot_key="0123456789ab"
    )
    _seed(location, TODAY, message_id=603, pinned=False, rendered=rendered, retired=rendered)
    # A record a history reset marked is on its way out: never re-uploaded.
    _seed(location, TODAY, message_id=604, pinned=False, rendered=rendered, reset=rendered)
    other = _monitored(location_factory, name="Other", chat_id=DEFAULT_CHAT_ID - 7)
    _seed(other, date(2026, 9, 29), message_id=701, pinned=False, rendered=rendered)
    _seed(other, YESTERDAY, message_id=702, pinned=False, rendered=rendered, retired=rendered)
    locations, _ = lifecycle.read_snapshot(TODAY)
    by_id = {loc.location_id: loc for loc in locations}

    found = lifecycle._probe_candidate(by_id[location.pk])

    assert found is not None
    assert (found.message_id, found.local_date) == (401, date(2026, 9, 29))
    assert found.location_id == location.pk
    assert found.chat_id == DEFAULT_CHAT_ID
    # Another chat's record and a retired one are no candidates.
    assert lifecycle._probe_candidate(by_id[other.pk]) is None


def test_INV17_probe_draws_a_finished_record_as_its_finished_day(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _monitored(location_factory, since=kyiv("2026-09-30 08:00"))
    _finished_yesterday(location)
    seen = _record_renders(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = _marked()

    assert _pass(clock, state) is True

    [drawn] = [call for call in seen if call[0] == YESTERDAY]
    assert drawn == (YESTERDAY, model.next_midnight(YESTERDAY, KYIV), False)
    [probe] = [c for c in fake_telegram.chart_calls if c.method == "editMessageMedia"]
    assert _caption(probe) == "No outages on Thu 01.10"
    assert not any(day == TODAY and live is False for day, _, live in seen)


def test_INV17_probe_draws_an_unfinished_record_as_of_its_last_render(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _monitored(location_factory)
    last = kyiv("2026-10-01 23:30")
    yesterday = _seed(location, YESTERDAY, message_id=501, pinned=True, rendered=last)
    seen = _record_renders(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(kyiv("2026-10-02 00:00:30"))
    state = _marked()

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["editMessageMedia", "sendPhoto"]
    [drawn] = [call for call in seen if call[0] == YESTERDAY]
    assert drawn == (YESTERDAY, last, True)
    # The probe never finalizes early and writes nothing.
    yesterday.refresh_from_db()
    assert yesterday.finalized_at is None
    assert yesterday.last_rendered_at == last


# D-1: the normal photo steps feed the mark


def test_INV16_refresh_outcomes_set_and_clear_the_photo_mark(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, TODAY, message_id=700, pinned=True, rendered=NOON_05 - timedelta(minutes=20))
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=502, json_body=BAD_GATEWAY
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    refresh_key = lifecycle.chart_key(location.pk, "refresh")

    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == ["editMessageMedia"]
    assert _photo_degraded(state)

    clock.set(state.not_before[refresh_key])
    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == ["editMessageMedia", "editMessageMedia"]
    assert not _photo_degraded(state)


def test_INV16_a_pin_timeout_does_not_set_the_photo_mark(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, TODAY, message_id=700, pinned=False, rendered=NOON_05)
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "pinChatMessage", exc=requests.ReadTimeout())
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == ["pinChatMessage"]
    assert _degraded(state)
    assert not _photo_degraded(state)


# INV-17: the 2026-11-03 incident replay (the tracer)


def test_INV17_incident_replay_20261103_photo_uploads_fail_while_get_me_and_alerts_work(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = _monitored(location_factory)
    _seed(location, YESTERDAY, message_id=501, pinned=True, rendered=kyiv("2026-10-01 23:30"))
    up = [False]
    edits: list[int] = []

    def edit(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        fields, _ = parse_multipart(request)
        edits.append(int(fields["message_id"]))
        if not up[0]:
            # As on 2026-11-03: a 502 or a 30 s read timeout, in turn.
            if len(edits) % 2 == 1:
                return 502, {}, json.dumps(BAD_GATEWAY)
            raise requests.ReadTimeout()
        return 200, {}, json.dumps({"ok": True, "result": {"message_id": edits[-1]}})

    fake_telegram.rsps.add_callback(
        responses.POST,
        f"{fake_telegram.API}/bot{DEFAULT_BOT_TOKEN}/editMessageMedia",
        callback=edit,
        content_type="application/json",
    )
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_probe(DEFAULT_BOT_TOKEN)
    _accept(fake_telegram, DEFAULT_BOT_TOKEN, "sendPhoto", "pinChatMessage", "unpinChatMessage")
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(kyiv("2026-10-01 23:50"))
    state = io_loop.RelayState()
    post_key = lifecycle.chart_key(location.pk, "post")
    bot_wide = io_loop.bot_wide_key(DEFAULT_BOT_TOKEN)

    def photos() -> int:
        return int(fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto"))

    # (A) The evening: the refresh gets a 502, and both marks are set.
    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == ["editMessageMedia"]
    assert _degraded(state)
    assert _photo_degraded(state)
    # An alert goes out fine: the ok sendMessage clears ``degraded``, so the getMe gate
    # alone would now let the post through; the photo mark stays.
    alert = _queue_alert(location, kyiv("2026-10-01 23:50:10"))
    clock.set(kyiv("2026-10-01 23:50:10"))
    start = len(fake_telegram.calls)
    assert _pass(clock, state) is True
    assert _requests(fake_telegram, start) == ["sendMessage"]
    alert.refresh_from_db()
    assert alert.status == "sent"
    assert not _degraded(state)
    assert _photo_degraded(state)

    # (B) Midnight: the post's photo probe of yesterday's chart fails; no photo, no getMe.
    t_b = kyiv("2026-10-02 00:00:30")
    clock.set(t_b)
    assert _pass(clock, state) is True
    assert photos() == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "getMe") == 0
    assert edits[-1] == 501
    assert any("photo uploads are failing" in w for w in _lines(caplog, logging.WARNING))
    # Several passes and backoffs while Telegram keeps failing uploads: never a photo.
    for _ in range(12):
        if state.chart_failures[post_key] >= 4:
            break
        clock.set(max(state.not_before[post_key], state.not_before.get(bot_wide, t_b)))
        _run_until_idle(clock, state)
        assert photos() == 0
    assert state.chart_failures[post_key] >= 4
    assert photos() == 0

    # (C) Telegram recovers: one probe answers ok and exactly one photo follows.
    up[0] = True
    clock.set(max(state.not_before[post_key], state.not_before.get(bot_wide, t_b)))
    start = len(fake_telegram.calls)
    assert _pass(clock, state) is True
    assert _requests(fake_telegram, start) == ["editMessageMedia", "sendPhoto"]
    assert photos() == 1
    assert _today_message_id() == 1001
    assert not _degraded(state)
    assert not _photo_degraded(state)

    _run_until_idle(clock, state)
    assert photos() == 1
    today = ChartMessage.objects.get(local_date=TODAY, retired_at__isnull=True)
    assert today.pinned is True
    oks = [
        line
        for line in _lines(caplog, logging.INFO)
        if line.startswith(f"chart photo probe for location {location.pk}: ok")
    ]
    assert len(oks) == 1
