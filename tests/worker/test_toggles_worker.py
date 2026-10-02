"""INV-05 #1 and K-4 driven through the location page's switches (LOC-09, LOC-10; D-05, D-06).

Each switch is clicked as the admin clicks it: a signed-in POST to its URL, answered with
a redirect back to the location page. The effect is then followed through the parts of
the worker that act on it: detection (``detection.run_cycle``), the heartbeat gate
(``transitions.record_heartbeat``) and the I/O pass (``io_loop.run_iteration``). Each
switch has exactly one effect (INV-05):
- alerts off: transitions and the timeline carry on, but a transition recorded while
  alerts are off queues no alert, and nothing is held for later; alerts already queued
  still go out (D-06);
- router grace: only the timeout of decisions made after the change (K-4).

``run_iteration`` calls ``close_old_connections()``, so every test here is
``django_db(transaction=True)``: it must see the rows the other steps committed, and no
test-wide transaction may hold them. Time comes only from the injected ``FakeClock`` and
the instants passed to the engine; Telegram is faked at the HTTP boundary
(``fake_telegram``), and chart renders are real (Pillow). The few chart helpers (the Kyiv
wall-time helper, the caption reader and the detection cursor of
tests/chart/test_lifecycle_midnight.py, after chart_fixtures' ``kyiv``) are copied instead
of imported: tests have no ``__init__.py``, so tests/chart is on ``sys.path`` only once one
of its modules was collected, and this file must also run on its own.
"""

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, ChartCall, FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.db.models import Value
from django.db.models.functions import Greatest
from django.test import Client

from powermon.alerts.models import OutboxMessage
from powermon.chart import source
from powermon.chart.models import ChartMessage
from powermon.engine import rules, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations.models import Location
from powermon.worker import detection, io_loop

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
KYIV = "Europe/Kyiv"


@pytest.fixture
def admin(client: Client, transactional_db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _click(admin: Client, location: Any, switch: str, value: str) -> None:
    """Click a switch on the location page: its POST answers with a redirect back there."""
    response = admin.post(f"/locations/{location.pk}/{switch}/", {"value": value})
    assert response.status_code == 302
    assert response.url == f"/locations/{location.pk}/"


def _anchors() -> None:
    """Detection resumed at 09:00 and the web start is unknown: no window after 09:00."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": None}
    )


def _beating(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """K-2: heartbeats every 60 s from 10:00:00 to 10:05:00, then silence (OFF after 10:06:30)."""
    _anchors()
    location = location_factory(**overrides)
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    return location


def _intervals(location: Any) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    return list(
        PowerInterval.objects.filter(location=location)
        .order_by("start_at")
        .values_list("state", "start_at", "end_at", "outage_start_at")
    )


# INV-05 #1, alert part: alerts off from the location page, then an outage


def test_INV05_1_alerts_off_outage_sends_nothing(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _beating(location_factory, name="Office")
    _click(admin, location, "alerts", "off")
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    relay = io_loop.RelayState()

    # The device went silent after 10:05:00: K-2, OFF after 10:06:30, not at it.
    assert detection.run_cycle(_at(10, 6, 30)) == 0
    assert detection.run_cycle(_at(10, 6, 31)) == 1

    # The OFF transition and the timeline are recorded as usual...
    state = LocationState.objects.get(location=location)
    assert (state.status, state.outage_started_at) == ("off", _at(10, 5))
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), None, _at(10, 5)),
    ]
    # ...but no alert is queued, so the relay has nothing to send.
    assert not OutboxMessage.objects.exists()
    assert io_loop.run_iteration(FakeClock(_at(10, 6, 35)), relay) is False

    # Power returns at 11:00: the ON transition is recorded, and again nothing is queued.
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    state = LocationState.objects.get(location=location)
    assert (state.status, state.on_since) == ("on", _at(11, 0))
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), _at(11, 0), _at(10, 5)),
        ("on", _at(11, 0), None, None),
    ]
    assert not OutboxMessage.objects.exists()
    assert io_loop.run_iteration(FakeClock(_at(11, 0, 5)), relay) is False
    # Subscribers got 0 messages for the whole outage.
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendMessage") == 0
    assert len(fake_telegram.calls) == 0


def test_alerts_already_queued_still_go_out(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    # D-06: whether an alert is sent is decided when its transition is recorded. The OFF
    # was recorded while alerts were on, so its alert is queued and goes out after the
    # switch; only the restore, recorded with alerts off, queues nothing.
    location = _beating(location_factory)
    assert detection.run_cycle(_at(10, 6, 31)) == 1
    [off] = OutboxMessage.objects.all()
    assert (off.kind, off.status) == ("power_off", "pending")

    _click(admin, location, "alerts", "off")
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    assert io_loop.run_iteration(FakeClock(_at(10, 6, 35)), io_loop.RelayState()) is True

    assert fake_telegram.sent == [
        {"chat_id": DEFAULT_CHAT_ID, "text": OFF_EN, "parse_mode": "HTML"}
    ]
    assert OutboxMessage.objects.get(pk=off.pk).status == "sent"
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"
    assert list(OutboxMessage.objects.values_list("kind", "status")) == [("power_off", "sent")]


# K-4 through the router-grace switch (LOC-09): only decisions made after the change


def _status(location: Any) -> tuple[str, datetime | None]:
    state = LocationState.objects.get(location=location)
    return state.status, state.outage_started_at


def _beat(location: Any, *instants: datetime) -> None:
    for at in instants:
        assert transitions.record_heartbeat(location.pk, at) in ("started", "plain", "restored")


def _today_totals(location: Any, now: datetime) -> tuple[int, int, int]:
    """Today's chart row totals (on, off, outages) as the chart reads them at ``now``."""
    week = source.load_week(location.pk, today=date(2026, 10, 1), now=now, tz="UTC", live=True)
    [today] = [row for row in week.rows if row.is_today]
    return today.on_us, today.off_us, today.count


def test_K4_router_grace_turned_on_from_the_page_keeps_off_back(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    # One location per K-4 case: period 60 s, grace 30 s, router grace turned on from the
    # location page before the first heartbeat at 12:00:00 (on since 12:00:00).
    _anchors()
    within = location_factory(name="Last heartbeat 12:04:00")
    outside = location_factory(name="Last heartbeat 12:05:30")
    boundary = location_factory(name="Last heartbeat 12:05:00")
    for location in (within, outside, boundary):
        _click(admin, location, "router-grace", "on")
        assert Location.objects.get(pk=location.pk).router_grace is True
        _beat(location, *(_at(12, minute) for minute in range(5)))
    _beat(outside, _at(12, 5), _at(12, 5, 30))
    _beat(boundary, _at(12, 5))
    assert LocationState.objects.get(location=within).on_since == _at(12, 0)

    # 12:05:30 is 330 s after on, outside the window: the plain 90 s applies.
    assert detection.run_cycle(_at(12, 7)) == 0
    assert detection.run_cycle(_at(12, 7, 1)) == 1
    assert _status(outside) == ("off", _at(12, 5, 30))
    # 12:04:00 is 240 s after on: 90 s + 180 s, so no OFF before 12:08:30.
    assert detection.run_cycle(_at(12, 8, 30)) == 0
    assert _status(within) == ("on", None)
    assert detection.run_cycle(_at(12, 8, 31)) == 1
    assert _status(within) == ("off", _at(12, 4))
    # Exactly 12:05:00 is 300 s after on: the window is inclusive, so the grace applies.
    assert detection.run_cycle(_at(12, 9, 30)) == 0
    assert _status(boundary) == ("on", None)
    assert detection.run_cycle(_at(12, 9, 31)) == 1
    assert _status(boundary) == ("off", _at(12, 5))
    for location, start in (
        (within, _at(12, 4)),
        (outside, _at(12, 5, 30)),
        (boundary, _at(12, 5)),
    ):
        assert _intervals(location) == [
            ("on", _at(12, 0), start, None),
            ("off", start, None, start),
        ]
    # The switch and the decisions make no Telegram call (KD2).
    assert len(fake_telegram.calls) == 0


def test_router_grace_changes_only_future_decisions(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # D-05, INV-05, INV-06: an OFF already recorded keeps its start, its interval and its
    # row totals whichever way router grace is switched afterwards.
    _anchors()
    location = location_factory(name="Office")
    _beat(location, *(_at(12, minute) for minute in range(5)))
    # Router grace off: the plain 90 s, so the OFF is recorded at 12:05:31 from 12:04:00.
    assert detection.run_cycle(_at(12, 5, 31)) == 1
    recorded = _intervals(location)
    assert recorded == [("on", _at(12, 0), _at(12, 4), None), ("off", _at(12, 4), None, _at(12, 4))]
    totals = _today_totals(location, _at(12, 6))

    _click(admin, location, "router-grace", "on")

    assert _status(location) == ("off", _at(12, 4))
    assert _intervals(location) == recorded
    assert _today_totals(location, _at(12, 6)) == totals
    # The next on period's decisions use the grace: restored at 12:10:00, last heartbeat
    # 12:11:00 (60 s after on), so the OFF waits 270 s instead of 90 s.
    _beat(location, _at(12, 10), _at(12, 11))
    assert detection.run_cycle(_at(12, 12, 31)) == 0
    assert detection.run_cycle(_at(12, 15, 30)) == 0
    assert detection.run_cycle(_at(12, 15, 31)) == 1
    assert _status(location) == ("off", _at(12, 11))
    second = _intervals(location)
    totals = _today_totals(location, _at(12, 16))

    _click(admin, location, "router-grace", "off")

    assert _status(location) == ("off", _at(12, 11))
    assert _intervals(location) == second
    assert _today_totals(location, _at(12, 16)) == totals


def test_router_grace_toggle_between_snapshot_and_decision_affects_the_next_cycle_only(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # Router grace off, on since 12:00:00, last heartbeat 12:04:00.
    _anchors()
    location = location_factory(name="Office")
    _beat(location, *(_at(12, minute) for minute in range(5)))
    # The detector reads its snapshot at 12:06:00 and decides as run_cycle does: the plain
    # 90 s ended at 12:05:30, so the OFF is due.
    now = _at(12, 6)
    system = SystemState.objects.get(pk=1)
    anchors = rules.Anchors(
        detection_resumed_at=system.detection_resumed_at, web_started_at=system.web_started_at
    )
    [snap] = transitions.read_snapshots()
    decision = rules.decide(snap, anchors, now)
    assert (snap.router_grace, decision.off, decision.outage_start) == (False, True, _at(12, 4))
    version = LocationState.objects.get(location=location).state_version

    # The admin turns router grace on before the decision is written.
    _click(admin, location, "router-grace", "on")

    # A configuration-only write leaves the CAS token alone, so the decision made with the
    # snapshot's router_grace is written as it was made: no outage is rewritten (D-05).
    assert LocationState.objects.get(location=location).state_version == version
    assert transitions.mark_off(snap, decision, now) is True
    assert _status(location) == ("off", _at(12, 4))
    assert _intervals(location) == [
        ("on", _at(12, 0), _at(12, 4), None),
        ("off", _at(12, 4), None, _at(12, 4)),
    ]
    # The next cycle reads router grace afresh. Restored at 12:10:00 with a last heartbeat
    # at 12:11:00, the plain 90 s would end at 12:12:30; the grace applies, so the cycle at
    # 12:13:00 records no OFF.
    _beat(location, _at(12, 10), _at(12, 11))
    [next_snap] = transitions.read_snapshots()
    assert next_snap.router_grace is True
    assert detection.run_cycle(_at(12, 13)) == 0
    assert LocationState.objects.get(location=location).status == "on"
    assert _intervals(location)[-1] == ("on", _at(12, 10), None, None)


# INV-05 #1, chart part: with alerts off the chart is posted, refreshed and re-pinned


@pytest.fixture
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-01 12:00"`` (fold 0) as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(KYIV)).astimezone(UTC)


def _caption(call: ChartCall) -> str:
    """The caption of an accepted sendPhoto or editMessageMedia call."""
    if call.method == "editMessageMedia":
        return str(json.loads(call.fields["media"])["caption"])
    return str(call.fields["caption"])


def _chart_steps(calls: list[ChartCall]) -> list[tuple[str, int | None]]:
    """(method, message id) of each accepted chart call."""
    out = []
    for call in calls:
        message_id = call.fields.get("message_id")
        out.append((call.method, None if message_id is None else int(message_id)))
    return out


def _detected(at: datetime) -> None:
    """Detection has completed its cycles up to ``at``: its cursor moves there, never back."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(at))
    )


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass with charts, with detection caught up to the clock's now."""
    _detected(clock.now())
    return io_loop.run_iteration(clock, state, charts=True)


def _run_until_idle(clock: FakeClock, state: io_loop.RelayState, limit: int = 10) -> int:
    """Run passes until one makes no call; return how many made a call."""
    for made in range(limit):
        if not _pass(clock, state):
            return made
    raise AssertionError(f"still making calls after {limit} passes")


def test_INV05_1_chart_keeps_working_with_alerts_off(
    admin: Client,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    kyiv_tz: Any,
) -> None:
    # Thu 2026-10-01: heartbeats every minute from 12:00 to 12:05 local, then alerts off.
    _anchors()
    location = location_factory(name="Office")
    _beat(location, *(_kyiv(f"2026-10-01 12:0{minute}") for minute in range(6)))
    _click(admin, location, "alerts", "off")
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    # Registered so that a subscriber message, if one were sent, would be accepted and seen.
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    clock = FakeClock(_kyiv("2026-10-01 12:05"))
    state = io_loop.RelayState()

    # Alerts off never stops the chart: today's is posted silently and pinned.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _chart_steps(fake_telegram.chart_calls) == [
        ("sendPhoto", None),
        ("pinChatMessage", 1001),
    ]
    assert _caption(fake_telegram.chart_calls[0]) == "No outages today\nUpdated 12:05"

    # An outage: silent after 12:05, OFF recorded at 12:06:31, power back at 12:15.
    assert detection.run_cycle(_kyiv("2026-10-01 12:06:31")) == 1
    _beat(location, _kyiv("2026-10-01 12:15"))
    # The 15-min refresh counts from the last render (12:05): nothing before 12:20.
    clock.set(_kyiv("2026-10-01 12:19:59"))
    assert _pass(clock, state) is False
    clock.set(_kyiv("2026-10-01 12:20"))
    assert _pass(clock, state) is True

    refresh = fake_telegram.chart_calls[-1]
    assert _chart_steps([refresh]) == [("editMessageMedia", 1001)]
    assert _caption(refresh) == "Today off: 10m · 1 outage\nUpdated 12:20"

    # After local midnight, once detection has settled past it, the passes post and pin
    # the new day's chart and finalize and unpin yesterday's (the midnight re-pin).
    clock.set(_kyiv("2026-10-02 00:07"))
    before = len(fake_telegram.chart_calls)
    assert _run_until_idle(clock, state) == 4

    midnight = fake_telegram.chart_calls[before:]
    assert _chart_steps(midnight) == [
        ("sendPhoto", None),
        ("pinChatMessage", 1002),
        ("editMessageMedia", 1001),
        ("unpinChatMessage", 1001),
    ]
    assert _caption(midnight[2]) == "Thu 01.10 off: 10m · 1 outage"
    yesterday = ChartMessage.objects.get(location=location, local_date=date(2026, 10, 1))
    today = ChartMessage.objects.get(location=location, local_date=date(2026, 10, 2))
    assert yesterday.pinned is False
    assert yesterday.finalized_at is not None and yesterday.unpinned_at is not None
    assert (today.message_id, today.pinned) == (1002, True)
    # Subscribers got 0 messages throughout: nothing was queued, nothing was sent.
    assert not OutboxMessage.objects.exists()
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendMessage") == 0
    assert fake_telegram.sent == []
