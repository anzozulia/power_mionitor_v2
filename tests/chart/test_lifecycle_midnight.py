"""The chart lifecycle at local midnight and after missed midnights (INV-18, INV-19).

At local midnight (Europe/Kyiv) a location gets today's chart posted and pinned, and the
previous day's chart gets its finished render (now = the midnight that ends its day, no
now line, no pill, the caption's line 1 only) and is unpinned (D-01, D-13). Each step is
its own condition on every I/O pass, in D-02 order (post, pin, finalize, unpin), so after
downtime across one or more midnights exactly one chart for today is posted and every
older chart is finalized and unpinned, a day the worker missed gets no chart (D-03), and
a failing step never blocks the others (D-02). Edits and unpins use the chat and the
message id stored with each record, never the location's current chat, and nothing but a
recorded message is ever unpinned (D-04, INV-19). A record whose chat or bot no longer
matches its location's is released instead: unpinned in its stored chat, retired, and
never given a final edit (D-08).

A day's final edit waits until detection has settled past the end of that day (INV-03):
OFF is recorded after the fact, backdated to the last heartbeat, so an outage that started
in the day's last minutes is only in the timeline a timeout after midnight. The unpin does
not wait for it, so two charts are pinned for seconds only (D-02).

Every test runs ``io_loop.run_iteration(..., charts=True)``, which calls
``close_old_connections()``, so each is ``django_db(transaction=True)``. Time comes only
from the ``FakeClock``; Telegram is faked at the HTTP boundary (``fake_telegram``); renders
are real (Pillow). Older records are seeded with ``ChartMessage.objects.create``. The
worker's first-cycle gate arrives in 03-10, so a test that needs the downtime drawn as not
monitored carves it itself with ``lapse.carve_window``. The worker's detection thread keeps
its cursor (``system_state.last_cycle_completed_at``) within a cycle of now, so ``_pass``
first moves the cursor to the clock's now; a test about the settle time runs detection
cycles itself (``_detection_cycle``).
"""

import dataclasses
import io
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, insert_pieces, kyiv, local_pieces, monitor, set_status
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db.models import Value
from django.db.models.functions import Greatest
from PIL import Image

from powermon.alerts.models import OpsIncident
from powermon.chart import lifecycle, model
from powermon.chart.models import ChartMessage
from powermon.engine import lapse
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations.models import Location
from powermon.worker import detection, io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# Fri 2026-10-02 is today; the location has been monitored since Wed 2026-09-30 00:00.
TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
SINCE = kyiv("2026-09-30 00:00")
AFTER_MIDNIGHT = kyiv("2026-10-02 00:07")
OLD_CHAT_ID = -1009999999999
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
TOKEN_C = "876543210:" + "D" * 35
CHAT_C = -1008765432109
LIFECYCLE_LOGGER = lifecycle.__name__
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B", TOKEN_C: "C"}
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
CANT_EDIT = {"ok": False, "error_code": 400, "description": "Bad Request: message can't be edited"}
KICKED = {
    "ok": False,
    "error_code": 403,
    "description": "Forbidden: bot was kicked from the channel chat",
}
EDIT_GONE = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: message to edit not found",
}
UNPIN_GONE = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: message to unpin not found",
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


def _with_outage(location_factory: Callable[..., Any], **kw: Any) -> Any:
    """A location monitored since SINCE that was off on 2026-10-01 from 10:00 to 12:00."""
    location = location_factory(**kw)
    insert_pieces(
        location,
        local_pieces(
            [
                ("on", "2026-09-30 00:00", "2026-10-01 10:00"),
                ("off", "2026-10-01 10:00", "2026-10-01 12:00"),
                ("on", "2026-10-01 12:00", None),
            ]
        ),
    )
    set_status(location, "on", at=kyiv("2026-10-01 12:00"))
    return location


def _seed(
    location: Any,
    day: date,
    *,
    message_id: int,
    pinned: bool,
    finalized: bool = False,
    chat_id: int = DEFAULT_CHAT_ID,
    rendered: datetime | None = None,
) -> ChartMessage:
    """A record as an earlier run left it: posted (and maybe pinned) on ``day``.

    Posted by the location's own bot (``bot_key``), so only a chat that differs from the
    location's makes it stale (D-08).
    """
    at = rendered if rendered is not None else model.next_midnight(day, KYIV) - _min(15)
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=chat_id,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=at,
        finalized_at=at if finalized else None,
        created_at=at,
    )


def _accept(fake: Any, token: str, *methods: str) -> None:
    """Accept every call of these chart methods only; ``fail_method`` answers the others."""
    for method in methods:
        fake.answer_method(token, method, lambda: None)


def _calls(fake: Any) -> list[tuple[str, int, int | None]]:
    """(method, chat id, message id) of every accepted chart call, in order."""
    out = []
    for call in fake.chart_calls:
        message_id = call.fields.get("message_id")
        out.append(
            (
                call.method,
                int(call.fields["chat_id"]),
                None if message_id is None else int(message_id),
            )
        )
    return out


def _requests(fake: Any, start: int = 0) -> list[tuple[str, str]]:
    """(bot label, Bot API method) of every request from index ``start`` on, failed ones too."""
    out = []
    for call in list(fake.calls)[start:]:
        token, method = call.request.url.split("/bot", 1)[1].split("/", 1)
        out.append((BOTS[token], method))
    return out


def _caption(call: Any) -> str:
    """The caption of an accepted sendPhoto or editMessageMedia call."""
    if call.method == "editMessageMedia":
        return str(json.loads(call.fields["media"])["caption"])
    return str(call.fields["caption"])


def _detected(at: datetime) -> None:
    """Detection has completed its cycles up to ``at``: its cursor moves there, never back."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(at))
    )


def _detection_cycle(at: datetime) -> int:
    """One detection cycle at ``at``, in the worker's order: the cursor, then the OFF decisions."""
    _detected(at)
    return detection.run_cycle(at)


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


def _records() -> list[tuple[Any, ...]]:
    return list(ChartMessage.objects.order_by("id").values_list())


def _png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        return image.size


def _min(n: float) -> timedelta:
    return timedelta(minutes=n)


def _sec(n: float) -> timedelta:
    return timedelta(seconds=n)


# INV-18 #1: the worker was down across midnight (D-01, D-02, D-04, D-13)


def test_INV18_1_worker_down_across_midnight(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    yesterday = _seed(
        location, YESTERDAY, message_id=501, pinned=True, rendered=kyiv("2026-10-01 23:45")
    )
    # Down from 23:58 to 00:07: the restart's lapse carve draws it as not monitored, and
    # the detection cursor is at 00:07 (``_pass``), so yesterday has settled.
    lapse.carve_window(kyiv("2026-10-01 23:58"), AFTER_MIDNIGHT)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(AFTER_MIDNIGHT)
    state = io_loop.RelayState()

    for _ in range(4):
        assert _pass(clock, state) is True

    assert _calls(fake_telegram) == [
        ("sendPhoto", DEFAULT_CHAT_ID, None),
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("editMessageMedia", DEFAULT_CHAT_ID, 501),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 501),
    ]
    photo, _pin, final, unpin = fake_telegram.chart_calls
    # Today so far (00:00-00:07) is all downtime, not monitored: no on or off time, so the
    # caption says so instead of "No outages today" (D-03).
    assert _caption(photo) == "Today: not monitored"
    # The finished day: one line, with the weekday and date (D-13).
    assert _caption(final) == "No outages on Thu 01.10"
    assert "\n" not in _caption(final)
    assert unpin.fields == {"chat_id": DEFAULT_CHAT_ID, "message_id": 501}
    today_row = ChartMessage.objects.get(location=location, local_date=TODAY)
    assert (today_row.message_id, today_row.pinned, today_row.finalized_at) == (1001, True, None)
    yesterday.refresh_from_db()
    assert yesterday.finalized_at == AFTER_MIDNIGHT
    assert yesterday.pinned is False
    # Only today's chart is pinned now, and a fifth pass has nothing left to do.
    assert list(ChartMessage.objects.filter(pinned=True).values_list("message_id", flat=True)) == [
        1001
    ]
    assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == 4
    assert not any(
        call.request.url.endswith("/unpinAllChatMessages") for call in fake_telegram.calls
    )


def test_finalize_sends_the_finished_render(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, YESTERDAY, message_id=501, pinned=True)
    _seed(location, TODAY, message_id=900, pinned=True, rendered=AFTER_MIDNIGHT)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(AFTER_MIDNIGHT)

    assert _pass(clock, io_loop.RelayState()) is True

    [final] = fake_telegram.chart_calls
    assert (final.method, final.fields["message_id"]) == ("editMessageMedia", "501")
    png = final.files["chart"]
    assert _png_size(png) == (1280, 1000)
    # The image is the finished day as of the midnight that ends it (chart-spec §7)...
    [snapshot], _ = lifecycle.read_snapshot(TODAY)
    assert snapshot.location_id == location.pk
    end_of_day = model.next_midnight(YESTERDAY, KYIV)
    finished, _ = lifecycle.chart_content(snapshot, YESTERDAY, end_of_day, live=False, tz=KYIV)
    assert png == finished
    # ...and not a live render of that day (no now line, no pill).
    live, _ = lifecycle.chart_content(
        snapshot, YESTERDAY, kyiv("2026-10-01 23:59"), live=True, tz=KYIV
    )
    assert png != live


def test_finished_caption_with_outages(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    english = _with_outage(location_factory)
    _seed(english, YESTERDAY, message_id=501, pinned=True)
    ukrainian = _with_outage(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    _seed(ukrainian, YESTERDAY, message_id=601, pinned=True, chat_id=CHAT_B)
    # The language is read at render time (D-14).
    Location.objects.filter(pk=ukrainian.pk).update(language="uk")
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(AFTER_MIDNIGHT)

    assert _run_until_idle(clock, io_loop.RelayState()) == 8

    finals = {
        call.token: _caption(call)
        for call in fake_telegram.chart_calls
        if call.method == "editMessageMedia"
    }
    assert finals == {
        DEFAULT_BOT_TOKEN: "Thu 01.10 off: 2h · 1 outage",
        TOKEN_B: "Чт 01.10 без світла: 2 год · 1 відключення",
    }


# INV-03, INV-08: the finished day comes from its settled timeline (Wave 4 audit, fix 1)


def test_INV03_INV08_final_edit_waits_for_an_outage_detected_after_midnight(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Period 60 s + grace 30 s: detection records an OFF once 90 s have passed in silence,
    # backdated to the last heartbeat, which came at 23:59:25 (a blackout at midnight).
    location = _monitored(location_factory)
    last_heartbeat = kyiv("2026-10-01 23:59:25")
    LocationState.objects.filter(location=location).update(last_heartbeat_at=last_heartbeat)
    yesterday = _seed(
        location, YESTERDAY, message_id=501, pinned=True, rendered=kyiv("2026-10-01 23:45")
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    # The OFF alert.
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    start = kyiv("2026-10-02 00:00:01")
    clock = FakeClock(start)
    state = io_loop.RelayState()
    calls_at: list[tuple[datetime, str]] = []
    off_recorded_at: list[datetime] = []

    # Detection runs a cycle every 5 s, and the I/O thread a pass after each, to 00:02:01.
    for step in range(25):
        at = start + _sec(5 * step)
        clock.set(at)
        if _detection_cycle(at):
            off_recorded_at.append(at)
        before = len(fake_telegram.chart_calls)
        io_loop.run_iteration(clock, state, charts=True)
        calls_at.extend((at, call.method) for call in fake_telegram.chart_calls[before:])

    # The OFF is recorded at 00:00:56, backdated into yesterday (INV-03).
    assert off_recorded_at == [kyiv("2026-10-02 00:00:56")]
    assert PowerInterval.objects.get(location=location, state="off").start_at == last_heartbeat
    # Today's chart is posted and pinned and yesterday's is unpinned right away: two
    # charts are pinned for seconds only (D-02). The final edit waits until the detection
    # cursor is past yesterday's end + 90 s + the lapse threshold (00:01:45), so it is made
    # by the first pass after that, never before the cursor passed midnight + the timeout.
    assert calls_at == [
        (start, "sendPhoto"),
        (start + _sec(5), "pinChatMessage"),
        (start + _sec(10), "unpinChatMessage"),
        (kyiv("2026-10-02 00:01:46"), "editMessageMedia"),
    ]
    final = fake_telegram.chart_calls[-1]
    assert final.fields["message_id"] == "501"
    # The finished day counts the outage from 23:59:25 (35 s, shown as 1m), the outage
    # today's row starts with: it counts on both days (chart-spec §8).
    assert _caption(final) == "Thu 01.10 off: 1m · 1 outage"
    [snapshot], _ = lifecycle.read_snapshot(TODAY)
    end_of_day = model.next_midnight(YESTERDAY, KYIV)
    settled, _ = lifecycle.chart_content(snapshot, YESTERDAY, end_of_day, live=False, tz=KYIV)
    assert final.files["chart"] == settled
    yesterday.refresh_from_db()
    assert (yesterday.finalized_at, yesterday.pinned) == (kyiv("2026-10-02 00:01:46"), False)
    pinned = ChartMessage.objects.filter(pinned=True).values_list("message_id", flat=True)
    assert list(pinned) == [1001]


# INV-18 #2, INV-19 #1: catch-up after missed midnights (D-02, D-03, D-04)


def test_INV18_2_down_all_of_10_02(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, YESTERDAY, message_id=501, pinned=True)
    start = kyiv("2026-10-03 09:00")
    # The worker was down from 10-01 23:58 to 10-03 09:00: all of 10-02 is not monitored.
    lapse.carve_window(kyiv("2026-10-01 23:58"), start)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(start)

    assert _run_until_idle(clock, io_loop.RelayState()) == 4

    assert _calls(fake_telegram) == [
        ("sendPhoto", DEFAULT_CHAT_ID, None),
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("editMessageMedia", DEFAULT_CHAT_ID, 501),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 501),
    ]
    assert _caption(fake_telegram.chart_calls[2]) == "No outages on Thu 01.10"
    records = ChartMessage.objects.filter(location=location).order_by("local_date")
    assert [(r.local_date, r.message_id, r.pinned, r.finalized_at) for r in records] == [
        (YESTERDAY, 501, False, start),
        (date(2026, 10, 3), 1001, True, None),
    ]
    # A day the worker missed entirely gets no chart afterwards (D-03).
    assert not ChartMessage.objects.filter(local_date=TODAY).exists()


def test_INV19_1_both_older_charts_finalized_and_unpinned(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # The 10-01 and 10-02 charts are both still pinned, in the location's current chat
    # and posted by its current bot: neither moved, so neither is released (D-08).
    location = _monitored(location_factory)
    first = _seed(location, YESTERDAY, message_id=501, pinned=True)
    second = _seed(location, TODAY, message_id=502, pinned=True)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(kyiv("2026-10-03 00:05"))

    assert _run_until_idle(clock, io_loop.RelayState()) == 6

    # Each final edit and unpin names the record's stored chat and its own message id
    # (Phase 3 D-04, still true for every record that did not move), each exactly once.
    assert _calls(fake_telegram) == [
        ("sendPhoto", DEFAULT_CHAT_ID, None),
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("editMessageMedia", DEFAULT_CHAT_ID, 501),
        ("editMessageMedia", DEFAULT_CHAT_ID, 502),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 501),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 502),
    ]
    for record in (first, second):
        record.refresh_from_db()
        assert (record.pinned, record.finalized_at) == (False, clock.now())
    pinned = ChartMessage.objects.filter(pinned=True).values_list("local_date", "message_id")
    assert list(pinned) == [(date(2026, 10, 3), 1001)]


def test_D04_D08_moved_record_is_unpinned_in_its_stored_chat_and_never_finalized(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # The location's chat changed after yesterday's chart was posted to OLD_CHAT_ID, by
    # the location's own bot. At 00:07 its day has settled, so Phase 3 would have made its
    # final edit in OLD_CHAT_ID; D-08 releases it instead: one unpin in its stored chat, by
    # its message id (D-04), then it is retired. No sendPhoto goes before the release
    # (Pitfall 1).
    location = _monitored(location_factory)
    moved = _seed(location, YESTERDAY, message_id=501, pinned=True, chat_id=OLD_CHAT_ID)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)

    assert _run_until_idle(FakeClock(AFTER_MIDNIGHT), io_loop.RelayState()) == 3

    assert _calls(fake_telegram) == [
        ("unpinChatMessage", OLD_CHAT_ID, 501),
        ("sendPhoto", DEFAULT_CHAT_ID, None),
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
    ]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 0
    moved.refresh_from_db()
    assert (moved.retired_at, moved.pinned, moved.finalized_at) == (AFTER_MIDNIGHT, False, None)
    today_row = ChartMessage.objects.get(local_date=TODAY)
    assert (today_row.chat_id, today_row.message_id, today_row.pinned) == (
        DEFAULT_CHAT_ID,
        1001,
        True,
    )


# D-02: a failing step never blocks the others (INV-19, T-03-34)


def test_D02_failing_post_never_blocks_cleanup(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    older = _seed(location, YESTERDAY, message_id=501, pinned=True)
    # Every sendPhoto answers 502; final edits and unpins are accepted.
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "sendPhoto", status=502, json_body=BAD_GATEWAY)
    _accept(fake_telegram, DEFAULT_BOT_TOKEN, "editMessageMedia", "unpinChatMessage")
    answer = kyiv("2026-10-02 09:00")
    clock = FakeClock(answer)
    state = io_loop.RelayState()
    post_key = lifecycle.chart_key(location.pk, "post")
    bot = io_loop.bot_wide_key(DEFAULT_BOT_TOKEN)

    assert _pass(clock, state) is True
    # The bot is held as an alert's 5xx would hold it; the post waits step_delay(1) longer.
    assert state.not_before == {bot: answer + _sec(2), post_key: answer + _sec(2 + 30)}
    assert not ChartMessage.objects.filter(local_date=TODAY).exists()
    clock.set(answer + _sec(1))
    assert _pass(clock, state) is False
    # Once the bot's hold ends, the older chart is finalized and unpinned...
    clock.set(answer + _sec(2))
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False
    # ...and the post is retried at its own key.
    clock.set(answer + _sec(31))
    assert _pass(clock, state) is False
    second = answer + _sec(32)
    clock.set(second)
    assert _pass(clock, state) is True
    assert state.not_before[bot] == second + _sec(4)
    assert state.not_before[post_key] == second + _sec(4 + 60)

    assert _requests(fake_telegram) == [
        ("A", "sendPhoto"),
        ("A", "editMessageMedia"),
        ("A", "unpinChatMessage"),
        ("A", "sendPhoto"),
    ]
    older.refresh_from_db()
    assert (older.finalized_at, older.pinned) == (answer + _sec(2), False)
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 2
    assert not ChartMessage.objects.filter(local_date=TODAY).exists()


def test_D02_failing_pin_never_blocks_cleanup(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    older = _seed(location, YESTERDAY, message_id=501, pinned=True)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "pinChatMessage", status=502, json_body=BAD_GATEWAY
    )
    _accept(fake_telegram, DEFAULT_BOT_TOKEN, "sendPhoto", "editMessageMedia", "unpinChatMessage")
    answer = kyiv("2026-10-02 09:00")
    clock = FakeClock(answer)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    today_row = ChartMessage.objects.get(local_date=TODAY)
    pin_key = lifecycle.chart_key(location.pk, "pin", today_row.pk)
    assert _pass(clock, state) is True
    assert state.not_before == {
        io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): answer + _sec(2),
        pin_key: answer + _sec(2 + 30),
    }
    clock.set(answer + _sec(1))
    assert _pass(clock, state) is False
    clock.set(answer + _sec(2))
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False
    clock.set(answer + _sec(32))
    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == [
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
        ("A", "editMessageMedia"),
        ("A", "unpinChatMessage"),
        ("A", "pinChatMessage"),
    ]
    older.refresh_from_db()
    assert (older.finalized_at, older.pinned) == (answer + _sec(2), False)
    today_row.refresh_from_db()
    # A 5xx is not "the bot may not pin": no pin failure, no incident (D-07).
    assert (today_row.pinned, today_row.pin_failed_at) == (False, None)
    assert not OpsIncident.objects.filter(kind="chart_pin_failed").exists()


# Older charts: cleanup is best effort, a gone chart is forgotten (INV-19, D-06)


def test_permanent_cleanup_errors_are_best_effort(
    location_factory: Callable[..., Any], fake_telegram: Any, caplog: pytest.LogCaptureFixture
) -> None:
    location = _monitored(location_factory)
    _seed(location, TODAY, message_id=900, pinned=True, rendered=AFTER_MIDNIGHT)
    older = _seed(location, YESTERDAY, message_id=501, pinned=True)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=CANT_EDIT
    )
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "unpinChatMessage", status=403, json_body=KICKED)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(AFTER_MIDNIGHT)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True
    older.refresh_from_db()
    assert (older.finalized_at, older.pinned) == (AFTER_MIDNIGHT, True)
    assert _pass(clock, state) is True
    older.refresh_from_db()
    assert older.pinned is False

    # Done for good: no retry on later passes, and no step key or failure count is left.
    for minutes in (0, 1, 5, 14):
        clock.set(AFTER_MIDNIGHT + _min(minutes))
        assert _pass(clock, state) is False
    assert _requests(fake_telegram) == [("A", "editMessageMedia"), ("A", "unpinChatMessage")]
    assert state.not_before == {}
    assert state.chart_failures == {}
    lines = [r.getMessage() for r in caplog.records if r.name == LIFECYCLE_LOGGER]
    assert len(lines) == 2
    assert all(str(location.pk) in line and str(older.pk) in line for line in lines)
    assert "http_400" in lines[0] and "finalized" in lines[0]
    assert "http_403" in lines[1] and "unpinned" in lines[1]
    assert "can't be edited" not in caplog.text and "kicked" not in caplog.text


def test_older_chart_gone_is_forgotten(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, TODAY, message_id=900, pinned=True, rendered=AFTER_MIDNIGHT)
    unpinned_gone = _seed(location, date(2026, 9, 30), message_id=401, pinned=True, finalized=True)
    edit_gone = _seed(location, YESTERDAY, message_id=501, pinned=True)
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "editMessageMedia", status=400, json_body=EDIT_GONE
    )
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "unpinChatMessage", status=400, json_body=UNPIN_GONE
    )
    clock = FakeClock(AFTER_MIDNIGHT)
    state = io_loop.RelayState()

    assert _run_until_idle(clock, state) == 2

    # The deleted 10-01 chart is retired: no repost for that day, no unpin of it.
    edit_gone.refresh_from_db()
    assert (edit_gone.retired_at, edit_gone.pinned, edit_gone.finalized_at) == (
        AFTER_MIDNIGHT,
        False,
        None,
    )
    # The 09-30 chart was already finalized; "message to unpin not found" ends its unpin.
    unpinned_gone.refresh_from_db()
    assert (unpinned_gone.pinned, unpinned_gone.retired_at) == (False, None)
    assert _requests(fake_telegram) == [("A", "editMessageMedia"), ("A", "unpinChatMessage")]
    unpin_body = json.loads(fake_telegram.calls[1].request.body)
    assert unpin_body == {"chat_id": DEFAULT_CHAT_ID, "message_id": 401}
    assert ChartMessage.objects.filter(local_date=YESTERDAY).count() == 1
    assert state.chart_failures == {}


# Ordering across locations, idempotency, concurrency (D-02, CHRT-05)


def test_midnight_work_of_one_location_finishes_before_the_next(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    a = _monitored(location_factory)
    b = _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    c = _monitored(location_factory, bot_token=TOKEN_C, chat_id=CHAT_C)
    assert a.pk < b.pk < c.pk
    _seed(a, YESTERDAY, message_id=501, pinned=True)
    _seed(b, YESTERDAY, message_id=601, pinned=True, chat_id=CHAT_B)
    # C did its midnight work at 00:05; its refresh is due at 00:20.
    _seed(c, TODAY, message_id=701, pinned=True, chat_id=CHAT_C, rendered=kyiv("2026-10-02 00:05"))
    for token in (DEFAULT_BOT_TOKEN, TOKEN_B, TOKEN_C):
        fake_telegram.accept_chart(token)
    clock = FakeClock(kyiv("2026-10-02 00:30"))

    assert _run_until_idle(clock, io_loop.RelayState()) == 9

    assert _requests(fake_telegram) == [
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
        ("A", "editMessageMedia"),
        ("A", "unpinChatMessage"),
        ("B", "sendPhoto"),
        ("B", "pinChatMessage"),
        ("B", "editMessageMedia"),
        ("B", "unpinChatMessage"),
        ("C", "editMessageMedia"),
    ]


def test_midnight_job_twice_changes_nothing(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, YESTERDAY, message_id=501, pinned=True)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(AFTER_MIDNIGHT)
    state = io_loop.RelayState()
    assert _run_until_idle(clock, state) == 4
    done = _records()

    clock.set(kyiv("2026-10-02 00:10"))
    for _ in range(3):
        assert _pass(clock, state) is False

    assert len(fake_telegram.calls) == 4
    assert _records() == done


def test_a_concurrent_final_edit_is_not_written_twice(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    _seed(location, TODAY, message_id=900, pinned=True, rendered=AFTER_MIDNIGHT)
    older = _seed(location, YESTERDAY, message_id=501, pinned=True)
    other_worker = kyiv("2026-10-02 00:06")

    def finalized_by_another_worker() -> None:
        ChartMessage.objects.filter(pk=older.pk).update(finalized_at=other_worker)

    # Another worker writes its final edit while this one's call is in flight.
    fake_telegram.answer_method(DEFAULT_BOT_TOKEN, "editMessageMedia", finalized_by_another_worker)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(AFTER_MIDNIGHT)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    # finalized_at NULL -> set is conditional: the first write stays.
    older.refresh_from_db()
    assert older.finalized_at == other_worker
    # The next pass sees it finalized: the unpin goes, and no second final edit is made.
    assert _run_until_idle(clock, state) == 1
    assert [call.method for call in fake_telegram.chart_calls] == [
        "editMessageMedia",
        "unpinChatMessage",
    ]
