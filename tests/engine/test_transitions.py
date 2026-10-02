"""Heartbeat gates and the stored power timeline (KD1, MON-01, MON-03; K-1, K-3, PITFALLS 15).

Heartbeats write the timeline in the same transaction as the state change, and
PostgreSQL itself rejects overlapping, zero-length, second-open and inconsistent
intervals, so no code path can corrupt the one source of truth. The first heartbeat is
silent (K-1); the first heartbeat after an outage turns the location ON and queues
exactly one ON alert in the same transaction (K-3, D-14). A restore clamped after a
backward clock step (IN-01) dates the ON alert at the clamp but records it at the
heartbeat's receive time, so the relay can send it at once (audit A2). An OFF is
recorded from the open interval's start when a lapse carve moved that start past the
decided outage start (D2), so it never closes an interval before its start.

Whether an alert is sent is decided when its transition is recorded (INV-05, D-06): the
OFF reads alerts_enabled from its own CAS UPDATE (Phase 1 WR-05), never from the
detector's snapshot, and a transition recorded while alerts are off queues nothing, for
good ("suppressed, not queued"). INV-05 #2 runs end to end, through detection, the
heartbeat gate, the outbox and the relay.

Tests that reach OFF through ``detection.run_cycle`` are ``django_db(transaction=True)``:
the cycle calls ``close_old_connections()``, which would close the connection inside
pytest-django's per-test transaction.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db import IntegrityError, connection, transaction

from powermon.alerts import texts
from powermon.alerts.models import OutboxMessage
from powermon.engine import lapse, rules, timeline, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations.models import Location
from powermon.worker import detection, io_loop

Interval = tuple[str, datetime, datetime | None, datetime | None]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _set_open_state(
    location: Any, at: datetime, state: str | None, outage_start_at: datetime | None = None
) -> None:
    with transaction.atomic(), connection.cursor() as cur:
        timeline.set_open_state(cur, location.pk, at, state, outage_start_at)


def _insert(location: Any, state: str, start: datetime, end: datetime | None, **extra: Any) -> None:
    PowerInterval.objects.create(
        location=location, state=state, start_at=start, end_at=end, **extra
    )


def _assert_rejected(constraint: str, location: Any, *args: Any, **extra: Any) -> None:
    # The savepoint keeps the test transaction usable after the failed INSERT.
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        _insert(location, *args, **extra)


def _kinds() -> list[str]:
    """The kinds of all outbox rows, oldest first."""
    return list(OutboxMessage.objects.order_by("id").values_list("kind", flat=True))


def _off_since_1005(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """K-2: heartbeats every 60 s from 10:00:00 to 10:05:00, OFF at the cycle at 10:06:31."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": None}
    )
    location = location_factory(**overrides)
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    assert detection.run_cycle(_at(10, 6, 31)) == 1
    return location


# The first heartbeat opens the timeline (K-1, MON-01)


@pytest.mark.django_db
def test_K1_first_heartbeat_opens_one_on_interval(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory()
    assert fixed_now == _at(8, 0)

    assert transitions.record_heartbeat(location.pk, fixed_now) == "started"

    assert LocationState.objects.get(pk=location.pk).status == "on"
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    # No data before the first heartbeat: nothing starts before 08:00:00.
    assert not PowerInterval.objects.filter(location=location, start_at__lt=fixed_now).exists()


@pytest.mark.django_db
def test_K1_first_heartbeat_queues_no_alert(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory()

    assert transitions.record_heartbeat(location.pk, fixed_now) == "started"
    assert transitions.record_heartbeat(location.pk, _at(8, 1)) == "plain"

    # Monitoring starts silently (MON-01): no alert for the first heartbeat or the next.
    assert _kinds() == []


# Power returns: ON at the first heartbeat after the outage, one ON alert (K-3, MON-03)


@pytest.mark.django_db(transaction=True)
def test_K3_restore_queues_on_alert_was_off_55m(location_factory: Callable[..., Any]) -> None:
    location = _off_since_1005(location_factory)

    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.on_since, state.last_heartbeat_at) == ("on", _at(11, 0), _at(11, 0))
    off, on = OutboxMessage.objects.order_by("id")
    assert (off.kind, on.kind) == ("power_off", "power_on")
    assert (on.channel, on.status, on.location_id) == ("subscriber", "pending", location.pk)
    assert (on.event_at, on.recorded_at) == (_at(11, 0), _at(11, 0))
    # "Was OFF for" = restore time - outage start = 11:00:00 - 10:05:00, in integer µs.
    assert on.payload == {"was_off_us": 3_300_000_000}
    assert texts.render_alert(on.kind, location.language, on.payload["was_off_us"]) == (
        "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>"
    )
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), _at(11, 0), _at(10, 5)),
        ("on", _at(11, 0), None, None),
    ]


@pytest.mark.django_db(transaction=True)
def test_restore_with_alerts_off_queues_nothing(location_factory: Callable[..., Any]) -> None:
    location = _off_since_1005(location_factory, alerts_enabled=False)

    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    # Alerts off suppresses the alert only: the state and the timeline still change.
    assert LocationState.objects.get(pk=location.pk).status == "on"
    assert _kinds() == []
    assert _intervals(location)[-2:] == [
        ("off", _at(10, 5), _at(11, 0), _at(10, 5)),
        ("on", _at(11, 0), None, None),
    ]


# Alerts on or off is decided when the transition is recorded (INV-05, D-06; Phase 1
# WR-05). The OFF reads alerts_enabled from its own CAS row, never from the detector's
# snapshot, and a transition recorded while alerts are off queues nothing, for good.


def _silent_after_1005(location_factory: Callable[..., Any], **overrides: Any) -> Any:
    """K-2: heartbeats every 60 s from 10:00:00 to 10:05:00, detection resumed at 09:00."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": None}
    )
    location = location_factory(**overrides)
    for minute in range(6):
        transitions.record_heartbeat(location.pk, _at(10, minute))
    return location


def _set_alerts(location: Any, enabled: bool) -> None:
    """The admin's alerts toggle: one configuration column, nothing else (D-05)."""
    assert Location.objects.filter(pk=location.pk).update(alerts_enabled=enabled) == 1


@pytest.mark.django_db(transaction=True)
def test_INV05_2_alerts_off_at_off_and_on_before_restore_sends_only_on(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # INV-05 #2: alerts are off when the OFF is recorded and back on before power returns.
    location = _silent_after_1005(location_factory)
    _set_alerts(location, False)

    assert detection.run_cycle(_at(10, 6, 31)) == 1
    # The OFF is recorded (state and timeline), but with alerts off it queues nothing.
    assert LocationState.objects.get(pk=location.pk).status == "off"
    assert _kinds() == []

    _set_alerts(location, True)
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    [on] = OutboxMessage.objects.all()
    assert (on.kind, on.payload) == ("power_on", {"was_off_us": 3_300_000_000})
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    assert io_loop.run_iteration(FakeClock(_at(11, 0, 5)), io_loop.RelayState()) is True
    # Exactly one Telegram call in total, and it is the ON alert: the OFF is never sent.
    assert len(fake_telegram.calls) == 1
    assert fake_telegram.sent == [
        {
            "chat_id": DEFAULT_CHAT_ID,
            "text": "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>",
            "parse_mode": "HTML",
        }
    ]
    assert OutboxMessage.objects.get(pk=on.pk).status == "sent"
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("off", _at(10, 5), _at(11, 0), _at(10, 5)),
        ("on", _at(11, 0), None, None),
    ]


@pytest.mark.django_db
def test_WR05_alert_decision_comes_from_the_cas_not_the_snapshot(
    location_factory: Callable[..., Any],
) -> None:
    # The detector reads its snapshot while alerts are on; the admin turns them off before
    # the OFF transaction runs. The OFF is recorded with the setting of that moment: off.
    location = _silent_after_1005(location_factory)
    [snap] = transitions.read_snapshots()
    decision = rules.decide(snap, rules.Anchors(detection_resumed_at=_at(9, 0)), _at(10, 6, 31))
    _set_alerts(location, False)

    assert transitions.mark_off(snap, decision, _at(10, 6, 31)) is True

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.outage_started_at) == ("off", _at(10, 5))
    assert _kinds() == []
    assert _intervals(location)[-1] == ("off", _at(10, 5), None, _at(10, 5))


@pytest.mark.django_db
def test_WR05_alerts_turned_on_after_the_snapshot_queue_the_off_alert(
    location_factory: Callable[..., Any],
) -> None:
    # The mirror case: the snapshot is read while alerts are off, the admin turns them on,
    # then the OFF is recorded. Alerts are on at that moment, so the OFF alert is queued.
    location = _silent_after_1005(location_factory, alerts_enabled=False)
    [snap] = transitions.read_snapshots()
    decision = rules.decide(snap, rules.Anchors(detection_resumed_at=_at(9, 0)), _at(10, 6, 31))
    _set_alerts(location, True)

    assert transitions.mark_off(snap, decision, _at(10, 6, 31)) is True

    [off] = OutboxMessage.objects.all()
    assert (off.kind, off.location_id, off.event_at, off.recorded_at) == (
        "power_off",
        location.pk,
        _at(10, 5),
        _at(10, 6, 31),
    )
    assert off.payload == {"was_on_us": 300_000_000}


@pytest.mark.django_db
def test_WR05_a_lost_cas_writes_nothing_whatever_the_alerts_setting(
    location_factory: Callable[..., Any],
) -> None:
    # A heartbeat between the snapshot and the CAS bumps state_version: the CAS returns no
    # row, so the stale OFF is skipped and its alerts setting is never read (INV-01).
    location = _silent_after_1005(location_factory, alerts_enabled=False)
    [snap] = transitions.read_snapshots()
    decision = rules.decide(snap, rules.Anchors(detection_resumed_at=_at(9, 0)), _at(10, 6, 31))
    _set_alerts(location, True)
    assert transitions.record_heartbeat(location.pk, _at(10, 6, 32)) == "plain"

    assert transitions.mark_off(snap, decision, _at(10, 6, 31)) is False

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.outage_started_at) == ("on", None)
    assert _kinds() == []
    assert _intervals(location) == [("on", _at(10, 0), None, None)]


@pytest.mark.django_db(transaction=True)
def test_INV05_alerts_turned_back_on_never_replay_a_suppressed_off(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # "Suppressed, not queued" (PROJECT.md, D-06): an OFF recorded while alerts are off is
    # never held for later. Turning alerts back on sends nothing for it, on any later pass.
    location = _silent_after_1005(location_factory, alerts_enabled=False)
    assert detection.run_cycle(_at(10, 6, 31)) == 1

    _set_alerts(location, True)
    # Later cycles see an off location: there is nothing to decide, nothing to queue.
    assert detection.run_cycle(_at(10, 7, 31)) == 0
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(_at(10, 7, 32)), state) is False
    assert io_loop.run_iteration(FakeClock(_at(11, 0)), state) is False

    assert LocationState.objects.get(pk=location.pk).status == "off"
    assert not OutboxMessage.objects.exists()
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_heartbeat_after_restore_is_plain(location_factory: Callable[..., Any]) -> None:
    location = _off_since_1005(location_factory)
    assert transitions.record_heartbeat(location.pk, _at(11, 0)) == "restored"

    assert transitions.record_heartbeat(location.pk, _at(11, 1)) == "plain"

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.on_since, state.last_heartbeat_at) == ("on", _at(11, 0), _at(11, 1))
    assert _kinds() == ["power_off", "power_on"]
    assert _intervals(location)[-1] == ("on", _at(11, 0), None, None)


@pytest.mark.django_db(transaction=True)
def test_IN01_backward_clock_step_heartbeat_restores_at_outage_start(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # IN-01: the server clock stepped back after the OFF, so the heartbeat's receive time
    # (10:04) lies before the outage start (10:05). The restore is clamped to the outage
    # start: no 500, the CHECK holds, and "was OFF for" is 0, never negative.
    location = _off_since_1005(location_factory)
    other = _off_since_1005(location_factory, name="Other location")
    monkeypatch.setattr(transitions, "_restore_clamp_warned", False)
    caplog.set_level(logging.WARNING, logger=transitions.__name__)

    assert transitions.record_heartbeat(location.pk, _at(10, 4)) == "restored"

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.on_since, state.last_heartbeat_at) == ("on", _at(10, 5), _at(10, 5))
    # The off piece would end where it starts: it is deleted, never closed before its start.
    assert _intervals(location) == [
        ("on", _at(10, 0), _at(10, 5), None),
        ("on", _at(10, 5), None, None),
    ]
    on = OutboxMessage.objects.get(location=location, kind="power_on")
    # The alert is dated at the clamped restore, but recorded (and so due) at the receive
    # time: the relay sends it now, not when the wall clock catches up (audit A2).
    assert (on.event_at, on.recorded_at, on.next_attempt_at) == (_at(10, 5), _at(10, 4), _at(10, 4))
    assert on.payload == {"was_off_us": 0}

    # One WARNING per process: a second clamped restore logs nothing more.
    assert transitions.record_heartbeat(other.pk, _at(10, 4)) == "restored"
    warnings = [r for r in caplog.records if r.name == transitions.__name__]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert f"location {location.pk} " in warnings[0].getMessage()
    assert warnings[0].exc_info is None


@pytest.mark.django_db
def test_IN01_restore_never_closes_before_the_open_interval_start(
    location_factory: Callable[..., Any],
) -> None:
    # What a lapse carve leaves (MON-05): an outage since 09:00 cut by a not-monitored
    # span [10:00, 10:10), its open off piece now starting at 10:10. A heartbeat received
    # at 10:09:59 that commits after the carve restores at 10:10, the open piece's start.
    location = location_factory()
    _insert(location, "off", _at(9, 0), _at(10, 0), outage_start_at=_at(9, 0))
    _insert(location, "not_monitored", _at(10, 0), _at(10, 10))
    _insert(location, "off", _at(10, 10), None, outage_start_at=_at(9, 0))
    LocationState.objects.filter(pk=location.pk).update(
        status="off",
        on_since=_at(8, 0),
        last_heartbeat_at=_at(9, 0),
        outage_started_at=_at(9, 0),
    )

    assert transitions.record_heartbeat(location.pk, _at(10, 9, 59)) == "restored"

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.on_since, state.last_heartbeat_at) == (
        "on",
        _at(10, 10),
        _at(10, 10),
    )
    assert _intervals(location) == [
        ("off", _at(9, 0), _at(10, 0), _at(9, 0)),
        ("not_monitored", _at(10, 0), _at(10, 10), None),
        ("on", _at(10, 10), None, None),
    ]
    [on] = OutboxMessage.objects.filter(location=location)
    # Dated at the open piece's start, recorded (and due) at the receive time.
    assert (on.kind, on.event_at, on.recorded_at, on.next_attempt_at) == (
        "power_on",
        _at(10, 10),
        _at(10, 9, 59),
        _at(10, 9, 59),
    )
    # D-02: "was OFF for" runs from the original outage start, 10:10 - 09:00.
    assert on.payload == {"was_off_us": 4_200_000_000}


@pytest.mark.django_db
def test_IN01_restore_without_an_open_interval_clamps_to_the_outage_start(
    location_factory: Callable[..., Any],
) -> None:
    # An off location with no stored interval (none to clamp to): the outage start alone
    # keeps "was OFF for" from going negative after a backward clock step.
    location = location_factory()
    LocationState.objects.filter(pk=location.pk).update(
        status="off",
        on_since=_at(10, 0),
        last_heartbeat_at=_at(10, 5),
        outage_started_at=_at(10, 5),
    )

    assert transitions.record_heartbeat(location.pk, _at(10, 4)) == "restored"

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.on_since, state.last_heartbeat_at) == ("on", _at(10, 5), _at(10, 5))
    assert _intervals(location) == [("on", _at(10, 5), None, None)]
    [on] = OutboxMessage.objects.filter(location=location)
    assert (on.event_at, on.payload) == (_at(10, 5), {"was_off_us": 0})


@pytest.mark.django_db(transaction=True)
def test_IN01_clamped_on_alert_is_sent_at_the_receive_time(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # OFF since 10:05, recorded and sent at 10:06:31. Then the server clock steps back
    # 1 h, and power returns at 09:07:30 by the stepped clock. The restore is clamped to
    # 10:05, but the ON alert goes out on the relay's next pass at 09:07:30, not about an
    # hour later when the wall clock reaches 10:05 again (audit A2).
    location = _off_since_1005(location_factory)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    state = io_loop.RelayState()
    assert io_loop.run_iteration(FakeClock(_at(10, 6, 31)), state) is True

    assert transitions.record_heartbeat(location.pk, _at(9, 7, 30)) == "restored"

    assert io_loop.run_iteration(FakeClock(_at(9, 7, 30)), state) is True
    on = OutboxMessage.objects.get(location=location, kind="power_on")
    assert (on.status, on.sent_at) == ("sent", _at(9, 7, 30))
    assert [message["text"] for message in fake_telegram.sent][1:] == [
        texts.render_alert("power_on", location.language, 0)
    ]


# D2 (final audit): an OFF never starts before the open interval it closes. A lapse carve
# can move the open on piece's start past the decided outage start (a carver that lost
# the cursor CAS with a later now, or a carve between the snapshot and the CAS). The OFF
# is then recorded from the open piece's start, the same clamp as IN-01's restore.


def _on_since_0800_carved_until_1010(location: Any) -> None:
    """On since 08:00, last heartbeat 09:59, a carved gap [10:00, 10:10), on again from 10:10."""
    _insert(location, "on", _at(8, 0), _at(10, 0))
    _insert(location, "not_monitored", _at(10, 0), _at(10, 10))
    _insert(location, "on", _at(10, 10), None)
    LocationState.objects.filter(pk=location.pk).update(
        status="on", on_since=_at(8, 0), last_heartbeat_at=_at(9, 59), state_version=1
    )


def _decided(resumed: datetime, now: datetime) -> tuple[rules.Snapshot, rules.Decision]:
    [snap] = transitions.read_snapshots()
    decision = rules.decide(snap, rules.Anchors(detection_resumed_at=resumed), now)
    assert decision.off
    return snap, decision


def _transition_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == transitions.__name__]


@pytest.mark.django_db
@pytest.mark.parametrize("shape", ["on_before_the_start", "on_from_the_start"])
def test_D2_mark_off_keeps_the_decided_outage_start_in_the_normal_case(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture, shape: str
) -> None:
    location = location_factory()
    if shape == "on_before_the_start":
        # K-2: on since 10:00, silent after 10:05; the open piece began long before.
        for minute in range(6):
            transitions.record_heartbeat(location.pk, _at(10, minute))
        snap, decision = _decided(_at(9, 0), _at(10, 6, 31))
        start, previous = _at(10, 5), ("on", _at(10, 0), _at(10, 5), None)
    else:
        # INV-11 #1: the fresh window and the open piece both start at the carve end.
        _on_since_0800_carved_until_1010(location)
        snap, decision = _decided(_at(10, 10), _at(10, 11, 31))
        start, previous = _at(10, 10), ("not_monitored", _at(10, 0), _at(10, 10), None)
    assert decision.outage_start == start
    caplog.set_level(logging.WARNING, logger=transitions.__name__)

    assert transitions.mark_off(snap, decision, _at(10, 11, 31)) is True

    assert LocationState.objects.get(pk=location.pk).outage_started_at == start
    assert _intervals(location)[-2:] == [previous, ("off", start, None, start)]
    [off] = OutboxMessage.objects.filter(location=location)
    assert (off.kind, off.event_at) == ("power_off", start)
    assert _transition_warnings(caplog) == []


@pytest.mark.django_db
def test_D2_mark_off_starts_the_off_where_a_later_carve_moved_the_open_piece(
    location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    _on_since_0800_carved_until_1010(location)
    snap, decision = _decided(_at(10, 10), _at(10, 11, 31))
    assert decision.outage_start == _at(10, 10)
    # After the snapshot, a carve up to 10:10:00.2 commits: the open piece starts later.
    moved = _at(10, 10) + timedelta(milliseconds=200)
    assert lapse.carve_window(_at(10, 0), moved) == 1
    caplog.set_level(logging.WARNING, logger=transitions.__name__)

    # Before D2 this closed the open piece before its start: power_interval_end_after_start.
    assert transitions.mark_off(snap, decision, _at(10, 11, 31)) is True

    state = LocationState.objects.get(pk=location.pk)
    assert (state.status, state.outage_started_at) == ("off", moved)
    assert _intervals(location)[-2:] == [
        ("not_monitored", _at(10, 10), moved, None),
        ("off", moved, None, moved),
    ]
    [off] = OutboxMessage.objects.filter(location=location)
    assert (off.kind, off.event_at, off.recorded_at) == ("power_off", moved, _at(10, 11, 31))
    # "Was ON for" is still last heartbeat - on time (D-03), unaffected by the clamp.
    assert off.payload == {"was_on_us": (_at(9, 59) - _at(8, 0)) // timedelta(microseconds=1)}
    [warning] = _transition_warnings(caplog)
    assert warning.levelno == logging.WARNING
    assert f"location {location.pk} " in warning.getMessage()
    assert warning.exc_info is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "stale"),
    [("waiting", "on"), ("waiting", "off"), ("on", "waiting")],
)
def test_MON04_gate_that_misses_the_locked_row_raises_and_writes_nothing(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    fixed_now: datetime,
    status: str,
    stale: str,
) -> None:
    # The second line of defence: under the row lock every gate matches its row. If one
    # ever changes 0 rows (here the lock read is faked to return a stale status), the
    # heartbeat raises and rolls back instead of writing a transition or an alert.
    location = location_factory()
    if status == "on":
        assert transitions.record_heartbeat(location.pk, fixed_now) == "started"
    before = LocationState.objects.filter(pk=location.pk).values().get()
    stored = _intervals(location)
    stale_lock = (
        f"SELECT '{stale}', NULL::timestamptz FROM location_state "  # noqa: S608
        "WHERE location_id = %s FOR UPDATE"
    )
    monkeypatch.setattr(transitions, "LOCK_SQL", stale_lock)

    with pytest.raises(RuntimeError, match=f"location {location.pk}: heartbeat gate changed 0"):
        transitions.record_heartbeat(location.pk, _at(8, 1))

    assert LocationState.objects.filter(pk=location.pk).values().get() == before
    assert _intervals(location) == stored
    assert _kinds() == []


@pytest.mark.django_db
def test_plain_heartbeats_keep_one_open_interval(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory()
    transitions.record_heartbeat(location.pk, fixed_now)
    opened = PowerInterval.objects.get(location=location)

    results = [transitions.record_heartbeat(location.pk, _at(8, m)) for m in (1, 2)]

    assert results == ["plain", "plain"]
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    assert PowerInterval.objects.get(location=location).pk == opened.pk


@pytest.mark.django_db
def test_first_heartbeat_in_maintenance_opens_not_monitored(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(maintenance=True)

    assert transitions.record_heartbeat(location.pk, fixed_now) == "started"

    # The live status is on; the timeline records the span as not monitored (LOC-08).
    assert LocationState.objects.get(pk=location.pk).status == "on"
    assert _intervals(location) == [("not_monitored", _at(8, 0), None, None)]


@pytest.mark.django_db
def test_record_heartbeat_for_an_unknown_location_writes_no_interval(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location_factory()

    assert transitions.record_heartbeat(10**9, fixed_now) == "ignored"

    assert not PowerInterval.objects.exists()


@pytest.mark.django_db
def test_config_row_reads_maintenance_and_alerts(location_factory: Callable[..., Any]) -> None:
    default = location_factory()
    toggled = location_factory(maintenance=True, alerts_enabled=False)

    with connection.cursor() as cur:
        assert transitions._config_row(cur, default.pk) == (False, True)
        assert transitions._config_row(cur, toggled.pk) == (True, False)
        with pytest.raises(LookupError, match="no location row"):
            transitions._config_row(cur, 10**9)


# set_open_state: close then open, never zero length, idempotent


@pytest.mark.django_db
def test_set_open_state_closes_then_opens(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(8, 0), "on")

    _set_open_state(location, _at(10, 5), "off", _at(10, 5))

    assert _intervals(location) == [
        ("on", _at(8, 0), _at(10, 5), None),
        ("off", _at(10, 5), None, _at(10, 5)),
    ]


@pytest.mark.django_db
def test_set_open_state_deletes_zero_length_interval(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(10, 0), "on")

    _set_open_state(location, _at(10, 0), "off", _at(10, 0))

    assert _intervals(location) == [("off", _at(10, 0), None, _at(10, 0))]


@pytest.mark.django_db
def test_set_open_state_is_idempotent(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(8, 0), "on")
    _set_open_state(location, _at(9, 0), "on")
    assert _intervals(location) == [("on", _at(8, 0), None, None)]
    _set_open_state(location, _at(10, 5), "off", _at(10, 5))
    before = _intervals(location)

    # Same state and same outage start as the open interval: nothing changes.
    _set_open_state(location, _at(10, 6), "off", _at(10, 5))
    _set_open_state(location, _at(10, 7), "off", _at(10, 5))

    assert _intervals(location) == before


@pytest.mark.django_db
def test_set_open_state_new_outage_start_is_a_new_off_interval(
    location_factory: Callable[..., Any],
) -> None:
    # INV-01 acceptance 2 shape: off since 10:01, restore at 13:00, silence again.
    location = location_factory()
    _set_open_state(location, _at(10, 1), "off", _at(10, 1))
    _set_open_state(location, _at(13, 0), "on")

    _set_open_state(location, _at(13, 0), "off", _at(13, 0))

    assert _intervals(location) == [
        ("off", _at(10, 1), _at(13, 0), _at(10, 1)),
        ("off", _at(13, 0), None, _at(13, 0)),
    ]


@pytest.mark.django_db
def test_set_open_state_none_only_closes(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _set_open_state(location, _at(8, 0), "on")

    _set_open_state(location, _at(9, 0), None)
    _set_open_state(location, _at(9, 30), None)

    assert _intervals(location) == [("on", _at(8, 0), _at(9, 0), None)]


@pytest.mark.django_db
def test_set_open_state_rejects_closing_before_the_open_start(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    _set_open_state(location, _at(10, 0), "on")

    with pytest.raises(IntegrityError, match="power_interval_end_after_start"):
        _set_open_state(location, _at(9, 59), "off", _at(9, 59))

    assert _intervals(location) == [("on", _at(10, 0), None, None)]


# PostgreSQL enforces the timeline invariants (KD1, PITFALLS 15)


@pytest.mark.django_db
def test_db_rejects_overlapping_intervals(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    other = location_factory()
    _insert(location, "on", _at(8, 0), _at(10, 0))

    _assert_rejected("power_interval_no_overlap", location, "on", _at(9, 0), _at(11, 0))

    # Half-open [start, end): an adjacent interval does not overlap.
    _insert(location, "off", _at(10, 0), _at(11, 0), outage_start_at=_at(10, 0))
    # Another location's interval may overlap freely.
    _insert(other, "on", _at(8, 30), _at(9, 30))
    assert len(_intervals(location)) == 2
    assert len(_intervals(other)) == 1


@pytest.mark.django_db
def test_db_rejects_second_open_interval(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    _insert(location, "on", _at(8, 0), None)

    # Two open intervals always overlap, so the exclusion constraint (created with the
    # table) usually reports first; the partial unique index is the second guard.
    _assert_rejected(
        "power_interval_(one_open|no_overlap)",
        location,
        "off",
        _at(9, 0),
        None,
        outage_start_at=_at(9, 0),
    )
    with connection.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'power_interval_one_open'")
        (indexdef,) = cur.fetchone()
    assert indexdef.startswith("CREATE UNIQUE INDEX power_interval_one_open")
    assert indexdef.endswith("(location_id) WHERE (end_at IS NULL)")


@pytest.mark.django_db
def test_db_rejects_zero_length_interval(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    _assert_rejected("power_interval_end_after_start", location, "on", _at(8, 0), _at(8, 0))
    _assert_rejected("power_interval_end_after_start", location, "on", _at(8, 0), _at(7, 59))


@pytest.mark.django_db
def test_db_rejects_off_without_outage_start(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    _assert_rejected("power_interval_outage_start_iff_off", location, "off", _at(8, 0), None)


@pytest.mark.django_db
def test_db_rejects_on_with_outage_start(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    for state in ("on", "not_monitored"):
        _assert_rejected(
            "power_interval_outage_start_iff_off",
            location,
            state,
            _at(8, 0),
            None,
            outage_start_at=_at(8, 0),
        )


@pytest.mark.django_db
def test_db_rejects_unknown_interval_state(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    _assert_rejected("power_interval_state_valid", location, "waiting", _at(8, 0), None)


@pytest.mark.django_db
def test_system_state_is_a_singleton() -> None:
    # Migration 0002 inserts the row; .get() also proves there is exactly one.
    row = SystemState.objects.get()
    assert row.pk == 1
    anchors = (row.web_started_at, row.last_cycle_completed_at, row.detection_resumed_at)
    assert anchors == (None, None, None)

    with pytest.raises(IntegrityError, match="system_state_singleton"), transaction.atomic():
        SystemState.objects.create(id=2)
