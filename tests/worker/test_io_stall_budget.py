"""The I/O stall watchdog never fires during a Telegram outage (INV-13, INV-14, 261030-11v).

Photo uploads wait up to 30 s for Telegram's answer (D-2), and a degraded bot's post is
preceded by a ``getMe`` probe (D-1). A pass over many locations whose every call takes its
full timeout can then last longer than ``IO_STALL_S`` (180 s). That is progress, not a
stall: ``io_loop.run_iteration`` stamps the ``telegram-io`` progress after every
subscriber head, the ops step, the delete step and the chart step, so the longest gap
between two stamps is one step. The worst step is a chart post behind its probe:
(5 + 5) + (5 + 30) s = 45 s, or 55 s with a second address family's connect attempt each,
well inside 180 s.

The failure counterpart, a single call that blocks longer than the limit and fires the
stall action, is ``test_INV13_hung_loop_triggers_watchdog_stall_action`` in
tests/worker/test_supervision.py.

The fake's callbacks advance the ``FakeClock`` by the full (connect + read) timeout of the
request they answer, then record what the watchdog sees. They never assert: the client
turns any exception into ``maybe_delivered``.
"""

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests
import responses
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from requests import PreparedRequest

from powermon.alerts import outbox
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.telegram import client
from powermon.worker import io_loop, supervision

pytestmark = pytest.mark.django_db(transaction=True)

KYIV = "Europe/Kyiv"


def _kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-02 12:05"`` (fold 0) as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(KYIV)).astimezone(UTC)


NOON_05 = _kyiv("2026-10-02 12:05")
SINCE = _kyiv("2026-10-01 08:00")
# 13 alerts of 15 s each (5 s connect + 10 s read) already outlast IO_STALL_S.
LOCATIONS = 13
GET_ME_OK = {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Fake bot"}}


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _monitor(location: Any, since: datetime) -> None:
    """A location on since ``since``: live state "on" and one open on piece from ``since``."""
    LocationState.objects.filter(location=location).update(
        status="on",
        on_since=since,
        last_heartbeat_at=since,
        outage_started_at=None,
        state_version=F("state_version") + 1,
    )
    PowerInterval.objects.create(
        location=location, state="on", start_at=since, end_at=None, outage_start_at=None
    )


def _queue_alert(location: Any) -> None:
    """An OFF alert recorded (and so due) at NOON_05, as a transition would queue it."""
    with transaction.atomic():
        outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=NOON_05 - timedelta(seconds=91),
            recorded_at=NOON_05,
            payload={"was_on_us": 300_000_000},
        )


def test_INV13_INV14_watchdog_never_fires_during_a_telegram_outage(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    for n in range(LOCATIONS):
        location = location_factory(
            name=f"Location {n}", bot_token=DEFAULT_BOT_TOKEN, chat_id=DEFAULT_CHAT_ID
        )
        if n == 0:
            # The first one is monitored and has no chart today: its post is due.
            _monitor(location, SINCE)
        _queue_alert(location)
    clock = FakeClock(NOON_05)
    progress = supervision.Progress(clock)
    fired: list[str] = []
    watchdog = supervision.Watchdog(
        clock, progress, {"telegram-io": supervision.IO_STALL_S}, {}, fired.append
    )
    stamps: list[float] = []
    seen: list[tuple[str, Any, str | None]] = []

    def tick() -> None:
        progress.stamp("telegram-io")
        stamps.append(clock.monotonic())

    def waits(method: str, answer: Callable[[], tuple[int, dict[str, str], str]]) -> Any:
        def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
            timeout = request.req_kwargs["timeout"]  # type: ignore[attr-defined]
            # The call takes its full connect and read timeouts.
            clock.advance(seconds=sum(timeout))
            seen.append((method, timeout, watchdog.check()))
            return answer()

        return callback

    def read_timeout() -> tuple[int, dict[str, str], str]:
        raise requests.ReadTimeout()

    def probe_ok() -> tuple[int, dict[str, str], str]:
        return 200, {}, json.dumps(GET_ME_OK)

    for method, answer in (
        ("sendMessage", read_timeout),
        ("getMe", probe_ok),
        ("sendPhoto", read_timeout),
    ):
        fake_telegram.rsps.add_callback(
            responses.POST,
            f"{fake_telegram.API}/bot{DEFAULT_BOT_TOKEN}/{method}",
            callback=waits(method, answer),
            content_type="application/json",
        )
    state = io_loop.RelayState()
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(NOON_05))
    )

    # run_worker stamps at the top of every I/O iteration.
    tick()
    assert io_loop.run_iteration(clock, state, None, tick=tick, charts=True) is True

    assert fired == []
    assert [check for _, _, check in seen] == [None] * len(seen)
    assert watchdog.check() is None
    # The whole pass outlasted the limit: only the per-step stamps keep the watchdog quiet.
    assert clock.monotonic() > supervision.IO_STALL_S
    assert max(b - a for a, b in pairwise(stamps)) <= 45.0
    timeouts = [(method, timeout) for method, timeout, _ in seen]
    assert timeouts == (
        [("sendMessage", (5.0, 10.0))] * LOCATIONS
        + [("getMe", (5.0, 5.0)), ("sendPhoto", (5.0, 30.0))]
    )
    # The static bound: a probe and a photo, each with a second connect attempt.
    assert client.PROBE_TIMEOUT == (5.0, 5.0)
    assert client.PHOTO_TIMEOUT == (5.0, 30.0)
    assert 2 * 5.0 + 5.0 + 2 * 5.0 + 30.0 < supervision.IO_STALL_S
