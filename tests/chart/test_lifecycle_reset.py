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

05-02's tests emulate the reset with ``_reset_by_hand``, which writes what
``history.reset_history`` writes, in one transaction. 05-05's tests use the real reset: the
location page's POST (``HistoryResetView`` on a RequestFactory request with an injected
clock) or ``history.reset_history`` itself. No admin action calls Telegram: the unpin is
the worker's, made in an I/O pass.

Every test that runs ``io_loop.run_iteration`` (``_pass``) is ``django_db(transaction=True)``,
because the pass calls ``close_old_connections()``. The pure ``stale`` and ``plan`` cases
need no database. Time comes only from the ``FakeClock``; Telegram is faked at the HTTP
boundary (``fake_telegram``); renders are real (Pillow), so each test keeps to a handful.
"""

import dataclasses
import json
import logging
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, OPS_BOT_TOKEN, FakeClock
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.http import HttpResponse
from django.test import RequestFactory

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle, model, source
from powermon.chart.models import ChartMessage
from powermon.engine import history, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.web.history_views import HISTORY_RESET_MESSAGE, HistoryResetView
from powermon.worker import io_loop

# Fri 2026-10-02 12:05 local is "now"; the location has been monitored since 10-01 08:00.
TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-02 12:05")
SINCE = kyiv("2026-10-01 08:00")
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A", OPS_BOT_TOKEN: "ops"}
DB = pytest.mark.django_db(transaction=True)
LIFECYCLE_LOGGER = lifecycle.__name__
# The bot may not unpin in the channel any more (best effort: the record is retired).
NO_RIGHTS = {
    "ok": False,
    "error_code": 403,
    "description": "Forbidden: not enough rights to unpin a message",
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


@pytest.fixture(autouse=True)
def never_unpin_all(request: pytest.FixtureRequest) -> Iterator[None]:
    """After every test that fakes Telegram: no request used unpinAllChatMessages (D-08).

    The fake is taken before the test runs, so this check runs before the fake's own
    teardown resets its request list.
    """
    fake = (
        request.getfixturevalue("fake_telegram")
        if "fake_telegram" in request.fixturenames
        else None
    )
    yield
    if fake is not None:
        assert _no_unpin_all(fake), "a request used unpinAllChatMessages"


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


def _reset_post(
    rf: RequestFactory, location: Any, clock: FakeClock
) -> tuple[HttpResponse, list[str]]:
    """POST the reset to the location page's view with ``clock``; the response and flashes."""
    request = rf.post(f"/locations/{location.pk}/reset/")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    response = HistoryResetView.as_view(clock=clock)(request, pk=location.pk)
    return response, [str(m) for m in request._messages]  # type: ignore[attr-defined]


def _state(location: Any) -> tuple[Any, ...]:
    """The location's live state: status and the four times the reset clears."""
    return LocationState.objects.filter(location_id=location.pk).values_list(
        "status", "last_heartbeat_at", "on_since", "outage_started_at", "window_start_at"
    )[0]


def _rows() -> list[ChartMessage]:
    return list(ChartMessage.objects.order_by("id"))


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The lifecycle's WARNING (and worse) lines, in order."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LIFECYCLE_LOGGER and r.levelno >= logging.WARNING
    ]


def _all_passes(
    clock: FakeClock, state: io_loop.RelayState, fake: Any
) -> list[list[tuple[str, int, int | None]]]:
    """The accepted chart calls of each pass, until a pass makes no call."""
    passes = []
    while True:
        start = len(fake.chart_calls)
        if not _pass(clock, state):
            return passes
        passes.append(_calls(fake)[start:])
        assert len(passes) < 10


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


def _location(
    location_id: int = 1,
    token: str = DEFAULT_BOT_TOKEN,
    chat_id: int = DEFAULT_CHAT_ID,
    *,
    deleted: bool = False,
    awaiting_heartbeat: bool = False,
) -> lifecycle.ChartLocation:
    """A location as the planner sees it: period 60 s, grace 30 s."""
    settle = lifecycle.settle_time(60, 30, False)
    return lifecycle.ChartLocation(
        location_id,
        f"L{location_id}",
        "en",
        token,
        chat_id,
        settle,
        deleted=deleted,
        awaiting_heartbeat=awaiting_heartbeat,
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
    history_reset_at: datetime | None = None,
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
        history_reset_at=history_reset_at,
    )


def _keys_of(location_id: int, keys: set[str]) -> set[str]:
    """The chart step keys of one location."""
    return {key for key in keys if key.startswith(f"chart:{location_id}:")}


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


# INV-19 (reset), DATA-03 end to end: the admin's reset POST, then the worker's next pass
# (05-05 tracer)


@DB
def test_INV19_reset_from_the_location_page_unpins_the_old_chart(
    location_factory: Callable[..., Any], fake_telegram: Any, rf: RequestFactory
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    # Today's chart is posted and pinned in the location's chat by earlier passes.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    [old] = _rows()
    assert (old.message_id, old.chat_id, old.pinned) == (1001, DEFAULT_CHAT_ID, True)
    clock.advance(seconds=30)
    calls = len(fake_telegram.calls)

    response, flashes = _reset_post(rf, location, clock)

    assert response.status_code == 302
    assert response.url == f"/locations/{location.pk}/"
    assert flashes == [HISTORY_RESET_MESSAGE]
    # The timeline is gone and the location waits for its first heartbeat.
    assert not PowerInterval.objects.filter(location_id=location.pk).exists()
    assert _state(location) == ("waiting", None, None, None, None)
    # The record is marked, never retired by the web: the worker releases it.
    old.refresh_from_db()
    assert (old.history_reset_at, old.retired_at, old.pinned) == (clock.now(), None, True)
    # The POST made no Telegram call (KD2).
    assert len(fake_telegram.calls) == calls

    clock.advance(seconds=5)
    assert _pass(clock, state) is True

    # The next pass unpins the old chart by its own id, in its stored chat, with the
    # location's bot, and retires its record.
    assert _requests(fake_telegram, calls) == [("A", "unpinChatMessage")]
    assert json.loads(fake_telegram.calls[calls].request.body) == {
        "chat_id": DEFAULT_CHAT_ID,
        "message_id": 1001,
    }
    old.refresh_from_db()
    assert (old.retired_at, old.pinned, old.finalized_at) == (clock.now(), False, None)
    assert _pass(clock, state) is False


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


# stale(): a reset-marked record is released whatever its channel (D-08)


def test_D08_stale_when_the_record_is_reset_marked() -> None:
    location = _location()

    # The location's own chat and bot: unmarked it is the location's, marked it is released.
    assert lifecycle.stale(location, _row(10)) is False
    assert lifecycle.stale(location, _row(10, history_reset_at=NOON_05)) is True
    assert lifecycle.stale(location, _row(11, day=YESTERDAY, history_reset_at=NOON_05)) is True
    # The marker alone decides: a waiting location's unmarked record is not stale.
    waiting = _location(awaiting_heartbeat=True)
    assert lifecycle.stale(waiting, _row(10)) is False
    assert lifecycle.stale(waiting, _row(10, history_reset_at=NOON_05)) is True


# plan(): a waiting location makes releases only, oldest first (D-08, Pitfall 1)


def test_D08_plan_waiting_location_only_releases() -> None:
    waiting = _location(awaiting_heartbeat=True)
    # Today's record is not pinned and due for a refresh, yesterday's day has settled and
    # it was never unpinned: a monitored location would pin, refresh, finalize and unpin.
    today_row = _row(
        10, pinned=False, rendered=NOON_05 - timedelta(hours=1), history_reset_at=NOON_05
    )
    older = _row(9, day=YESTERDAY, rendered=NOON_05 - timedelta(days=1), history_reset_at=NOON_05)
    rows = [today_row, older]
    later = NOON_05 + timedelta(seconds=30)
    older_held = {lifecycle.chart_key(1, "release", 9): later}
    both_held = {**older_held, lifecycle.chart_key(1, "release", 10): later}

    # The older record first, though its final edit is due (no final edit, D-08).
    assert lifecycle.plan(
        [waiting], rows, today=TODAY, now=NOON_05, not_before={}, settled={9}
    ) == lifecycle.Action("release", waiting, older)
    # Its release backing off lets today's record go.
    assert lifecycle.plan(
        [waiting], rows, today=TODAY, now=NOON_05, not_before=older_held, settled={9}
    ) == lifecycle.Action("release", waiting, today_row)
    # Both backing off: no step at all for the location.
    assert (
        lifecycle.plan([waiting], rows, today=TODAY, now=NOON_05, not_before=both_held, settled={9})
        is None
    )
    # Never a post, pin, refresh, final edit or unpin, whatever is left or held.
    for not_before in ({}, older_held, both_held):
        for left in (rows, [today_row], [older], []):
            action = lifecycle.plan(
                [waiting], left, today=TODAY, now=NOON_05, not_before=not_before, settled={9}
            )
            assert action is None or action.step == "release"


def test_D08_plan_waiting_location_without_stale_rows_plans_nothing() -> None:
    # RESEARCH Pitfall 1: the release retired the last marked record between the
    # snapshot's two reads, so the waiting location comes without rows.
    waiting = _location(awaiting_heartbeat=True)

    assert lifecycle.plan([waiting], [], today=TODAY, now=NOON_05, not_before={}) is None
    assert _keys_of(1, lifecycle._live_keys([waiting], [], TODAY)) == set()
    # Pitfall 12: a post in flight at the reset was recorded unmarked. It is not stale, and
    # the waiting location still makes no step: no pin, no refresh.
    unmarked = _row(10, pinned=False, rendered=NOON_05 - timedelta(hours=1))
    assert lifecycle.plan([waiting], [unmarked], today=TODAY, now=NOON_05, not_before={}) is None
    assert _keys_of(1, lifecycle._live_keys([waiting], [unmarked], TODAY)) == set()
    # Another location's chart work goes on meanwhile.
    other = _location(2)
    assert lifecycle.plan(
        [waiting, other], [], today=TODAY, now=NOON_05, not_before={}
    ) == lifecycle.Action("post", other)
    assert lifecycle._live_keys([waiting, other], [], TODAY) == {lifecycle.chart_key(2, "post")}


def test_D08_live_keys_keep_a_backing_off_release() -> None:
    waiting = _location(awaiting_heartbeat=True)
    marked = _row(10, history_reset_at=NOON_05)
    release = lifecycle.chart_key(1, "release", 10)
    post = lifecycle.chart_key(1, "post")
    later = NOON_05 + timedelta(seconds=30)

    assert lifecycle._live_keys([waiting], [marked], TODAY) == {release}
    # The prune keeps the release's backoff and drops the location's other keys.
    state = io_loop.RelayState()
    state.not_before.update({release: later, post: later})
    state.chart_failures.update({release: 1, post: 1})
    lifecycle._prune(state, [waiting], [marked], TODAY)
    assert (state.not_before, state.chart_failures) == ({release: later}, {release: 1})
    # On again (the first heartbeat) with the marked record still active: the same.
    on_again = _location()
    assert lifecycle._live_keys([on_again], [marked], TODAY) == {release}
    # Once the record is retired (it leaves the snapshot), today's post key is live.
    assert lifecycle._live_keys([on_again], [], TODAY) == {post}


def test_existing_lifecycle_rows_and_locations_build_with_the_new_defaults() -> None:
    # The Phase 3/4 tests build both without the new fields: they default to "not reset"
    # and "not waiting", and nothing changes for them.
    settle = lifecycle.settle_time(60, 30, False)
    location = lifecycle.ChartLocation(1, "L1", "en", DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, settle)
    row = lifecycle.ChartRow(
        id=10,
        location_id=1,
        local_date=TODAY,
        chat_id=DEFAULT_CHAT_ID,
        message_id=1010,
        pinned=True,
        pin_failed_at=None,
        last_rendered_at=NOON_05 - timedelta(hours=1),
        finalized_at=None,
        unpinned_at=None,
        bot_key=io_loop.bot_key(DEFAULT_BOT_TOKEN),
    )

    assert (location.deleted, location.awaiting_heartbeat) == (False, False)
    assert row.history_reset_at is None
    assert [f.name for f in dataclasses.fields(row)][-1] == "history_reset_at"
    assert lifecycle.stale(location, row) is False
    assert lifecycle.plan(
        [location], [row], today=TODAY, now=NOON_05, not_before={}
    ) == lifecycle.Action("refresh", location, row)


# read_snapshot(): a waiting location is in it only while it has a marked record (D-08)


@DB
def test_D08_snapshot_keeps_a_waiting_location_only_while_it_has_a_marked_record(
    location_factory: Callable[..., Any],
) -> None:
    live = _monitored(location_factory)
    reset = _monitored(location_factory)
    marked = _seed(reset, message_id=501)
    _reset_by_hand(reset, NOON_05)
    # Waiting after a restore (05-03): its record is not marked, so it is not in it.
    restored = _monitored(location_factory)
    _seed(restored, message_id=601)
    _reset_by_hand(restored, NOON_05, mark=False)
    # Waiting with no record at all (never monitored): not in it either.
    location_factory()

    locations, rows = lifecycle.read_snapshot(TODAY)

    assert [(loc.location_id, loc.deleted, loc.awaiting_heartbeat) for loc in locations] == [
        (live.pk, False, False),
        (reset.pk, False, True),
    ]
    reset_rows = [row for row in rows if row.location_id == reset.pk]
    assert [(row.id, row.history_reset_at) for row in reset_rows] == [(marked.pk, NOON_05)]
    assert [row.history_reset_at for row in rows if row.location_id != reset.pk] == [None]
    # The release retires the marked record: the waiting location leaves the snapshot.
    ChartMessage.objects.filter(pk=marked.pk).update(retired_at=NOON_05, pinned=False)
    locations, rows = lifecycle.read_snapshot(TODAY)
    assert [(loc.location_id, loc.awaiting_heartbeat) for loc in locations] == [(live.pk, False)]
    assert [row.location_id for row in rows] == [restored.pk]


@DB
def test_D08_snapshot_ignores_marked_records_outside_the_rows_predicate(
    location_factory: Callable[..., Any],
) -> None:
    # A marked older record already finalized and unpinned is done: no release is owed.
    finished = _monitored(location_factory)
    done = _seed(finished, YESTERDAY, message_id=501, pinned=False)
    ChartMessage.objects.filter(pk=done.pk).update(finalized_at=NOON_05, unpinned_at=NOON_05)
    # A marked record already retired: gone.
    retired = _monitored(location_factory)
    gone = _seed(retired, message_id=601)
    ChartMessage.objects.filter(pk=gone.pk).update(retired_at=NOON_05, pinned=False)
    # A marked older record finalized but not unpinned yet: still owed its release.
    unpinning = _monitored(location_factory)
    owed = _seed(unpinning, YESTERDAY, message_id=701)
    ChartMessage.objects.filter(pk=owed.pk).update(finalized_at=NOON_05)
    for location in (finished, retired, unpinning):
        _reset_by_hand(location, NOON_05)
    ChartMessage.objects.update(history_reset_at=NOON_05)

    locations, rows = lifecycle.read_snapshot(TODAY)

    assert [(loc.location_id, loc.awaiting_heartbeat) for loc in locations] == [
        (unpinning.pk, True)
    ]
    assert [row.message_id for row in rows] == [701]


# An older marked record is released with one unpin and never finalized (D-08, INV-19)


@DB
def test_D08_older_marked_record_is_released_not_finalized(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    # Yesterday's chart: pinned, not finalized, its day long settled (``_pass``).
    yesterday = _seed(location, YESTERDAY, message_id=501, rendered=kyiv("2026-10-01 23:45"))
    today = _seed(location, message_id=502)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    _reset_by_hand(location, NOON_05)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    passes = _all_passes(clock, state, fake_telegram)

    # One unpin per pass, yesterday's first; no final edit, and nothing after them.
    assert passes == [
        [("unpinChatMessage", DEFAULT_CHAT_ID, 501)],
        [("unpinChatMessage", DEFAULT_CHAT_ID, 502)],
    ]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "editMessageMedia") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 0
    for record in (yesterday, today):
        record.refresh_from_db()
        assert (record.retired_at, record.pinned, record.finalized_at) == (NOON_05, False, None)
    assert _no_unpin_all(fake_telegram)


# Best effort: a release that cannot succeed retires the record and never loops (INV-19)


@DB
def test_D08_permanent_release_error_retires_with_one_warning(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    _reset_by_hand(location, NOON_05)
    # The bot may not unpin in the channel any more; later calls would be accepted.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "unpinChatMessage", status=403, json_body=NO_RIGHTS
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(NOON_05 + timedelta(seconds=5))
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (clock.now(), False)
    # Done for good: no backoff, no failure count, one WARNING with the short code.
    assert state.not_before == {}
    assert state.chart_failures == {}
    [line] = _lines(caplog)
    assert "release" in line and str(location.pk) in line and str(record.pk) in line
    assert "http_403" in line
    # Never the token (in any form) or Telegram's description in any log line (OPS-08).
    secret = DEFAULT_BOT_TOKEN.split(":", 1)[1]
    assert secret not in caplog.text and DEFAULT_BOT_TOKEN not in caplog.text
    assert "not enough rights" not in caplog.text
    # No ops notice and no incident in v1 (the accepted Open Edge 6 risk).
    assert not OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS).exists()
    assert not OpsIncident.objects.exists()
    # The waiting location gets nothing else, and the release is never tried again.
    clock.advance(minutes=15)
    assert _pass(clock, state) is False
    assert _requests(fake_telegram) == [("A", "unpinChatMessage")]


@DB
def test_D08_release_not_found_retires(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    _reset_by_hand(location, NOON_05)
    # The old chart was deleted in the channel.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "unpinChatMessage", status=400, json_body=UNPIN_GONE
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    assert _pass(clock, state) is True

    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (NOON_05, False)
    assert state.not_before == {}
    assert _lines(caplog) == []
    assert _pass(clock, state) is False
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "unpinChatMessage") == 1


# Backoff: a transient release waits under a key _prune keeps, and blocks the post even
# after the first heartbeat (Pitfall 1)


@DB
def test_D08_transient_release_backs_off_and_blocks_the_post(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    _reset_by_hand(location, NOON_05)
    # The unpin answers 502 once, then works.
    fake_telegram.fail_method(
        DEFAULT_BOT_TOKEN, "unpinChatMessage", status=502, json_body=BAD_GATEWAY
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    key = lifecycle.chart_key(location.pk, "release", record.pk)
    bot = io_loop.bot_wide_key(DEFAULT_BOT_TOKEN)
    retry = NOON_05 + timedelta(seconds=32)

    # 12:05:00: the release answers 502. The bot is held as an alert's 5xx would hold it
    # (2 s), and the release waits step_delay(1) (30 s) longer.
    assert _pass(clock, state) is True
    assert state.not_before == {bot: NOON_05 + timedelta(seconds=2), key: retry}
    record.refresh_from_db()
    assert record.retired_at is None
    # 12:05:01: the location's first heartbeat after the reset: it is on again.
    clock.advance(seconds=1)
    assert transitions.record_heartbeat(location.pk, clock.now()) == "started"
    # Inside the backoff no pass makes a request, and nothing is posted though the
    # location is monitored again; the release's key survives every prune.
    for seconds in (1, 2, 10, 31):
        clock.set(NOON_05 + timedelta(seconds=seconds))
        assert _pass(clock, state) is False
        assert state.not_before.get(key) == retry
    assert _requests(fake_telegram) == [("A", "unpinChatMessage")]
    # 12:05:32: the release works; only a later pass posts today's new chart.
    clock.set(retry)
    assert _pass(clock, state) is True
    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (retry, False)
    assert key not in state.not_before and key not in state.chart_failures
    assert _all_passes(clock, state, fake_telegram) == [
        [("sendPhoto", DEFAULT_CHAT_ID, None)],
        [("pinChatMessage", DEFAULT_CHAT_ID, 1001)],
    ]

    assert _requests(fake_telegram) == [
        ("A", "unpinChatMessage"),
        ("A", "unpinChatMessage"),
        ("A", "sendPhoto"),
        ("A", "pinChatMessage"),
    ]
    new = ChartMessage.objects.get(retired_at__isnull=True)
    assert (new.message_id, new.pinned, new.history_reset_at) == (1001, True, None)


# The first heartbeat after the release brings one new pinned chart (Phase 3 D-03, INV-19)


@DB
def test_D08_first_heartbeat_after_release_posts_and_pins_a_new_chart(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    # Today's chart is posted and pinned; the reset comes 30 s later and is released.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    clock.advance(seconds=30)
    _reset_by_hand(location, clock.now())
    assert _pass(clock, state) is True
    [old] = _rows()
    assert (old.message_id, old.retired_at, old.pinned) == (1001, clock.now(), False)
    # The location waits 20 min: nothing is sent.
    clock.advance(minutes=20)
    assert _pass(clock, state) is False
    start = len(fake_telegram.calls)

    # The first heartbeat restarts monitoring silently (MON-01, K-1).
    assert transitions.record_heartbeat(location.pk, clock.now()) == "started"

    assert _all_passes(clock, state, fake_telegram) == [
        [("sendPhoto", DEFAULT_CHAT_ID, None)],
        [("pinChatMessage", DEFAULT_CHAT_ID, 1002)],
    ]
    assert _requests(fake_telegram, start) == [("A", "sendPhoto"), ("A", "pinChatMessage")]
    new = ChartMessage.objects.get(retired_at__isnull=True)
    assert (new.message_id, new.chat_id, new.local_date, new.pinned, new.history_reset_at) == (
        1002,
        DEFAULT_CHAT_ID,
        TODAY,
        True,
        None,
    )
    # The old record is never called again: the refresh 15 min later edits the new chart.
    clock.advance(minutes=15)
    assert _pass(clock, state) is True
    assert _calls(fake_telegram)[-1] == ("editMessageMedia", DEFAULT_CHAT_ID, 1002)
    assert [call for call in _calls(fake_telegram) if call[2] == 1001] == [
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 1001),
    ]
    # The restart sent nothing to subscribers or to the ops chat.
    assert not OutboxMessage.objects.exists()


@DB
def test_D08_location_on_with_a_marked_record_releases_before_posting(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    location = _monitored(location_factory)
    record = _seed(location, message_id=501)
    _reset_by_hand(location, NOON_05)
    # The first heartbeat comes before the worker's next pass: the location is on again
    # with its marked record still active.
    assert transitions.record_heartbeat(location.pk, NOON_05 + timedelta(seconds=5)) == "started"
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05 + timedelta(seconds=10))
    state = io_loop.RelayState()
    locations, rows = lifecycle.read_snapshot(TODAY)
    assert [(loc.location_id, loc.awaiting_heartbeat) for loc in locations] == [
        (location.pk, False)
    ]
    assert [(row.id, row.history_reset_at) for row in rows] == [(record.pk, NOON_05)]

    passes = _all_passes(clock, state, fake_telegram)

    # The release first; the post only once the old today-record is retired, so the
    # partial unique index never refuses it (Phase 4 Pitfall 1).
    assert passes == [
        [("unpinChatMessage", DEFAULT_CHAT_ID, 501)],
        [("sendPhoto", DEFAULT_CHAT_ID, None)],
        [("pinChatMessage", DEFAULT_CHAT_ID, 1001)],
    ]
    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (clock.now(), False)
    assert ChartMessage.objects.get(retired_at__isnull=True).message_id == 1001


# Only the reset location's own messages are unpinned, never with unpinAllChatMessages


@DB
def test_D08_release_never_calls_unpin_all(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Two locations share the channel; only one is reset. The channel's other pins (the
    # other location's chart, the admin's own posts) must stay pinned (D-08, INV-19).
    reset = _monitored(location_factory)
    yesterday = _seed(reset, YESTERDAY, message_id=501, rendered=kyiv("2026-10-01 23:45"))
    today = _seed(reset, message_id=502)
    other = _monitored(location_factory)
    neighbour = _seed(other, message_id=601)
    _reset_by_hand(reset, NOON_05)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    passes = _all_passes(clock, state, fake_telegram)

    assert passes == [
        [("unpinChatMessage", DEFAULT_CHAT_ID, 501)],
        [("unpinChatMessage", DEFAULT_CHAT_ID, 502)],
    ]
    unpinned = [
        json.loads(call.request.body)["message_id"]
        for call in fake_telegram.calls
        if call.request.url.endswith("/unpinChatMessage")
    ]
    assert unpinned == [yesterday.message_id, today.message_id]
    assert _no_unpin_all(fake_telegram)
    neighbour.refresh_from_db()
    assert (neighbour.pinned, neighbour.retired_at, neighbour.history_reset_at) == (
        True,
        None,
        None,
    )


# INV-19 reset scenario end to end (05-05): the reset POST, the release, nothing while the
# location waits, then one new pinned chart that shows no data before the restart


@DB
def test_INV19_reset_scenario_end_to_end(
    location_factory: Callable[..., Any], fake_telegram: Any, rf: RequestFactory
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    clock.advance(seconds=30)

    response, flashes = _reset_post(rf, location, clock)

    assert (response.status_code, flashes) == (302, [HISTORY_RESET_MESSAGE])
    clock.advance(seconds=5)
    assert _pass(clock, state) is True
    assert _calls(fake_telegram)[-1] == ("unpinChatMessage", DEFAULT_CHAT_ID, 1001)
    released = len(fake_telegram.calls)
    # While the location waits, a pass every 5 minutes for 30 minutes makes no request:
    # no photo, no pin, no edit of the old message.
    for _ in range(6):
        clock.advance(minutes=5)
        assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == released

    # The first heartbeat after the reset restarts monitoring silently (MON-01, K-1).
    restart = clock.now()
    assert transitions.record_heartbeat(location.pk, restart) == "started"
    assert not OutboxMessage.objects.exists()

    # The next passes post and pin one new chart in the location's chat.
    assert _all_passes(clock, state, fake_telegram) == [
        [("sendPhoto", DEFAULT_CHAT_ID, None)],
        [("pinChatMessage", DEFAULT_CHAT_ID, 1002)],
    ]
    assert _requests(fake_telegram, released) == [("A", "sendPhoto"), ("A", "pinChatMessage")]
    clock.advance(minutes=15)
    assert _pass(clock, state) is True
    assert _calls(fake_telegram)[-1] == ("editMessageMedia", DEFAULT_CHAT_ID, 1002)
    # No request ever targets the old message after its unpin.
    assert [call for call in _calls(fake_telegram) if call[2] == 1001] == [
        ("pinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("unpinChatMessage", DEFAULT_CHAT_ID, 1001),
    ]
    assert [(row.message_id, row.retired_at is None, row.pinned) for row in _rows()] == [
        (1001, False, False),
        (1002, True, True),
    ]
    # chart-spec §9: no data before the restart. Every stored piece starts at or after it,
    # and the new chart's today row has no segment before its wall-clock time.
    starts = list(PowerInterval.objects.filter(location_id=location.pk).values_list("start_at"))
    assert starts == [(restart,)]
    week = source.load_week(location.pk, today=TODAY, now=clock.now(), tz=KYIV, live=True)
    restart_us = model.wall_us(restart, KYIV, end=False)
    segments = week.today_row.segments
    assert segments and segments[0].start_us == restart_us
    assert all(segment.start_us >= restart_us for segment in segments)
    assert [row.segments for row in week.rows if not row.is_today] == [()] * 6


def _pin_incidents(location: Any) -> list[tuple[datetime, datetime | None]]:
    rows = OpsIncident.objects.filter(kind=lifecycle.KIND_CHART_PIN_FAILED, location=location)
    return [(row.started_at, row.ended_at) for row in rows.order_by("id")]


def _ops_rows(kind: str) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS, kind=kind)
    return list(rows.order_by("id"))


@DB
def test_reset_keeps_an_open_pin_failed_incident_until_the_new_chart_pins(
    location_factory: Callable[..., Any], fake_telegram: Any, ops_settings: Any
) -> None:
    # Planner's discretion (05-05, recorded): the reset leaves an open chart_pin_failed
    # incident open; it closes with its usual notice when the new chart pins.
    location = _monitored(location_factory)
    record = _seed(location, message_id=501, pinned=False)
    ChartMessage.objects.filter(pk=record.pk).update(pin_failed_at=NOON_05)
    OpsIncident.objects.create(
        kind=lifecycle.KIND_CHART_PIN_FAILED, location=location, started_at=NOON_05
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    clock = FakeClock(NOON_05 + timedelta(seconds=30))
    state = io_loop.RelayState()

    assert history.reset_history(location.pk, clock.now()) == "reset"

    assert _pin_incidents(location) == [(NOON_05, None)]
    # The release retires the old record; the incident is still open.
    assert _pass(clock, state) is True
    record.refresh_from_db()
    assert (record.retired_at, record.pinned) == (clock.now(), False)
    assert _pin_incidents(location) == [(NOON_05, None)]
    assert _ops_rows(outbox.KIND_OPS_PIN_RESTORED) == []

    # The first heartbeat: the new chart is posted, then pinned, which closes the incident.
    clock.advance(minutes=1)
    assert transitions.record_heartbeat(location.pk, clock.now()) == "started"
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    pinned_at = clock.now()
    assert _calls(fake_telegram)[-1] == ("pinChatMessage", DEFAULT_CHAT_ID, 1001)
    assert _pin_incidents(location) == [(NOON_05, pinned_at)]
    [restored] = _ops_rows(outbox.KIND_OPS_PIN_RESTORED)
    assert (restored.location_id, restored.payload) == (location.pk, {})
    assert _ops_rows(outbox.KIND_OPS_PIN_FAILED) == []
    # Its notice goes out in the next pass, the only ops message of the whole hand-over.
    assert _pass(clock, state) is True
    restored.refresh_from_db()
    assert restored.status == "sent"
    assert [req for req in _requests(fake_telegram) if req[0] == "ops"] == [("ops", "sendMessage")]
    assert not OutboxMessage.objects.filter(channel=outbox.CHANNEL_SUBSCRIBER).exists()
