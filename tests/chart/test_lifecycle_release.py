"""The chart half of a channel change and of a delete (INV-19 #2, D-08, D-09, Pitfall 1).

A chart record is ``stale`` once its channel is no longer its location's: its stored chat
differs from the location's chat, the bot that posted it (``bot_key``) differs from the
location's current bot, or the location was deleted. The worker then releases it: one
unpin by its own message id in its stored chat, with the location's current bot, and the
record is retired, so no final edit, pin or refresh ever targets it again (D-08, D-09).
Today's chart is then posted and pinned in the new chat. While any stale record of a
location is active, that location makes no other chart step, so no photo is posted before
the release succeeds (Pitfall 1: a post while the old today-record is active would hit
``chart_message_one_active_per_day`` and leave an untracked photo on every pass).

Every test that runs ``io_loop.run_iteration`` (``_pass``) is ``django_db(transaction=True)``,
because the pass calls ``close_old_connections()``. The pure ``stale`` and ``plan`` cases
need no database. Time comes only from the ``FakeClock``; Telegram is faked at the HTTP
boundary (``fake_telegram``); renders are real (Pillow), so each test keeps to a handful.
A location's chat, token or ``deleted_at`` is changed with a queryset ``update``, as the
admin's save does (the admin views arrive in 04-07). CHAT_B and TOKEN_B are the location's
new chat and new bot.
"""

import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db.models import Value
from django.db.models.functions import Greatest

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle, model, render
from powermon.chart.model import Week
from powermon.chart.models import ChartMessage
from powermon.engine.models import SystemState
from powermon.locations.models import Location
from powermon.worker import io_loop

# Fri 2026-10-02 12:05 local is "now"; the location has been monitored since 10-01 08:00.
TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-02 12:05")
SINCE = kyiv("2026-10-01 08:00")
# The location's new chat and new bot.
CHAT_B = -1009876543210
TOKEN_B = "987654321:" + "B" * 35
# Another location's chat (its bot is TOKEN_B).
CHAT_C = -1008765432109
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B"}
DB = pytest.mark.django_db(transaction=True)
LIFECYCLE_LOGGER = lifecycle.__name__
NO_UNPIN_RIGHTS = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: not enough rights to unpin a message",
}
UNPIN_GONE = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: message to unpin not found",
}
BAD_GATEWAY = {"ok": False, "error_code": 502, "description": "Bad Gateway"}


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _monitored(location_factory: Callable[..., Any], since: datetime = SINCE, **kw: Any) -> Any:
    """A location on since ``since``, with its open on piece (a monitored location)."""
    location = location_factory(**kw)
    monitor(location, since)
    return location


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass, with the detection cursor moved to the clock's now (never back).

    The worker's detection thread keeps the cursor within a cycle of now, so an older
    record's day has settled and Phase 3 would make its final edit: a stale record must
    get its release instead (D-08).
    """
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
    return io_loop.run_iteration(clock, state, charts=True)


def _seed(
    location: Any,
    day: date = TODAY,
    *,
    message_id: int,
    pinned: bool = True,
    rendered: datetime = NOON_05,
) -> ChartMessage:
    """A record an earlier pass left, in the location's chat, by the location's bot."""
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=location.chat_id,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=rendered,
        created_at=rendered,
    )


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The lifecycle's WARNING (and worse) lines, in order."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LIFECYCLE_LOGGER and r.levelno >= logging.WARNING
    ]


def _rows() -> list[ChartMessage]:
    return list(ChartMessage.objects.order_by("id"))


def _requests(fake: Any, start: int = 0) -> list[tuple[str, str]]:
    """(bot label, Bot API method) of every request from index ``start`` on, failed ones too."""
    out = []
    for call in list(fake.calls)[start:]:
        token, method = call.request.url.split("/bot", 1)[1].split("/", 1)
        out.append((BOTS[token], method))
    return out


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


def _location(
    location_id: int = 1,
    token: str = DEFAULT_BOT_TOKEN,
    chat_id: int = DEFAULT_CHAT_ID,
    *,
    deleted: bool = False,
) -> lifecycle.ChartLocation:
    """A location as the planner sees it: period 60 s, grace 30 s."""
    settle = lifecycle.settle_time(60, 30, False)
    return lifecycle.ChartLocation(
        location_id, f"L{location_id}", "en", token, chat_id, settle, deleted=deleted
    )


def _row(
    row_id: int,
    location_id: int = 1,
    *,
    day: date = TODAY,
    chat_id: int = DEFAULT_CHAT_ID,
    token: str = DEFAULT_BOT_TOKEN,
    pinned: bool = True,
    rendered: datetime = NOON_05,
) -> lifecycle.ChartRow:
    """A record of ``location_id`` in ``chat_id``, posted by the bot ``token``."""
    return lifecycle.ChartRow(
        id=row_id,
        location_id=location_id,
        local_date=day,
        chat_id=chat_id,
        message_id=1000 + row_id,
        pinned=pinned,
        pin_failed_at=None,
        last_rendered_at=rendered,
        finalized_at=None,
        unpinned_at=None,
        bot_key=io_loop.bot_key(token),
    )


# INV-19 #2, D-08: a chat change moves the chart (tracer)


@DB
def test_INV19_2_chat_change_moves_the_chart(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    # Today's chart is posted and pinned in the location's chat.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    [old] = _rows()
    assert (old.message_id, old.chat_id, old.pinned) == (1001, DEFAULT_CHAT_ID, True)

    # The admin moves the location to chat B.
    Location.objects.filter(pk=location.pk).update(chat_id=CHAT_B)
    clock.advance(seconds=30)
    start = len(fake_telegram.calls)
    assert _pass(clock, state) is True

    # Exactly one call: the unpin, in the stored chat, by its message id, with the
    # location's bot; no photo goes before it (Pitfall 1).
    assert _requests(fake_telegram, start) == [("A", "unpinChatMessage")]
    assert json.loads(fake_telegram.calls[start].request.body) == {
        "chat_id": DEFAULT_CHAT_ID,
        "message_id": 1001,
    }
    old.refresh_from_db()
    assert (old.retired_at, old.pinned) == (clock.now(), False)

    # The next pass posts today's chart in chat B and records it there...
    assert _pass(clock, state) is True
    assert _requests(fake_telegram, start + 1) == [("A", "sendPhoto")]
    new = ChartMessage.objects.get(location=location, retired_at__isnull=True)
    assert (new.message_id, new.chat_id, new.pinned) == (1002, CHAT_B, False)
    assert new.bot_key == io_loop.bot_key(DEFAULT_BOT_TOKEN)
    # ...and the one after pins it there.
    assert _pass(clock, state) is True
    new.refresh_from_db()
    assert new.pinned is True
    # The old record is never called again: the refresh 15 min later edits chat B's chart.
    clock.advance(minutes=15)
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False

    assert _calls(fake_telegram) == [
        ("sendPhoto", DEFAULT_CHAT_ID, None),
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("sendPhoto", CHAT_B, None),
        ("pinChatMessage", CHAT_B, 1002),
        ("editMessageMedia", CHAT_B, 1002),
    ]
    later = _calls(fake_telegram)[3:]
    assert all(message_id != 1001 for _, _, message_id in later)
    # Only the chart's own message is ever unpinned, never the admin's pins (T-04-26).
    assert not any(
        call.request.url.endswith("/unpinAllChatMessages") for call in fake_telegram.calls
    )
    pinned = ChartMessage.objects.filter(pinned=True).values_list("chat_id", "message_id")
    assert list(pinned) == [(CHAT_B, 1002)]


# stale(): the record's channel is (bot, chat), and a deleted location's records are stale


def test_stale_when_the_chat_the_bot_or_the_location_changed() -> None:
    location = _location()

    # Same chat, same bot: the record is the location's.
    assert lifecycle.stale(location, _row(10)) is False
    assert lifecycle.stale(location, _row(11, day=YESTERDAY)) is False
    # Another chat, another bot, or a deleted location: released.
    assert lifecycle.stale(location, _row(10, chat_id=CHAT_B)) is True
    assert lifecycle.stale(location, _row(10, token=TOKEN_B)) is True
    assert lifecycle.stale(_location(deleted=True), _row(10)) is True
    # The location moved to chat B with bot B: a record already there by bot B is its own.
    moved = _location(token=TOKEN_B, chat_id=CHAT_B)
    assert lifecycle.stale(moved, _row(10, chat_id=CHAT_B, token=TOKEN_B)) is False
    assert lifecycle.stale(moved, _row(10, chat_id=CHAT_B)) is True


def test_stale_ignores_the_name_and_the_language() -> None:
    # A change of only the name or the language needs no cleanup (Phase 3 D-14).
    renamed = dataclasses.replace(_location(), name="Дача", language="uk")

    assert lifecycle.stale(renamed, _row(10)) is False


def test_stale_record_without_a_bot_key_is_a_mismatch() -> None:
    # An empty key names no bot: it is released like any other mismatch (no special case).
    nameless = dataclasses.replace(_row(10), bot_key="")

    assert lifecycle.stale(_location(), nameless) is True


# plan(): release first, and nothing else for that location meanwhile (Pitfall 1)


def test_plan_releases_a_stale_record_and_never_posts_meanwhile() -> None:
    moved = _location(chat_id=CHAT_B)
    old_today = _row(10, rendered=NOON_05 - timedelta(hours=1))

    # The old today-record is released; no post in chat B, no refresh of the old chart.
    assert lifecycle.plan([moved], [old_today], today=TODAY, now=NOON_05, not_before={}) == (
        lifecycle.Action("release", moved, old_today)
    )
    # While its release waits (its own key, or the stored chat's channel for the current
    # bot), the location makes no step at all: never a post (Pitfall 1).
    later = NOON_05 + timedelta(seconds=30)
    for held in (
        {lifecycle.chart_key(1, "release", 10): later},
        {io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID): later},
        {io_loop.bot_wide_key(DEFAULT_BOT_TOKEN): later},
    ):
        assert (
            lifecycle.plan([moved], [old_today], today=TODAY, now=NOON_05, not_before=held) is None
        )
    # Another location's chart work goes on meanwhile.
    other = _location(2, TOKEN_B, CHAT_B)
    held = {lifecycle.chart_key(1, "release", 10): later}
    assert lifecycle.plan(
        [moved, other], [old_today], today=TODAY, now=NOON_05, not_before=held
    ) == lifecycle.Action("post", other)
    # Once the record is retired (it leaves the snapshot), today's chart is posted.
    assert lifecycle.plan([moved], [], today=TODAY, now=NOON_05, not_before={}) == (
        lifecycle.Action("post", moved)
    )


def test_plan_releases_stale_records_oldest_first() -> None:
    moved = _location(chat_id=CHAT_B)
    today_row = _row(5)
    older = _row(9, day=YESTERDAY, rendered=NOON_05 - timedelta(days=1))
    oldest = _row(12, day=date(2026, 9, 30), rendered=NOON_05 - timedelta(days=2))
    rows = [today_row, older, oldest]

    # By local date, then id, whatever the snapshot order; one release per pass.
    assert lifecycle.plan([moved], rows, today=TODAY, now=NOON_05, not_before={}) == (
        lifecycle.Action("release", moved, oldest)
    )
    assert lifecycle.plan(
        [moved], [today_row, older], today=TODAY, now=NOON_05, not_before={}
    ) == lifecycle.Action("release", moved, older)
    # A release that waits lets the next stale record of the location go.
    held = {lifecycle.chart_key(1, "release", 12): NOON_05 + timedelta(seconds=30)}
    assert lifecycle.plan([moved], rows, today=TODAY, now=NOON_05, not_before=held) == (
        lifecycle.Action("release", moved, older)
    )
    # A settled older record is still released, never finalized (D-08).
    assert lifecycle.plan(
        [moved], [today_row, older], today=TODAY, now=NOON_05, not_before={}, settled={9}
    ) == lifecycle.Action("release", moved, older)


def test_plan_deleted_location_only_releases() -> None:
    # A deleted location's records are released, and it never gets a new post (D-09).
    gone = _location(deleted=True)
    today_row = _row(10, rendered=NOON_05 - timedelta(hours=1))

    assert lifecycle.plan([gone], [today_row], today=TODAY, now=NOON_05, not_before={}) == (
        lifecycle.Action("release", gone, today_row)
    )
    assert lifecycle.plan([gone], [], today=TODAY, now=NOON_05, not_before={}) is None


# INV-19 #2, D-08: a token change is a channel change


@DB
def test_INV19_2_token_change_is_a_channel_change(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    [old] = _rows()
    assert (old.message_id, old.pinned, old.bot_key) == (
        1001,
        True,
        io_loop.bot_key(DEFAULT_BOT_TOKEN),
    )

    # The admin gives the location a new bot; the chat stays.
    Location.objects.filter(pk=location.pk).update(bot_token=TOKEN_B)
    start = len(fake_telegram.calls)
    assert _pass(clock, state) is True

    # One unpin through the new bot, in the stored chat, by the old message id.
    assert _requests(fake_telegram, start) == [("B", "unpinChatMessage")]
    assert json.loads(fake_telegram.calls[start].request.body) == {
        "chat_id": DEFAULT_CHAT_ID,
        "message_id": 1001,
    }
    old.refresh_from_db()
    assert (old.retired_at, old.pinned) == (NOON_05, False)
    # Then today's chart is posted and pinned by the new bot in the location's chat.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False
    assert _requests(fake_telegram, start) == [
        ("B", "unpinChatMessage"),
        ("B", "sendPhoto"),
        ("B", "pinChatMessage"),
    ]
    new = ChartMessage.objects.get(retired_at__isnull=True)
    assert (new.message_id, new.chat_id, new.pinned, new.bot_key) == (
        1002,
        DEFAULT_CHAT_ID,
        True,
        io_loop.bot_key(TOKEN_B),
    )
    assert _calls(fake_telegram)[-1] == ("pinChatMessage", DEFAULT_CHAT_ID, 1002)


# INV-19 #2, D-09: a deleted location's chart is unpinned and never posted again


@DB
def test_INV19_2_deleted_location_is_unpinned_and_never_posted_again(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    [record] = _rows()
    assert (record.message_id, record.pinned) == (1001, True)

    # The admin deletes the location (the tombstone keeps its token and chat).
    Location.objects.filter(pk=location.pk).update(deleted_at=NOON_05)
    locations, rows = lifecycle.read_snapshot(TODAY)
    assert [(loc.location_id, loc.deleted) for loc in locations] == [(location.pk, True)]
    assert [row.id for row in rows] == [record.pk]
    start = len(fake_telegram.calls)
    assert _pass(clock, state) is True

    # One unpin in the stored chat with the tombstone's bot; the record is retired.
    assert _requests(fake_telegram, start) == [("A", "unpinChatMessage")]
    assert json.loads(fake_telegram.calls[start].request.body) == {
        "chat_id": DEFAULT_CHAT_ID,
        "message_id": 1001,
    }
    record.refresh_from_db()
    assert (record.retired_at, record.pinned, record.finalized_at) == (NOON_05, False, None)
    # The deleted location leaves the snapshot and never gets another chart call: no
    # refresh 15 min later, no post after the next midnight, no final edit.
    assert lifecycle.read_snapshot(TODAY) == ([], [])
    for at in (NOON_05, NOON_05 + timedelta(minutes=15), kyiv("2026-10-03 00:05")):
        clock.set(at)
        assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == start + 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 0
    assert state.not_before == {}


@DB
def test_snapshot_keeps_a_deleted_location_only_while_it_has_active_records(
    location_factory: Callable[..., Any],
) -> None:
    live = _monitored(location_factory)
    # Deleted with today's chart still active: in the snapshot, marked deleted.
    pending = _monitored(location_factory)
    _seed(pending, message_id=501)
    # Deleted with yesterday's chart finalized but not unpinned yet: still in it.
    unpinning = _monitored(location_factory)
    older = _seed(unpinning, YESTERDAY, message_id=601, rendered=NOON_05 - timedelta(days=1))
    ChartMessage.objects.filter(pk=older.pk).update(finalized_at=NOON_05)
    # Deleted with only a retired record, or a finished older one, or none: gone.
    retired = _monitored(location_factory)
    ChartMessage.objects.filter(pk=_seed(retired, message_id=701).pk).update(
        retired_at=NOON_05, pinned=False
    )
    finished = _monitored(location_factory)
    done = _seed(finished, YESTERDAY, message_id=801, pinned=False)
    ChartMessage.objects.filter(pk=done.pk).update(finalized_at=NOON_05, unpinned_at=NOON_05)
    bare = _monitored(location_factory)
    gone = (pending, unpinning, retired, finished, bare)
    Location.objects.filter(pk__in=[loc.pk for loc in gone]).update(deleted_at=NOON_05)

    locations, rows = lifecycle.read_snapshot(TODAY)

    assert [(loc.location_id, loc.deleted) for loc in locations] == [
        (live.pk, False),
        (pending.pk, True),
        (unpinning.pk, True),
    ]
    assert sorted(row.message_id for row in rows) == [501, 601]


# Older records are released too, oldest first, and never finalized (D-08)


@DB
def test_moved_location_releases_older_records_without_a_final_edit(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    # Yesterday's chart: not finalized, not unpinned, its day long settled (``_pass``).
    yesterday = _seed(location, YESTERDAY, message_id=501, rendered=kyiv("2026-10-01 23:45"))
    today = _seed(location, message_id=502)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    Location.objects.filter(pk=location.pk).update(chat_id=CHAT_B)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    passes = []
    while True:
        start = len(fake_telegram.chart_calls)
        if not _pass(clock, state):
            break
        passes.append(_calls(fake_telegram)[start:])
        assert len(passes) < 10

    # One call per pass: two releases, oldest first, then the post and pin in chat B.
    assert passes == [
        [("unpinChatMessage", DEFAULT_CHAT_ID, 501)],
        [("unpinChatMessage", DEFAULT_CHAT_ID, 502)],
        [("sendPhoto", CHAT_B, None)],
        [("pinChatMessage", CHAT_B, 1001)],
    ]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 0
    for record in (yesterday, today):
        record.refresh_from_db()
        assert (record.retired_at, record.pinned, record.finalized_at) == (NOON_05, False, None)


# Best effort: a release that cannot succeed retires the record and never loops (INV-19)


@DB
def test_release_permanent_error_retires_and_moves_on(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    Location.objects.filter(pk=location.pk).update(chat_id=CHAT_B)
    # The bot may not unpin in the old chat (Open Edge 6); the later calls are accepted.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "unpinChatMessage", status=400, json_body=NO_UNPIN_RIGHTS
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (NOON_05, False)
    # Done for good: no backoff, no failure count, one WARNING with the short code.
    assert state.not_before == {}
    assert state.chart_failures == {}
    [line] = _lines(caplog)
    assert "release" in line and str(location.pk) in line and str(record.pk) in line
    assert "http_400" in line
    assert "not enough rights" not in caplog.text
    secret = DEFAULT_BOT_TOKEN.split(":", 1)[1]
    assert secret not in caplog.text
    # No ops notice and no incident in v1 (the accepted Open Edge 6 risk).
    assert not OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS).exists()
    assert not OpsIncident.objects.exists()
    # The next pass posts in the new chat; the old record is never called again.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False
    assert _requests(fake_telegram) == [
        ("A", "unpinChatMessage"),
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
    ]
    assert _calls(fake_telegram) == [
        ("sendPhoto", CHAT_B, None),
        ("pinChatMessage", CHAT_B, 1001),
    ]


@DB
def test_release_not_found_retires(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    Location.objects.filter(pk=location.pk).update(chat_id=CHAT_B)
    # The old chart was deleted in the old chat.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "unpinChatMessage", status=400, json_body=UNPIN_GONE
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (NOON_05, False)
    assert state.not_before == {}
    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == [("A", "unpinChatMessage"), ("A", "sendPhoto")]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "unpinChatMessage") == 1


# Backoff: a transient release waits under a key _prune keeps, and blocks the post


@DB
def test_release_transient_backs_off_and_blocks_the_post(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    moved = _monitored(location_factory)
    _seed(moved, message_id=501)
    other = _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_C)
    assert moved.pk < other.pk
    Location.objects.filter(pk=moved.pk).update(chat_id=CHAT_B)
    # The old chat's unpin answers 502 twice, then works.
    for _ in range(2):
        fake_telegram.fail_method(
            DEFAULT_BOT_TOKEN, "unpinChatMessage", status=502, json_body=BAD_GATEWAY
        )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    record = ChartMessage.objects.get(location=moved)
    key = lifecycle.chart_key(moved.pk, "release", record.pk)
    bot = io_loop.bot_wide_key(DEFAULT_BOT_TOKEN)

    # 12:05:00: the release answers 502. The bot is held as an alert's 5xx would hold
    # it (2 s), and the release waits step_delay(1) (30 s) longer.
    assert _pass(clock, state) is True
    assert state.not_before == {
        bot: NOON_05 + timedelta(seconds=2),
        key: NOON_05 + timedelta(seconds=32),
    }
    # The other location's chart work goes on; the moved one's key survives every prune.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert state.not_before.get(key) == NOON_05 + timedelta(seconds=32)
    for seconds in (2, 10, 31):
        clock.set(NOON_05 + timedelta(seconds=seconds))
        assert _pass(clock, state) is False
        assert state.not_before.get(key) == NOON_05 + timedelta(seconds=32)
    # 12:05:32: the second try answers 502 too: 4 s bot hold + step_delay(2) (60 s).
    second = NOON_05 + timedelta(seconds=32)
    clock.set(second)
    assert _pass(clock, state) is True
    assert state.not_before.get(key) == second + timedelta(seconds=64)
    assert state.chart_failures.get(key) == 2
    clock.set(second + timedelta(seconds=63))
    assert _pass(clock, state) is False
    # Until the release succeeds, nothing is posted for the moved location (Pitfall 1).
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "unpinChatMessage") == 2
    # 12:06:36: the release works; then today's chart goes to chat B.
    clock.set(second + timedelta(seconds=64))
    assert _pass(clock, state) is True
    assert key not in state.not_before and key not in state.chart_failures
    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == [
        ("A", "unpinChatMessage"),
        ("B", "sendPhoto"),
        ("B", "pinChatMessage"),
        ("A", "unpinChatMessage"),
        ("A", "unpinChatMessage"),
        ("A", "sendPhoto"),
    ]
    assert [(row.location_id, row.chat_id, row.retired_at is None) for row in _rows()] == [
        (moved.pk, DEFAULT_CHAT_ID, False),
        (other.pk, CHAT_C, True),
        (moved.pk, CHAT_B, True),
    ]


# A change of only the name or the language releases nothing (Phase 3 D-14)


@DB
def test_name_or_language_change_releases_nothing(
    location_factory: Callable[..., Any], fake_telegram: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = _monitored(location_factory)
    _seed(location, message_id=501)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    seen: list[tuple[str, str]] = []
    real = render.render_png

    def spy(week: Week, *, lang: str, name: str) -> bytes:
        seen.append((lang, name))
        return real(week, lang=lang, name=name)

    monkeypatch.setattr(render, "render_png", spy)
    Location.objects.filter(pk=location.pk).update(name="Дача", language="uk")
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is False
    clock.advance(minutes=15)
    assert _pass(clock, state) is True

    assert _requests(fake_telegram) == [("A", "editMessageMedia")]
    [edit] = fake_telegram.chart_calls
    assert json.loads(edit.fields["media"])["caption"] == (
        "Сьогодні відключень не було\nОновлено о 12:20"
    )
    assert seen == [("uk", "Дача")]
    assert ChartMessage.objects.get().retired_at is None


# settled_records: a deleted location's or a moved record's day never gets a final edit


def test_settled_records_skip_deleted_locations() -> None:
    older = _row(10, day=YESTERDAY, rendered=NOON_05 - timedelta(days=1))
    moved_older = _row(20, 2, day=YESTERDAY, rendered=NOON_05 - timedelta(days=1))
    later = model.next_midnight(YESTERDAY, KYIV) + timedelta(hours=12)

    def settled(locations: list[lifecycle.ChartLocation]) -> frozenset[int]:
        return lifecycle.settled_records(
            locations, [older, moved_older], today=TODAY, detected_until=later, tz=KYIV
        )

    # Both days have settled while both locations keep their channel.
    assert settled([_location(1), _location(2)]) == {10, 20}
    # A deleted location's record, or a record whose chat moved, is released instead.
    assert settled([_location(1, deleted=True), _location(2, chat_id=CHAT_B)]) == frozenset()
    assert settled([_location(1), _location(2, token=TOKEN_B)]) == {10}
