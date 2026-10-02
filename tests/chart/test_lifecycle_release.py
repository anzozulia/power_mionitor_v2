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
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock

from powermon.chart import lifecycle
from powermon.chart.models import ChartMessage
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
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B"}
DB = pytest.mark.django_db(transaction=True)


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
    return io_loop.run_iteration(clock, state, charts=True)


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
