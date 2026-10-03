"""The chart half of a history reset (INV-19 reset scenario, D-08, DATA-03).

A history reset deletes the location's timeline and sets it back to "waiting for its first
heartbeat"; it also marks every active chart record of the location (``history_reset_at``).
A marked record is ``stale`` whatever its channel, so the worker releases it like a
record left behind by a chat change (Phase 4 D-08): one unpin by its own message id, in
the chat stored with it, with the location's current bot, and the record is retired, so
no final edit, pin or refresh ever targets it again. The release runs even while the
location waits: a waiting location is in the chart snapshot only while it has an active
marked record (``ChartLocation.awaiting_heartbeat``), and gets nothing but releases. The
first heartbeat after the release brings exactly one new pinned chart (Phase 3 D-03).

RESEARCH Pitfall 1: the snapshot is two statements (locations, then records), so a release
that retires the last marked record between them leaves a waiting location with no rows;
``plan`` must still never post or pin for it. RESEARCH Pitfall 12: a refresh chosen just
before the reset commits may edit the old message once, and a post in flight may be
recorded unmarked; that race is accepted and documented in the lifecycle module.

No reset writer exists here: 05-05 adds ``history.reset_history`` and the end-to-end reset
through the web view. ``_reset_by_hand`` writes what that reset writes, in one transaction.

Every test that runs ``io_loop.run_iteration`` (``_pass``) is ``django_db(transaction=True)``,
because the pass calls ``close_old_connections()``. The pure ``stale`` and ``plan`` cases
need no database. Time comes only from the ``FakeClock``; Telegram is faked at the HTTP
boundary (``fake_telegram``); renders are real (Pillow), so each test keeps to a handful.
"""

import dataclasses
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db import connection, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest

from powermon.chart import lifecycle
from powermon.chart.models import ChartMessage
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.worker import io_loop

# Fri 2026-10-02 12:05 local is "now"; the location has been monitored since 10-01 08:00.
TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-02 12:05")
SINCE = kyiv("2026-10-01 08:00")
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A"}
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
    """One I/O pass, with the detection cursor moved to the clock's now (never back).

    The worker's detection thread keeps the cursor within a cycle of now, so an older
    record's day has settled and Phase 3 would make its final edit: a marked record must
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


def _reset_by_hand(location: Any, at: datetime, *, mark: bool = True) -> None:
    """What a history reset writes (05-05 ``reset_history``), in one transaction.

    The location's timeline is deleted, its state goes back to "waiting for its first
    heartbeat" with ``state_version`` bumped, and (``mark``) its active chart records are
    marked ``history_reset_at = at``. ``mark=False`` leaves the records as a restore does
    (05-03): a waiting location with unmarked records.
    """
    with transaction.atomic():
        PowerInterval.objects.filter(location_id=location.pk).delete()
        LocationState.objects.filter(location_id=location.pk).update(
            status="waiting",
            last_heartbeat_at=None,
            on_since=None,
            outage_started_at=None,
            window_start_at=None,
            state_version=F("state_version") + 1,
        )
        if mark:
            ChartMessage.objects.filter(
                location_id=location.pk, retired_at__isnull=True, history_reset_at__isnull=True
            ).update(history_reset_at=at)


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


def _no_unpin_all(fake: Any) -> bool:
    """No request ever used unpinAllChatMessages: the admin's own pins stay (D-08, INV-19)."""
    return not any(call.request.url.endswith("/unpinAllChatMessages") for call in fake.calls)


# INV-19 (reset), D-08: the old chart is unpinned in its own chat while the location waits
# (tracer)


@DB
def test_INV19_reset_unpins_the_old_chart_while_the_location_waits(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    # Today's chart is posted and pinned in the location's chat by earlier passes.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    [old] = _rows()
    assert (old.message_id, old.chat_id, old.pinned, old.history_reset_at) == (
        1001,
        DEFAULT_CHAT_ID,
        True,
        None,
    )

    # The admin resets the location's history 30 s later.
    clock.advance(seconds=30)
    _reset_by_hand(location, clock.now())
    old.refresh_from_db()
    assert old.history_reset_at == clock.now()
    clock.advance(seconds=5)
    start = len(fake_telegram.calls)
    assert _pass(clock, state) is True

    # Exactly one call: the unpin, in the stored chat, by its message id, with the
    # location's bot, although the location is waiting for its first heartbeat.
    assert _requests(fake_telegram, start) == [("A", "unpinChatMessage")]
    assert json.loads(fake_telegram.calls[start].request.body) == {
        "chat_id": DEFAULT_CHAT_ID,
        "message_id": 1001,
    }
    old.refresh_from_db()
    assert (old.retired_at, old.pinned, old.finalized_at) == (clock.now(), False, None)

    # While the location waits, nothing more is sent: no photo, no pin, no edit.
    for _ in range(3):
        clock.advance(minutes=10)
        assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == start + 1
    assert _calls(fake_telegram) == [
        ("sendPhoto", DEFAULT_CHAT_ID, None),
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 1001),
    ]
    assert _no_unpin_all(fake_telegram)
    assert lifecycle.read_snapshot(TODAY) == ([], [])
    assert state.not_before == {}


# Migration 0010: a nullable marker with no default; NULL means "not reset"


@pytest.mark.django_db
def test_migration_0010_adds_a_nullable_history_reset_at(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()

    record = _seed(location, message_id=501)

    record.refresh_from_db()
    assert record.history_reset_at is None
    with connection.cursor() as cur:
        cur.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'chart_message' AND column_name = 'history_reset_at'"
        )
        assert cur.fetchall() == [("YES", None)]


# Failure direction: a waiting location whose records are not marked gets no chart call


@DB
def test_unmarked_waiting_location_makes_no_chart_call(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    # The location waits with its timeline gone, but its record is not marked (as after a
    # restore, 05-03): there is nothing to release, and a waiting location is never posted.
    _reset_by_hand(location, NOON_05, mark=False)
    clock = FakeClock(NOON_05 + timedelta(seconds=30))
    state = io_loop.RelayState()

    assert _pass(clock, state) is False
    clock.advance(minutes=15)
    assert _pass(clock, state) is False

    assert len(fake_telegram.calls) == 0
    assert lifecycle.read_snapshot(TODAY)[0] == []
    record.refresh_from_db()
    assert (record.retired_at, record.pinned, record.history_reset_at) == (None, True, None)
