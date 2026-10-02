"""The chart lifecycle at local midnight and after missed midnights (INV-18, INV-19).

At local midnight (Europe/Kyiv) a location gets today's chart posted and pinned, and the
previous day's chart gets its finished render (now = the midnight that ends its day, no
now line, no pill, the caption's line 1 only) and is unpinned (D-01, D-13). Each step is
its own condition on every I/O pass, in D-02 order (post, pin, finalize, unpin), so after
downtime across one or more midnights exactly one chart for today is posted and every
older chart is finalized and unpinned, a day the worker missed gets no chart (D-03), and
a failing step never blocks the others (D-02). Edits and unpins go to the chat and the
message stored with each record, never the location's current chat, and nothing but a
recorded message is ever unpinned (D-04, INV-19).

Every test runs ``io_loop.run_iteration(..., charts=True)``, which calls
``close_old_connections()``, so each is ``django_db(transaction=True)``. Time comes only
from the ``FakeClock``; Telegram is faked at the HTTP boundary (``fake_telegram``); renders
are real (Pillow). Older records are seeded with ``ChartMessage.objects.create``. The
worker's first-cycle gate arrives in 03-10, so a test that needs the downtime drawn as not
monitored carves it itself with ``lapse.carve_window``.
"""

import dataclasses
import io
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, insert_pieces, kyiv, local_pieces, monitor, set_status
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from PIL import Image

from powermon.chart import lifecycle, model
from powermon.chart.models import ChartMessage
from powermon.engine import lapse
from powermon.locations.models import Location
from powermon.worker import io_loop

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
    """A record as an earlier run left it: posted (and maybe pinned) on ``day``."""
    at = rendered if rendered is not None else model.next_midnight(day, KYIV) - _min(15)
    return ChartMessage.objects.create(
        location=location,
        local_date=day,
        chat_id=chat_id,
        message_id=message_id,
        pinned=pinned,
        last_rendered_at=at,
        finalized_at=at if finalized else None,
        created_at=at,
    )


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


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
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
    # Down from 23:58 to 00:07: the restart's lapse carve draws it as not monitored.
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
    assert _caption(photo) == "No outages today\nUpdated 00:07"
    # The finished day: line 1 only, no "Updated" line (D-13).
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
    snapshot = lifecycle.ChartLocation(
        location.pk, location.name, location.language, location.bot_token, location.chat_id
    )
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
