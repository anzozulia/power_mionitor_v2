"""Today's chart is redrawn right after an outage removal (261006-qv7, DATA-02 amended; D7).

The removal marks today's active record (``redraw_requested_at``) in its transaction. The
worker's next pass with no alert and no delete to make redraws today's chart in place, as
of the time of its last update: ``min(last_rendered_at, now)``. So the now pill keeps the
time the image already showed, the removed outage is drawn as on, ``last_rendered_at`` is
unchanged, and the next regular update comes on schedule. The mark is cleared only if it
is still the value the pass saw, so a removal during the redraw gets its own redraw; a
failed redraw keeps it. When a slot is due anyway, one regular refresh at ``now`` clears
the mark. Finished charts of earlier days are never redrawn (D-14, owner default 1).

The owner's example (chart-spec §10): refresh answered at 18:30:00.8, period 15 min,
removal at 18:43 -> the redraw's pill shows 18:30, and the 18:45 update shows 18:45.

The planner cases are pure ``plan`` calls; the rest run I/O passes on PostgreSQL
(``django_db(transaction=True)``: a pass calls ``close_old_connections()``), with the
injected ``FakeClock``, Telegram faked at the HTTP boundary and real renders.
"""

import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
import requests
from chart_fixtures import KYIV, insert_pieces, kyiv, local_pieces, set_status
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, FakeTelegram
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import DatabaseError
from django.test import RequestFactory
from PIL import Image

from powermon.alerts import ops
from powermon.alerts.models import OutboxMessage
from powermon.chart import lifecycle, render
from powermon.chart.model import Week
from powermon.chart.models import ChartMessage
from powermon.engine import history, transitions
from powermon.engine.models import SystemState
from powermon.i18n import chart_texts
from powermon.web.history_views import OutageRemoveView
from powermon.worker import detection, io_loop

DB = pytest.mark.django_db(transaction=True)
TODAY = date(2026, 10, 1)
YESTERDAY = date(2026, 9, 30)
SUB = timedelta(milliseconds=800)
# The owner's example: the regular refresh was answered at 18:30:00.8, period 15 min.
RENDERED = kyiv("2026-10-01 18:30") + SUB
REMOVED = kyiv("2026-10-01 18:43")
LIFECYCLE_LOGGER = lifecycle.__name__
TOKEN_B = "987654321:" + "B" * 35
GONE = {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
NO_OUTAGES = chart_texts.live_caption(0, 0, "en")


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


# Pure planner cases


def _location(location_id: int, *, refresh_min: int = 15) -> lifecycle.ChartLocation:
    return lifecycle.ChartLocation(
        location_id,
        f"L{location_id}",
        "en",
        DEFAULT_BOT_TOKEN,
        DEFAULT_CHAT_ID,
        lifecycle.settle_time(60, 30, False),
        refresh_min=refresh_min,
    )


def _row(
    row_id: int,
    location_id: int,
    *,
    rendered: datetime,
    redraw: datetime | None = None,
    day: date = TODAY,
    chat_id: int = DEFAULT_CHAT_ID,
    done: bool = False,
) -> lifecycle.ChartRow:
    """A pinned record; ``done`` marks an older one finalized and unpinned already."""
    return lifecycle.ChartRow(
        id=row_id,
        location_id=location_id,
        local_date=day,
        chat_id=chat_id,
        message_id=1000 + row_id,
        pinned=not done,
        pin_failed_at=None,
        last_rendered_at=rendered,
        finalized_at=rendered if done else None,
        unpinned_at=rendered if done else None,
        bot_key=io_loop.bot_key(DEFAULT_BOT_TOKEN),
        redraw_requested_at=redraw,
    )


def _plan(
    locations: list[lifecycle.ChartLocation],
    rows: list[lifecycle.ChartRow],
    now: datetime,
    not_before: dict[str, datetime] | None = None,
) -> lifecycle.Action | None:
    return lifecycle.plan(
        locations,
        rows,
        today=TODAY,
        now=now,
        not_before={} if not_before is None else not_before,
        tz=KYIV,
    )


def test_DATA02_owner_example_redraws_at_the_last_update_time() -> None:
    location = _location(1)
    row = _row(1, 1, rendered=RENDERED, redraw=REMOVED)

    action = _plan([location], [row], REMOVED + timedelta(seconds=1))

    assert action == lifecycle.Action("refresh", location, row, as_of=RENDERED)


def test_DATA02_unmarked_record_between_slots_has_nothing_due() -> None:
    row = _row(1, 1, rendered=RENDERED)

    assert _plan([_location(1)], [row], REMOVED + timedelta(seconds=1)) is None


def test_DATA02_marked_record_with_a_due_slot_is_a_regular_refresh() -> None:
    location = _location(1)
    row = _row(1, 1, rendered=RENDERED, redraw=REMOVED)

    action = _plan([location], [row], kyiv("2026-10-01 18:45") + timedelta(milliseconds=200))

    assert action == lifecycle.Action("refresh", location, row)
    assert action is not None and action.as_of is None


def test_DATA02_redraw_goes_before_an_older_regular_refresh_but_after_a_post() -> None:
    marked, regular, unposted = _location(1), _location(2), _location(3)
    marked_row = _row(1, 1, rendered=RENDERED, redraw=REMOVED)
    # Location 2's last render is older and its slot is due: a regular refresh.
    regular_row = _row(2, 2, rendered=kyiv("2026-10-01 18:29"))
    now = REMOVED + timedelta(seconds=1)

    first = _plan([marked, regular], [marked_row, regular_row], now)
    with_post = _plan([marked, regular, unposted], [marked_row, regular_row], now)

    assert first == lifecycle.Action("refresh", marked, marked_row, as_of=RENDERED)
    # A midnight-class step (today's post of location 3) still goes first.
    assert with_post == lifecycle.Action("post", unposted)


def test_DATA02_marks_on_older_or_stale_records_are_ignored() -> None:
    location = _location(1)
    older = _row(1, 1, rendered=RENDERED, redraw=REMOVED, day=YESTERDAY, done=True)
    now = REMOVED + timedelta(seconds=1)

    # A finished older record is never redrawn (D-14): nothing at all is due.
    assert _plan([location], [older, _row(2, 1, rendered=RENDERED)], now) is None
    # A stale record (its chat changed) is released, never redrawn.
    stale = _row(3, 1, rendered=RENDERED, redraw=REMOVED, chat_id=-100555)
    action = _plan([location], [stale], now)
    assert action == lifecycle.Action("release", location, stale)


def test_DATA02_clock_stepped_back_redraws_at_now() -> None:
    location = _location(1)
    row = _row(1, 1, rendered=kyiv("2026-10-01 18:50"), redraw=REMOVED)

    action = _plan([location], [row], REMOVED)

    # Never a time later than now: the pill cannot show the future.
    assert action == lifecycle.Action("refresh", location, row, as_of=REMOVED)


def test_DATA02_one_minute_period_redraws_at_this_minutes_mark() -> None:
    location = _location(1, refresh_min=1)
    rendered = kyiv("2026-10-01 18:43") + timedelta(milliseconds=400)
    row = _row(1, 1, rendered=rendered, redraw=REMOVED + timedelta(seconds=10))

    redraw = _plan([location], [row], REMOVED + timedelta(seconds=20))
    regular = _plan([location], [row], kyiv("2026-10-01 18:44"))

    assert redraw == lifecycle.Action("refresh", location, row, as_of=rendered)
    assert regular == lifecycle.Action("refresh", location, row)


@pytest.mark.parametrize("hold", ["refresh", "chat", "bot"])
def test_DATA02_redraw_waits_for_its_refresh_key_chat_and_bot(hold: str) -> None:
    keys = {
        "refresh": lifecycle.chart_key(1, "refresh"),
        "chat": io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID),
        "bot": io_loop.bot_wide_key(DEFAULT_BOT_TOKEN),
    }
    row = _row(1, 1, rendered=RENDERED, redraw=REMOVED)
    now = REMOVED + timedelta(seconds=1)

    waiting = _plan([_location(1)], [row], now, {keys[hold]: now + timedelta(seconds=5)})

    assert waiting is None


# Passes on PostgreSQL: helpers


def _spy_pills(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Record the now pill's text of every render (None: no pill); the real pill is drawn."""
    seen: list[str | None] = []
    real = render._draw_pill

    def spy(canvas: Image.Image, lay: Any, week: Week) -> Any:
        pill = real(canvas, lay, week)
        seen.append(None if pill is None else pill.text)
        return pill

    monkeypatch.setattr(render, "_draw_pill", spy)
    return seen


def _with_outages(location_factory: Callable[..., Any], *outages: tuple[str, str]) -> Any:
    """A location on since 08:00 local today, off in each (start, end) local span, on now."""
    location = location_factory()
    rows: list[tuple[str, str, str | None]] = []
    start = "2026-10-01 08:00"
    for off_start, off_end in outages:
        rows.append(("on", start, f"2026-10-01 {off_start}"))
        rows.append(("off", f"2026-10-01 {off_start}", f"2026-10-01 {off_end}"))
        start = f"2026-10-01 {off_end}"
    rows.append(("on", start, None))
    insert_pieces(location, local_pieces(rows))
    set_status(location, "on", at=kyiv(start))
    return location


def _seed(
    location: Any, *, rendered: datetime = RENDERED, day: date = TODAY, message_id: int = 1001
) -> ChartMessage:
    """Today's record as an earlier post and refresh left it: pinned, in its own chat."""
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=location.chat_id,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=message_id,
        pinned=True,
        last_rendered_at=rendered,
        created_at=rendered,
    )


def _remove(location: Any, start: str, at: datetime) -> None:
    outage = kyiv(f"2026-10-01 {start}")
    assert history.remove_outage(location.pk, outage, now=at, tz=KYIV) == "removed"


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    return io_loop.run_iteration(clock, state, charts=True)


def _record(row: ChartMessage) -> tuple[datetime, datetime | None]:
    stored = ChartMessage.objects.get(pk=row.pk)
    return stored.last_rendered_at, stored.redraw_requested_at


def _edits(fake: FakeTelegram) -> list[tuple[int, str]]:
    """(message id, caption) of every accepted editMessageMedia, in order."""
    return [
        (int(c.fields["message_id"]), json.loads(c.fields["media"])["caption"])
        for c in fake.chart_calls
        if c.method == "editMessageMedia"
    ]


def _methods(fake: FakeTelegram) -> list[str]:
    return [call.request.url.rsplit("/", 1)[1] for call in fake.calls]


def _chart_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage().split(" render_ms=")[0]
        for r in caplog.records
        if r.name == LIFECYCLE_LOGGER and r.getMessage().startswith("chart ")
    ]


# Integration: the owner's example and its edges


@DB
def test_DATA02_removal_redraws_todays_chart_at_its_last_update_time(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    state = io_loop.RelayState()
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    _remove(location, "17:00", REMOVED)
    assert _record(row) == (RENDERED, REMOVED)

    assert _pass(clock, state) is True

    # One edit, pill 18:30 (the last update's time), the removed outage drawn as on.
    assert pills == ["18:30"]
    assert _edits(fake_telegram) == [(1001, NO_OUTAGES)]
    assert _record(row) == (RENDERED, None)
    assert _chart_lines(caplog) == [f"chart redraw for location {location.pk}: ok ()"]
    # Nothing more until the next slot, which comes on schedule.
    clock.advance(seconds=1)
    assert _pass(clock, state) is False
    clock.set(kyiv("2026-10-01 18:44:59"))
    assert _pass(clock, state) is False
    clock.set(kyiv("2026-10-01 18:45"))
    assert _pass(clock, state) is True

    assert pills == ["18:30", "18:45"]
    assert _record(row) == (kyiv("2026-10-01 18:45"), None)
    assert _chart_lines(caplog)[-1] == f"chart refresh for location {location.pk}: ok ()"
    assert len(fake_telegram.calls) == 2


@DB
def test_DATA02_redraw_excludes_data_after_its_time(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A false outage at 17:00 and a real one at 18:35-18:40, after the last update.
    location = _with_outages(location_factory, ("17:00", "17:20"), ("18:35", "18:40"))
    _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    _remove(location, "17:00", REMOVED)

    assert _pass(clock, state) is True
    clock.set(kyiv("2026-10-01 18:45"))
    assert _pass(clock, state) is True

    five_min = 5 * 60 * 1_000_000
    assert pills == ["18:30", "18:45"]
    assert _edits(fake_telegram) == [
        (1001, NO_OUTAGES),
        (1001, chart_texts.live_caption(five_min, 1, "en")),
    ]


@DB
def test_DATA02_removal_just_after_a_boundary_gives_one_regular_refresh(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()
    boundary = kyiv("2026-10-01 18:45")
    _remove(location, "17:00", boundary + timedelta(milliseconds=100))
    clock = FakeClock(boundary + timedelta(milliseconds=200))

    assert _pass(clock, state) is True
    clock.advance(seconds=1)
    assert _pass(clock, state) is False

    assert pills == ["18:45"]
    assert _record(row) == (clock.now() - timedelta(seconds=1), None)
    assert len(fake_telegram.calls) == 1


@DB
def test_DATA02_two_removals_in_a_row_give_two_redraws(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"), ("17:40", "17:50"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    second = REMOVED + timedelta(seconds=1)
    # The second removal commits while the first redraw's edit is in flight.
    fake_telegram.answer_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", lambda: _remove(location, "17:40", second)
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()
    _remove(location, "17:00", REMOVED)

    assert _pass(clock, state) is True
    # The first redraw saw the first mark only: the second one stays.
    assert _record(row) == (RENDERED, second)
    clock.advance(seconds=1)
    assert _pass(clock, state) is True
    clock.advance(seconds=1)
    assert _pass(clock, state) is False

    assert pills == ["18:30", "18:30"]
    assert _record(row) == (RENDERED, None)
    assert [caption for _id, caption in _edits(fake_telegram)][-1] == NO_OUTAGES


@DB
def test_INV18_removal_while_the_worker_is_down_folds_into_the_catch_up(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    _remove(location, "17:00", REMOVED)
    # The worker comes back at 19:10: one catch-up refresh at now, which clears the mark.
    clock = FakeClock(kyiv("2026-10-01 19:10"))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    clock.advance(seconds=1)
    assert _pass(clock, state) is False

    assert pills == ["19:10"]
    assert _record(row) == (kyiv("2026-10-01 19:10"), None)
    assert _edits(fake_telegram) == [(1001, NO_OUTAGES)]


@DB
def test_DATA02_removal_after_midnight_before_the_post_marks_nothing(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    yesterday = _seed(location, rendered=kyiv("2026-10-01 23:45"))
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    after_midnight = kyiv("2026-10-02 00:00:20")
    _remove(location, "17:00", after_midnight)
    clock = FakeClock(after_midnight + timedelta(seconds=10))
    SystemState.objects.update_or_create(pk=1, defaults={"last_cycle_completed_at": None})

    assert _pass(clock, io_loop.RelayState()) is True

    # Nothing was marked; today's first chart is posted at now and shows the removal.
    assert _record(yesterday) == (kyiv("2026-10-01 23:45"), None)
    assert _methods(fake_telegram) == ["sendPhoto"]
    assert pills == ["00:00"]


@DB
def test_D14_finished_chart_is_not_redrawn(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    finished = _seed(location, rendered=kyiv("2026-10-02 00:00:05"), message_id=1000)
    ChartMessage.objects.filter(pk=finished.pk).update(
        pinned=False, finalized_at=kyiv("2026-10-02 00:05"), unpinned_at=kyiv("2026-10-02 00:01")
    )
    today = _seed(
        location, rendered=kyiv("2026-10-02 08:00") + SUB, day=date(2026, 10, 2), message_id=1001
    )
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    removed = kyiv("2026-10-02 08:07")
    # The removed outage was yesterday's; only today's chart is marked and redrawn.
    _remove(location, "17:00", removed)
    clock = FakeClock(removed + timedelta(seconds=1))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert _pass(clock, state) is False

    assert [message_id for message_id, _caption in _edits(fake_telegram)] == [1001]
    assert pills == ["08:00"]
    assert _record(finished)[1] is None
    assert _record(today) == (kyiv("2026-10-02 08:00") + SUB, None)


# Failures keep the mark


@DB
def test_DATA02_failed_redraw_keeps_the_mark_and_redraws_again(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=502, json_body=BAD_GATEWAY
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    _remove(location, "17:00", REMOVED)
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert _record(row) == (RENDERED, REMOVED)
    # The bot waits 2 s and the step 30 s more (step_delay), then the redraw runs again,
    # at the same time.
    clock.advance(seconds=31)
    assert _pass(clock, state) is False
    clock.advance(seconds=1)
    assert _pass(clock, state) is True

    assert pills == ["18:30", "18:30"]
    assert _record(row) == (RENDERED, None)


@DB
def test_INV17_3_redraw_of_a_deleted_chart_retires_it_and_posts_at_now(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=GONE)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    _remove(location, "17:00", REMOVED)
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert ChartMessage.objects.get(pk=row.pk).retired_at == clock.now()
    clock.advance(seconds=1)
    assert _pass(clock, state) is True

    # The replacement is posted at now, so its pill shows now, and it shows the removal.
    assert _methods(fake_telegram) == ["editMessageMedia", "sendPhoto"]
    assert pills == ["18:30", "18:43"]


@DB
def test_DATA02_a_clear_that_cannot_be_written_backs_off_and_redraws_again(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _with_outages(location_factory, ("17:00", "17:20"))
    row = _seed(location)
    pills = _spy_pills(monkeypatch)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._clear_redraw
    failures = [DatabaseError("canceling statement due to statement timeout")]

    def flaky(*args: Any) -> None:
        if failures:
            raise failures.pop()
        real(*args)

    monkeypatch.setattr(lifecycle, "_clear_redraw", flaky)
    _remove(location, "17:00", REMOVED)
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    assert _record(row) == (RENDERED, REMOVED)
    # The edit was made, the clear was not written: the step waits step_delay(1), 30 s.
    clock.advance(seconds=29)
    assert _pass(clock, state) is False
    clock.advance(seconds=1)
    assert _pass(clock, state) is True

    assert pills == ["18:30", "18:30"]
    assert _record(row) == (RENDERED, None)


# End to end: relay sends -> admin removal (the view) -> delete OFF, delete ON, redraw


def _no_anchors() -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _remove_post(location: Any, start: datetime, clock: FakeClock) -> list[str]:
    """POST the removal to the location page's view with ``clock``; the flashes."""
    request = RequestFactory().post(
        f"/locations/{location.pk}/outages/{ops.instant_us(start)}/remove/"
    )
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    view = OutageRemoveView.as_view(clock=clock)
    response = view(request, pk=location.pk, start_us=ops.instant_us(start))
    assert response.status_code == 302
    return [str(m) for m in request._messages]  # type: ignore[attr-defined]


@DB
def test_DATA02_false_outage_removed_deletes_off_then_on_then_redraws(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_anchors()
    location = location_factory()
    record = _seed(location, rendered=kyiv("2026-10-01 18:15") + SUB)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    pills = _spy_pills(monkeypatch)
    state = io_loop.RelayState()
    clock = FakeClock(kyiv("2026-10-01 08:00"))
    start = kyiv("2026-10-01 18:20")
    assert transitions.record_heartbeat(location.pk, kyiv("2026-10-01 08:00")) == "started"
    assert transitions.record_heartbeat(location.pk, start) == "plain"
    # A false outage from 18:20: OFF recorded at 18:21:31 and sent (message 1).
    assert detection.run_cycle(kyiv("2026-10-01 18:21:31")) == 1
    clock.set(kyiv("2026-10-01 18:21:32"))
    assert _pass(clock, state) is True
    # Power is back at 18:25: ON sent (message 2).
    assert transitions.record_heartbeat(location.pk, kyiv("2026-10-01 18:25")) == "restored"
    clock.set(kyiv("2026-10-01 18:25:01"))
    assert _pass(clock, state) is True
    # The 18:30 update, answered at 18:30:00.8.
    clock.set(RENDERED)
    assert _pass(clock, state) is True
    assert _record(record) == (RENDERED, None)
    calls = len(fake_telegram.chart_calls)

    clock.set(REMOVED)
    flashes = _remove_post(location, start, clock)

    assert len(flashes) == 1
    assert ChartMessage.objects.get(pk=record.pk).redraw_requested_at == REMOVED
    for _ in range(3):
        clock.advance(seconds=1)
        assert _pass(clock, state) is True
    clock.advance(seconds=1)
    assert _pass(clock, state) is False

    after = fake_telegram.chart_calls[calls:]
    assert [(c.method, c.fields.get("message_id")) for c in after] == [
        ("deleteMessage", 1),
        ("deleteMessage", 2),
        ("editMessageMedia", "1001"),
    ]
    assert pills[-1] == "18:30"
    assert json.loads(after[-1].fields["media"])["caption"] == NO_OUTAGES
    assert set(OutboxMessage.objects.values_list("delete_result", flat=True)) == {"deleted"}
    assert _record(record) == (RENDERED, None)


@DB
def test_DATA02_redraw_and_delete_never_hold_another_bots_chart(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    # A redraw failing on bot A (a refused connection) holds bot A only.
    location = _with_outages(location_factory, ("17:00", "17:20"))
    _seed(location)
    refused = requests.ConnectionError("refused")
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "editMessageMedia", exc=refused)
    _remove(location, "17:00", REMOVED)
    state = io_loop.RelayState()

    assert _pass(FakeClock(REMOVED + timedelta(seconds=1)), state) is True

    assert io_loop.bot_wide_key(TOKEN_B) not in state.not_before
    assert ChartMessage.objects.get().redraw_requested_at == REMOVED
