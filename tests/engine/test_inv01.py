"""INV-01: a transition is one conditional write that does no network I/O (MON-03, KD2).

The three INV-01 acceptance shapes of docs/v1-lessons.md, in their deterministic form
(the parallel race suite is Phase 2, MON-04):
1. a decision that lost to a heartbeat between its snapshot and its write writes nothing;
2. a restore followed by silence gives two separate outages and two OFF alerts;
3. the heartbeat answers at once while Telegram would hang, because it calls nothing.

All times are fixed aware datetimes on 2026-10-01 UTC. The heartbeat view is served
through RequestFactory with an injected FakeClock (constructor injection, the 01-03
pattern), never through the test client, whose view instance reads the system clock.
"""

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import responses
from conftest import FakeClock
from django.http import HttpResponse
from django.test import RequestFactory
from requests import PreparedRequest

from powermon.alerts import texts
from powermon.alerts.models import OutboxMessage
from powermon.engine import rules, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.web.views import HeartbeatView
from powermon.worker import detection

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _resume_detection(at: datetime) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": at, "web_started_at": None}
    )


def _state(location: Any) -> LocationState:
    return LocationState.objects.get(pk=location.pk)


def _intervals(location: Any) -> list[Interval]:
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _text(row: OutboxMessage, lang: str) -> str:
    (duration_us,) = row.payload.values()
    return texts.render_alert(row.kind, lang, duration_us)


# 1. A stale decision writes nothing (the state_version CAS)


@pytest.mark.django_db
def test_INV01_cas_loses_to_a_heartbeat_between_snapshot_and_write(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    [(snap, alerts_enabled)] = transitions.read_snapshots()
    decision = rules.decide(snap, rules.Anchors(detection_resumed_at=_at(9, 0)), _at(10, 6, 31))
    assert decision.off

    # The device reports between the detector's read and its write.
    assert transitions.record_heartbeat(location.pk, _at(10, 6, 32)) == "plain"
    assert transitions.mark_off(snap, decision, _at(10, 6, 31), alerts_enabled) is False

    state = _state(location)
    assert (state.status, state.outage_started_at) == ("on", None)
    assert state.last_heartbeat_at == _at(10, 6, 32)
    assert state.state_version == snap.state_version + 1
    assert not OutboxMessage.objects.exists()
    assert _intervals(location) == [("on", _at(10, 0), None, None)]


# 2. Restore, then silence: two outages, never one merged one


@pytest.mark.django_db(transaction=True)
def test_INV01_restore_then_silence_gives_two_outages(
    location_factory: Callable[..., Any],
) -> None:
    _resume_detection(_at(8, 0))
    location = location_factory()
    # On since 09:00:00, last heartbeat 10:01:00.
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(10, 1)) == "plain"
    assert detection.run_cycle(_at(10, 2, 30)) == 0
    assert detection.run_cycle(_at(10, 2, 31)) == 1

    assert transitions.record_heartbeat(location.pk, _at(13, 0)) == "restored"
    # Silence right after the restore: OFF at the first cycle after 13:01:30.
    assert detection.run_cycle(_at(13, 1, 30)) == 0
    assert detection.run_cycle(_at(13, 1, 31)) == 1

    state = _state(location)
    assert (state.status, state.outage_started_at) == ("off", _at(13, 0))
    first_off, on, second_off = OutboxMessage.objects.order_by("id")
    assert [m.kind for m in (first_off, on, second_off)] == ["power_off", "power_on", "power_off"]
    assert (first_off.event_at, on.event_at, second_off.event_at) == (
        _at(10, 1),
        _at(13, 0),
        _at(13, 0),
    )
    assert _text(first_off, "en") == "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>1h 1m</b>"
    assert on.payload == {"was_off_us": 10_740_000_000}
    assert _text(on, "en") == "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>2h 59m</b>"
    # On for zero time between the restore and the last heartbeat (both 13:00:00).
    assert second_off.payload == {"was_on_us": 0}
    assert _text(second_off, "en") == "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>0s</b>"
    # The zero-length on interval [13:00, 13:00) was deleted, not stored, and the two
    # off intervals carry different outage starts: two outages.
    assert _intervals(location) == [
        ("on", _at(9, 0), _at(10, 1), None),
        ("off", _at(10, 1), _at(13, 0), _at(10, 1)),
        ("off", _at(13, 0), None, _at(13, 0)),
    ]


# 3. The heartbeat does no network I/O, so a hanging Telegram cannot slow it


@pytest.mark.django_db(transaction=True)
def test_INV01_heartbeat_returns_fast_without_network(
    location_factory: Callable[..., Any], fake_telegram: Any, rf: RequestFactory
) -> None:
    _resume_detection(_at(9, 0))
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "started"
    assert detection.run_cycle(_at(10, 1, 31)) == 1
    assert _state(location).outage_started_at == _at(10, 0)

    def hang(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        time.sleep(60)
        return 200, {}, json.dumps({"ok": True, "result": {"message_id": 1}})

    # Telegram would hang for 60 s on every call.
    fake_telegram.rsps.add_callback(
        responses.POST, re.compile(r"https://api\.telegram\.org/.*"), callback=hang
    )
    request = rf.get("/hb", headers={"authorization": f"Bearer {location.device_key}"})

    started = time.monotonic()
    response: HttpResponse = HeartbeatView.as_view(clock=FakeClock(_at(10, 30)))(request)
    elapsed = time.monotonic() - started

    assert (response.status_code, response.content) == (200, b"ok")
    assert elapsed < 1.0
    assert len(fake_telegram.calls) == 0
    state = _state(location)
    assert (state.status, state.on_since) == ("on", _at(10, 30))
    [on] = OutboxMessage.objects.filter(kind="power_on")
    assert on.event_at == _at(10, 30)
    assert on.payload == {"was_off_us": 1_800_000_000}
