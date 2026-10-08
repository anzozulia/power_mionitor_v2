"""All-silent tells the admin once each way, and never changes a subscriber alert (OPS-04).

INV-12 with the D-01 default (no hold): when at least 2 active locations (on or off, not in
maintenance, not deleted) have each gone longer than their own period without a heartbeat,
counted from max(last heartbeat, end of the last lapse), the admin gets 1 neutral ops alert
(D-12); the first heartbeat after its start ends it with 1 recovery notice. Subscribers get
exactly the OFF and ON alerts they would get without the check.

What these scenarios prove:
- INV-12 #1: 3 locations silent from 14:09:10-14:10:00 (Kyiv) until 14:13:00-14:13:30 give
  one start notice at the 14:11:05 cycle and one end notice naming the first location back,
  and the same 3 OFF and 3 ON rows as with the check disabled;
- INV-12 #2 and #3: one silent location of two, or a single location, never starts it;
- the edges: silence is strict at exactly one period; only active locations count; the end
  names the earliest heartbeat, the lowest id on a tie; a lapse carve never ends it; a
  restarted worker sends no second start;
- D-04 (refined after the wave-1 audit): the start still counts only active locations; the
  end is the first heartbeat after the start from an active location, or the first one
  received after the incident was opened (detected) from a location in maintenance
  (monitored, not deleted). A maintenance beat between the backdated start and the
  detection ends nothing (in the INV-12 #1 shape it sent a false "Heartbeats are back"
  during the outage and swallowed the real recovery notice). Neither does that beat once
  the admin turns maintenance off during the incident (wave-2 audit): a location that left
  maintenance after the start, on (D-02 starts its window at the exit) or off, counts only
  after the open too, so there is no false end and no second start for the same silence.
  Putting locations into maintenance or deleting them never ends it by itself. An incident
  with no stored open time (opened before the rule) counts maintenance beats from the first
  evaluation that sees it;
- exactly once (D-11): two evaluations at once open one incident and send one notice, and
  two at once close it with one notice (the partial unique index and the conditional
  close, not a code convention);
- the wiring: ``run_detection`` evaluates after its decisions, skips the check on a cycle
  skipped for a clock step, and an error in the check never stops detection.

Histories are built with ``record_heartbeat`` and ``run_cycle``; time is fixed aware UTC
on 2026-10-01, shown in Europe/Kyiv (UTC+3). Tests that run a cycle or use actor threads
are ``django_db(transaction=True)``; their teardown truncates the system_state singleton,
so each test writes the row it needs.
"""

import dataclasses
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import Actor, FakeClock, terminate_backends
from django.db import InterfaceError, OperationalError, connection

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import all_silent, lapse, maintenance, transitions
from powermon.engine.models import LocationState, SystemState
from powermon.locations.models import Location
from powermon.worker import detection

OPS_LOGGER = "powermon.alerts.ops"
START_3_SINCE_14_10 = (
    "⚠️ All 3 active locations silent since 14:10: an area power/ISP outage or a "
    "server/network problem. Subscriber alerts continue as normal."
)
END_FIRST_A_14_13 = "✅ Heartbeats are back (first: A, 14:13:00); all-silent lasted 3m."
ACTOR_WAIT_S = 15.0

Alert = tuple[str, str, datetime]


def _at(hour: int, minute: int, second: int = 0, microsecond: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, microsecond, tzinfo=UTC)


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so no expected text depends on the env file."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    return settings


@pytest.fixture
def no_ops_chat(settings: Any) -> Any:
    """``settings.CFG`` with no ops chat, whatever the env file says (D-09)."""
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token="", ops_chat_id=None)
    return settings


def _system(cursor: datetime | None, resumed: datetime | None) -> None:
    """The system_state singleton: the cursor and the end of the last lapse."""
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "last_cycle_completed_at": cursor,
            "detection_resumed_at": resumed,
            "web_started_at": None,
        },
    )


def _silent(
    location_factory: Callable[..., Any], name: str, last: datetime, **overrides: Any
) -> Any:
    """A location that is on with ``last`` as its only heartbeat."""
    location = location_factory(name=name, **overrides)
    assert transitions.record_heartbeat(location.pk, last) == "started"
    return location


def _ops_rows(kind: str | None = None) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS).order_by("id")
    return list(rows if kind is None else rows.filter(kind=kind))


def _starts() -> list[OutboxMessage]:
    return _ops_rows(outbox.KIND_OPS_ALL_SILENT_START)


def _ends() -> list[OutboxMessage]:
    return _ops_rows(outbox.KIND_OPS_ALL_SILENT_END)


def _incidents() -> list[tuple[int | None, datetime, datetime | None]]:
    rows = OpsIncident.objects.filter(kind=all_silent.KIND_ALL_SILENT).order_by("id")
    return [(r.location_id, r.started_at, r.ended_at) for r in rows]


def _render(row: OutboxMessage) -> str:
    """The notice as the relay would send it at the time it was recorded."""
    return ops.render_text(row.kind, row.payload, row.location_id, now=row.recorded_at)


def _subscriber_alerts() -> list[Alert]:
    """Every subscriber alert as (kind, location name, event time), oldest first."""
    rows = (
        OutboxMessage.objects.filter(channel=outbox.CHANNEL_SUBSCRIBER)
        .select_related("location")
        .order_by("id")
    )
    return [(r.kind, r.location.name if r.location else "", r.event_at) for r in rows]


def _steps(first: datetime, last: datetime, every: timedelta) -> Iterator[datetime]:
    at = first
    while at <= last:
        yield at
        at += every


def _together(monkeypatch: pytest.MonkeyPatch, name: str) -> Callable[[], None]:
    """Make two callers of ``ops.<name>`` meet before either runs it; return the undo."""
    real = getattr(ops, name)
    barrier = threading.Barrier(2, timeout=10)

    def met(*args: Any, **kwargs: Any) -> Any:
        barrier.wait()
        return real(*args, **kwargs)

    monkeypatch.setattr(ops, name, met)
    return lambda: monkeypatch.setattr(ops, name, real)


def _evaluate_twice_at_once(now: datetime) -> list[Any]:
    """Two actors, each on its own connection, call ``evaluate(now)``; their results."""
    actors = [Actor(lambda: all_silent.evaluate(now)) for _ in range(2)]
    for actor in actors:
        actor.start()
    try:
        for actor in actors:
            actor.join(ACTOR_WAIT_S)
        assert not any(actor.is_alive() for actor in actors), "an actor is stuck"
    finally:
        # A stuck actor's transaction must not block the teardown.
        if any(actor.is_alive() for actor in actors):
            terminate_backends(Actor.APPLICATION_NAME)
    assert [actor.exc for actor in actors] == [None, None]
    return [actor.result for actor in actors]


# INV-12 #1: three locations silent at once (D-01, D-12)

# The heartbeats resume at 14:13:00, 14:13:15 and 14:13:30 Kyiv (11:13:00-11:13:30 UTC).
RESUMES = {_at(11, 13): "A", _at(11, 13, 15): "B", _at(11, 13, 30): "C"}
# Exactly what subscribers get with or without the all-silent check: each location's OFF at
# its own timeout (period 60 s + grace 30 s), backdated to its last heartbeat, and its ON
# at its first heartbeat back.
INV12_ALERTS: list[Alert] = [
    ("power_off", "A", _at(11, 9, 10)),
    ("power_off", "B", _at(11, 9, 40)),
    ("power_off", "C", _at(11, 10)),
    ("power_on", "A", _at(11, 13)),
    ("power_on", "B", _at(11, 13, 15)),
    ("power_on", "C", _at(11, 13, 30)),
]


def _inv12_locations(location_factory: Callable[..., Any]) -> dict[str, Any]:
    """A, B and C (period 60 s), last heartbeats 14:09:10, 14:09:40 and 14:10:00 Kyiv."""
    _system(cursor=None, resumed=_at(9, 0))
    last = {"A": _at(11, 9, 10), "B": _at(11, 9, 40), "C": _at(11, 10)}
    locations = {}
    for name, at in last.items():
        locations[name] = _silent(location_factory, name, _at(11, 0))
        transitions.record_heartbeat(locations[name].pk, at)
    return locations


def _run_inv12(
    locations: dict[str, Any],
    *,
    evaluate: bool,
    maintenance_beats: Mapping[datetime, int] | None = None,
) -> dict[datetime, str]:
    """A cycle (and the all-silent check) every 5 s from 11:10:05 to 11:14:00 UTC.

    A heartbeat at a step's time arrives just after that step's cycle; so does a beat in
    ``maintenance_beats`` (step -> id of a location in maintenance). Returns the non-None
    results of the check by step.
    """
    outcomes: dict[datetime, str] = {}
    for at in _steps(_at(11, 10, 5), _at(11, 14), timedelta(seconds=5)):
        detection.run_cycle(at)
        if evaluate:
            result = all_silent.evaluate(at)
            if result is not None:
                outcomes[at] = result
        if maintenance_beats is not None and at in maintenance_beats:
            assert transitions.record_heartbeat(maintenance_beats[at], at) == "plain"
        if at in RESUMES:
            assert transitions.record_heartbeat(locations[RESUMES[at]].pk, at) == "restored"
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_INV12_all_silent_one_alert_one_recovery(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    locations = _inv12_locations(location_factory)

    outcomes = _run_inv12(locations, evaluate=True)

    # The start at the first cycle after 14:11:30 (C silent for more than 90 s: period 60 +
    # grace 30), the end at the first cycle after A's heartbeat.
    assert outcomes == {_at(11, 11, 35): "started", _at(11, 13, 5): "ended"}
    [start] = _starts()
    assert (start.recorded_at, start.location_id) == (_at(11, 11, 35), None)
    assert start.payload == {"since_us": ops.instant_us(_at(11, 10)), "count": 3}
    assert _render(start) == START_3_SINCE_14_10
    [end] = _ends()
    assert (end.recorded_at, end.location_id) == (_at(11, 13, 5), locations["A"].pk)
    assert end.payload == {
        "since_us": ops.instant_us(_at(11, 10)),
        "first_us": ops.instant_us(_at(11, 13)),
    }
    assert _render(end) == END_FIRST_A_14_13
    assert [row.pk for row in _ops_rows()] == [start.pk, end.pk]
    assert _incidents() == [(None, _at(11, 10), _at(11, 13))]
    # D-01: no hold, subscribers notice no difference.
    assert _subscriber_alerts() == INV12_ALERTS


@pytest.mark.django_db(transaction=True)
def test_INV12_subscriber_alerts_are_the_same_without_the_check(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    locations = _inv12_locations(location_factory)

    assert _run_inv12(locations, evaluate=False) == {}

    assert _subscriber_alerts() == INV12_ALERTS
    assert (_ops_rows(), _incidents()) == ([], [])


@pytest.mark.django_db(transaction=True)
def test_INV12_real_recovery_after_maintenance_beat_gets_one_end_notice(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """INV-12 #1 with a fourth, powered device in maintenance (D-04, wave-1 audit).

    M's device beat at 14:10:30 Kyiv: after the silence start (14:10:00, backdated) and
    before the detection (14:11:05). Then the ingress outage silenced it as well. That beat
    says nothing about the path after the detection, so the incident stays open until A's
    heartbeat at 14:13:00, which ends it with exactly one end notice naming A. Counting M's
    beat sent a false "Heartbeats are back (first: M)" at 14:11:10, and one incident per
    silence then swallowed the real recovery.
    """
    locations = _inv12_locations(location_factory)
    m = _silent(location_factory, "M", _at(11, 0), maintenance=True)

    outcomes = _run_inv12(locations, evaluate=True, maintenance_beats={_at(11, 10, 30): m.pk})

    assert outcomes == {_at(11, 11, 5): "started", _at(11, 13, 5): "ended"}
    [start] = _starts()
    assert _render(start) == START_3_SINCE_14_10
    [end] = _ends()
    assert (end.recorded_at, end.location_id) == (_at(11, 13, 5), locations["A"].pk)
    assert _render(end) == END_FIRST_A_14_13
    assert [row.pk for row in _ops_rows()] == [start.pk, end.pk]
    assert _incidents() == [(None, _at(11, 10), _at(11, 13))]
    # M is in maintenance: no subscriber alert for it, the others' alerts are unchanged.
    assert _subscriber_alerts() == INV12_ALERTS


@pytest.mark.django_db(transaction=True)
def test_INV12_maintenance_turned_off_mid_incident_sends_no_false_end_and_no_second_start(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """INV-12 #1 with M taken out of maintenance while the incident is open (wave-2 audit).

    The ISP outage shape, so the admin can still reach the panel. A and B are active and
    fall quiet at 13:59:20 and 13:59:40 Kyiv; M is in maintenance and its device beats at
    13:59:50, after the backdated start (13:59:40) and before the detection (14:00:45).
    At 14:05 the admin turns M's maintenance off (D-02: M is on, so its fresh detection
    window starts at 14:05). The beat M sent while in maintenance still says nothing about
    the path after the detection. Judging it by the active rule once the flag was off sent
    a false "Heartbeats are back (first: M, 13:59:50); all-silent lasted 10s" while the
    outage went on, and then a second start notice for the same silence. B's heartbeat at
    14:07:40 ends the incident, once.
    """
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(10, 59, 20))
    b = _silent(location_factory, "B", _at(10, 59, 40))
    m = _silent(location_factory, "M", _at(10, 59), maintenance=True)
    assert transitions.record_heartbeat(m.pk, _at(10, 59, 50)) == "plain"

    # A check every 5 s; the admin's click and B's heartbeat land just before that step's
    # check.
    outcomes: dict[datetime, str] = {}
    for at in _steps(_at(11, 0, 5), _at(11, 8, 30), timedelta(seconds=5)):
        if at == _at(11, 5):
            assert maintenance.set_maintenance(m.pk, False, at) is True
        if at == _at(11, 7, 40):
            assert transitions.record_heartbeat(b.pk, at) == "plain"
        result = all_silent.evaluate(at)
        if result is not None:
            outcomes[at] = result

    assert outcomes == {_at(11, 0, 45): "started", _at(11, 7, 40): "ended"}
    [start] = _starts()
    assert start.payload == {"since_us": ops.instant_us(_at(10, 59, 40)), "count": 2}
    [end] = _ends()
    assert (end.location_id, end.payload) == (
        b.pk,
        {
            "since_us": ops.instant_us(_at(10, 59, 40)),
            "first_us": ops.instant_us(_at(11, 7, 40)),
        },
    )
    assert _render(end) == "✅ Heartbeats are back (first: B, 14:07:40); all-silent lasted 8m."
    assert [row.pk for row in _ops_rows()] == [start.pk, end.pk]
    assert _incidents() == [(None, _at(10, 59, 40), _at(11, 7, 40))]
    # M is active again, with the fresh window D-02 wrote at the click.
    assert not Location.objects.get(pk=m.pk).maintenance
    assert LocationState.objects.get(pk=m.pk).window_start_at == _at(11, 5)


# INV-12 #2 and #3: not every active location silent, or only one active location


@pytest.mark.django_db(transaction=True)
def test_INV12_one_silent_of_two_no_ops_alert(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 9, 10))
    b = _silent(location_factory, "B", _at(11, 9, 30))
    transitions.record_heartbeat(b.pk, _at(11, 10))

    for at in _steps(_at(11, 10, 5), _at(11, 14), timedelta(seconds=5)):
        detection.run_cycle(at)
        assert all_silent.evaluate(at) is None
        if at.second in (0, 30):
            transitions.record_heartbeat(b.pk, at)  # B keeps beating every 30 s

    assert (_ops_rows(), _incidents()) == ([], [])
    # A's OFF is queued normally, at its own timeout.
    assert _subscriber_alerts() == [("power_off", "A", _at(11, 9, 10))]


@pytest.mark.django_db(transaction=True)
def test_INV12_single_location_never_triggers(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))

    for at in _steps(_at(11, 1), _at(12, 0), timedelta(minutes=1)):
        detection.run_cycle(at)
        assert all_silent.evaluate(at) is None

    assert (_ops_rows(), _incidents()) == ([], [])
    assert _subscriber_alerts() == [("power_off", "A", _at(11, 0))]


# INV-12 as amended 2026-10-08 (F-02): the threshold is the OFF timeout, period + grace

# A's device beats every 60.9 s: inside its 90 s timeout, but more than its 60 s period.
JITTER_EVERY = timedelta(seconds=60, milliseconds=900)


def _run_jitter(a: Any, beats: int | None, last_step: datetime) -> list[tuple[datetime, str]]:
    """A cycle and the all-silent check every 5 s from 11:00:05 to ``last_step``.

    A's beats (at 11:00 + k * 60.9 s, k = 1, 2, ..., up to ``beats`` if given) that are
    due by a step's time arrive just before that step's cycle. Returns the non-None
    results of the check with their step.
    """
    results: list[tuple[datetime, str]] = []
    k = 1
    for at in _steps(_at(11, 0, 5), last_step, timedelta(seconds=5)):
        while (beats is None or k <= beats) and _at(11, 0) + k * JITTER_EVERY <= at:
            assert transitions.record_heartbeat(a.pk, _at(11, 0) + k * JITTER_EVERY) == "plain"
            k += 1
        detection.run_cycle(at)
        result = all_silent.evaluate(at)
        if result is not None:
            results.append((at, result))
    return results


@pytest.mark.django_db(transaction=True)
def test_INV12_jitter_within_grace_never_starts_all_silent_while_the_others_are_off(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    b = _silent(location_factory, "B", _at(11, 0))

    results = _run_jitter(a, None, _at(11, 30))

    # B went off at its own timeout; A, beating inside its timeout, stays on.
    assert LocationState.objects.get(pk=b.pk).status == "off"
    assert LocationState.objects.get(pk=a.pk).status == "on"
    # Under the old bare-period rule A's beat at 11:05:04.5 followed by the 11:06:05 cycle
    # (60.5 s of quiet, more than its 60 s period) opened a false incident, and A's next
    # beat closed it: a false start/end pair for the admin while only B was off.
    assert results == []
    assert (_incidents(), _starts(), _ends()) == ([], [], [])


@pytest.mark.django_db(transaction=True)
def test_INV12_jitter_then_real_silence_starts_once_at_the_first_cycle_past_the_timeout(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    last = _at(11, 0) + 9 * JITTER_EVERY
    assert last == _at(11, 9, 8, 100_000)

    results = _run_jitter(a, 9, _at(11, 14))

    # A falls quiet at 11:09:08.1; 11:10:40 is the first cycle more than 90 s later.
    assert results == [(_at(11, 10, 40), "started")]
    [start] = _starts()
    assert start.payload["since_us"] == ops.instant_us(last)
    assert start.recorded_at == _at(11, 10, 40)
    assert _incidents() == [(None, last, None)]


@pytest.mark.django_db(transaction=True)
def test_INV12_a_long_grace_location_keeps_all_silent_closed_until_its_own_timeout(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0), grace_s=300)

    # A is past its 90 s timeout, B (period 60 + grace 300) is not.
    assert all_silent.evaluate(_at(11, 1, 31)) is None
    # Exactly B's 360 s timeout: not silent yet (strict).
    assert all_silent.evaluate(_at(11, 6)) is None
    assert all_silent.evaluate(_at(11, 6) + timedelta(microseconds=1)) == "started"
    assert _incidents() == [(None, _at(11, 0), None)]


# The silence rule itself (pure)


def test_all_silent_boundary_exactly_one_timeout() -> None:
    last = _at(11, 10)
    rows = [all_silent.Active(1, 90, last), all_silent.Active(2, 90, last - timedelta(seconds=5))]

    # Exactly one timeout after the last heartbeat a location is not silent yet (strict).
    assert all_silent.silence_since(rows, None, last + timedelta(seconds=90)) is None
    assert all_silent.silence_since(rows, None, last + timedelta(seconds=90, microseconds=1)) == (
        last
    )


def test_silence_is_counted_from_the_end_of_the_last_lapse() -> None:
    lapse_end = _at(11, 30)
    long_ago = [all_silent.Active(1, 60, _at(11, 0)), all_silent.Active(2, 60, _at(11, 1))]

    # Heartbeats from before the lapse count from its end (adjacency).
    assert all_silent.silence_since(long_ago, lapse_end, _at(11, 31)) is None
    assert all_silent.silence_since(long_ago, lapse_end, _at(11, 31, 0, 1)) == lapse_end

    # A heartbeat after the lapse end counts from the heartbeat.
    one_after = [all_silent.Active(1, 60, _at(11, 30, 20)), all_silent.Active(2, 60, _at(11, 0))]
    assert all_silent.silence_since(one_after, lapse_end, _at(11, 31, 20)) is None
    assert all_silent.silence_since(one_after, lapse_end, _at(11, 31, 21)) == _at(11, 30, 20)


def test_each_location_is_silent_after_its_own_timeout() -> None:
    last = _at(11, 0)
    rows = [all_silent.Active(1, 60, last), all_silent.Active(2, 300, last)]

    assert all_silent.silence_since(rows, None, last + timedelta(seconds=61)) is None
    assert all_silent.silence_since(rows, None, last + timedelta(seconds=301)) == last


def test_silence_needs_two_locations_and_a_known_last_heartbeat() -> None:
    long_ago = _at(9, 0)
    now = _at(12, 0)

    assert all_silent.silence_since([], None, now) is None
    assert all_silent.silence_since([all_silent.Active(1, 60, long_ago)], None, now) is None
    # A location with no heartbeat and no lapse on record cannot be measured: not silent.
    unknown = [all_silent.Active(1, 60, long_ago), all_silent.Active(2, 60, None)]
    assert all_silent.silence_since(unknown, None, now) is None
    assert all_silent.silence_since(unknown, long_ago, now) == long_ago


def test_first_back_is_the_earliest_heartbeat_after_the_start() -> None:
    since = _at(11, 10)
    rows = [
        all_silent.Active(5, 60, _at(11, 13, 30)),
        all_silent.Active(3, 60, _at(11, 13)),
        all_silent.Active(2, 60, _at(11, 13)),
        all_silent.Active(1, 60, since),  # exactly at the start: not back
        all_silent.Active(4, 60, None),
    ]

    # Earliest heartbeat after the start; the lowest id on a tie, whatever the row order.
    assert all_silent.first_back(rows, since) == all_silent.Active(2, 60, _at(11, 13))
    assert all_silent.first_back(rows[3:], since) is None
    assert all_silent.first_back([], since) is None


def test_D04_first_back_counts_a_maintenance_heartbeat_only_after_the_open() -> None:
    since, opened = _at(11, 0), _at(11, 2)
    early_m = all_silent.Active(1, 60, _at(11, 1, 30), maintenance=True)
    at_open_m = all_silent.Active(2, 60, opened, maintenance=True)
    late_m = all_silent.Active(3, 60, _at(11, 3), maintenance=True)
    late_a = all_silent.Active(4, 60, _at(11, 3))
    early_a = all_silent.Active(5, 60, _at(11, 0, 30))

    # A heartbeat from a location in maintenance counts only strictly after the open.
    assert all_silent.first_back([early_m, at_open_m], since, opened) is None
    assert all_silent.first_back([early_m, at_open_m, late_m], since, opened) == late_m
    # A location active since before the start (on, out of maintenance, no window started
    # after the start) still counts from the start, even before the open: its heartbeat
    # breaks the silence itself. It can only have one there that the opening evaluation
    # did not see (committed just after that read); a location that was outside the
    # silence at the open is judged by the next test.
    assert all_silent.first_back([early_m, late_m, early_a], since, opened) == early_a
    # The same instant from both kinds: the lowest id.
    assert all_silent.first_back([late_a, late_m], since, opened) == late_m
    # Without an open time no maintenance heartbeat counts; the active rule is unchanged.
    assert all_silent.first_back([early_m, late_m], since, None) is None
    assert all_silent.first_back([early_m, late_m, late_a], since) == late_a


def test_D04_first_back_counts_a_location_out_of_maintenance_only_after_the_open() -> None:
    """Left maintenance after the start: its beat before the open ends nothing (wave-2 audit).

    The flag is read when the check runs, not when the heartbeat came in. A location that
    left maintenance on after the start has its window started at the exit (D-02); one
    that left it off gets no window, but an off location's last heartbeat came before its
    outage. Neither was part of the silence when the incident was opened, so both count
    only after the open, like a location still in maintenance.
    """
    since, opened = _at(11, 0), _at(11, 2)
    # Left maintenance on at 11:05; its last beat (11:01:30) came while it was in it.
    exited_on = all_silent.Active(1, 60, _at(11, 1, 30), window_start_at=_at(11, 5))
    # Beat at 11:00:10 while active, went off, entered maintenance and left it off.
    exited_off = all_silent.Active(2, 60, _at(11, 0, 10), off=True)
    # A window that started no later than the start: active throughout, the Phase 2 rule.
    old_window = all_silent.Active(3, 60, _at(11, 0, 30), window_start_at=since)

    assert all_silent.first_back([exited_on, exited_off], since, opened) is None
    # Strictly after the open, as for a location in maintenance.
    at_open = dataclasses.replace(exited_on, last_heartbeat_at=opened)
    assert all_silent.first_back([at_open, exited_off], since, opened) is None
    fresh = dataclasses.replace(exited_on, last_heartbeat_at=_at(11, 6))
    assert all_silent.first_back([fresh, exited_off], since, opened) == fresh
    assert all_silent.first_back([exited_on, exited_off, old_window], since, opened) == old_window
    # Without an open time neither of them counts; the active rule is unchanged.
    assert all_silent.first_back([fresh, exited_off], since, None) is None
    assert all_silent.first_back([fresh, old_window], since) == old_window


# Which locations count, and who is named first back (integration)


@pytest.mark.django_db(transaction=True)
def test_all_silent_counts_only_active_locations(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    location_factory(name="W")  # waiting: never sent a heartbeat
    in_maintenance = _silent(location_factory, "M", _at(11, 0))
    Location.objects.filter(pk=in_maintenance.pk).update(maintenance=True)
    deleted = _silent(location_factory, "D", _at(11, 0))
    Location.objects.filter(pk=deleted.pk).update(deleted_at=_at(11, 1))

    # Only A is active: all-silent needs at least two.
    assert all_silent.evaluate(_at(11, 10)) is None
    assert (_ops_rows(), _incidents()) == ([], [])

    _silent(location_factory, "B", _at(11, 0))
    assert all_silent.evaluate(_at(11, 10)) == "started"

    [start] = _starts()
    assert start.payload == {"since_us": ops.instant_us(_at(11, 0)), "count": 2}
    assert _incidents() == [(None, _at(11, 0), None)]


@pytest.mark.django_db(transaction=True)
def test_all_silent_end_tie_breaks_by_lowest_id(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    b = _silent(location_factory, "B", _at(11, 0))
    assert all_silent.evaluate(_at(11, 2)) == "started"

    # Both back at the same instant; B's heartbeat is even recorded first.
    transitions.record_heartbeat(b.pk, _at(11, 3))
    transitions.record_heartbeat(a.pk, _at(11, 3))
    assert all_silent.evaluate(_at(11, 3, 5)) == "ended"

    [end] = _ends()
    assert a.pk < b.pk
    assert end.location_id == a.pk
    assert _render(end) == "✅ Heartbeats are back (first: A, 14:03:00); all-silent lasted 3m."


# D-04 (Phase 4, refined after the wave-1 audit): maintenance and deletion. The start still
# counts only active locations. The end is the first heartbeat after the start from an
# active location (the Phase 2 rule), or the first heartbeat received after the incident
# was opened (detected) from a location in maintenance (monitored, not deleted). The start
# is backdated to the moment the last active location fell quiet, so a maintenance beat
# before the detection proves nothing about the server and network path now. Putting
# locations into maintenance or deleting them never closes the incident by itself.


def _started_with_a_and_b(location_factory: Callable[..., Any]) -> tuple[Any, Any]:
    """A and B active, last heartbeats at 11:00; all-silent started at the 11:02 check."""
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    b = _silent(location_factory, "B", _at(11, 0))
    assert all_silent.evaluate(_at(11, 2)) == "started"
    return a, b


@pytest.mark.django_db(transaction=True)
def test_D04_a_heartbeat_from_a_location_in_maintenance_ends_all_silent(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """C is in maintenance: it never counts for the start, but its heartbeat ends it.

    D-04: a heartbeat from a location in maintenance received after the incident was opened
    (11:02) proves the server and network path work, so it ends the incident.
    """
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    c = _silent(location_factory, "C", _at(11, 0), maintenance=True)
    assert all_silent.evaluate(_at(11, 2)) == "started"
    [start] = _starts()
    assert start.payload == {"since_us": ops.instant_us(_at(11, 0)), "count": 2}

    assert transitions.record_heartbeat(c.pk, _at(11, 3)) == "plain"
    assert all_silent.evaluate(_at(11, 3, 5)) == "ended"

    [end] = _ends()
    assert end.location_id == c.pk != a.pk
    assert end.payload == {
        "since_us": ops.instant_us(_at(11, 0)),
        "first_us": ops.instant_us(_at(11, 3)),
    }
    assert _render(end) == "✅ Heartbeats are back (first: C, 14:03:00); all-silent lasted 3m."
    assert _incidents() == [(None, _at(11, 0), _at(11, 3))]
    assert all_silent.evaluate(_at(11, 3, 10)) is None
    assert len(_ends()) == 1


@pytest.mark.django_db(transaction=True)
def test_D04_maintenance_heartbeat_before_detection_does_not_end_all_silent(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """A maintenance beat between the backdated start and the detection ends nothing.

    The INV-12 #1 shape: A and B fell quiet at 11:00, so all-silent is detected at 11:02 and
    starts at 11:00. C's device (in maintenance) beat at 11:01:30, after the start but
    before the detection, and then the ingress outage silenced it too. Counting that beat
    closed the incident at the next evaluation with a false "Heartbeats are back (first: C)"
    while the outage went on (the literal D-04 reading, replaced by the maintainer's
    refinement).
    """
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    c = _silent(location_factory, "C", _at(11, 0), maintenance=True)
    assert transitions.record_heartbeat(c.pk, _at(11, 1, 30)) == "plain"

    assert all_silent.evaluate(_at(11, 2)) == "started"
    for at in (_at(11, 2, 5), _at(11, 3), _at(11, 30)):
        assert all_silent.evaluate(at) is None

    assert _incidents() == [(None, _at(11, 0), None)]
    assert (len(_starts()), _ends()) == (1, [])
    # The detection time is stored with the incident (integers only, OPS-08).
    assert OpsIncident.objects.get().details == {"opened_us": ops.instant_us(_at(11, 2))}


@pytest.mark.django_db(transaction=True)
def test_D04_maintenance_heartbeat_after_detection_ends_all_silent(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """Pitfall 7, refined: a beating device in maintenance ends it at its first beat after the open.

    C's 11:01 beat predates the 11:02 detection and is ignored. Its 11:03 beat is fresh
    proof that the server and network path work, so it ends the incident at 11:03 with one
    end notice naming C. The admin gets the start and the end notice about one beat apart.
    """
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    c = _silent(location_factory, "C", _at(11, 0), maintenance=True)
    assert transitions.record_heartbeat(c.pk, _at(11, 1)) == "plain"
    assert all_silent.evaluate(_at(11, 2)) == "started"
    assert all_silent.evaluate(_at(11, 2, 5)) is None

    assert transitions.record_heartbeat(c.pk, _at(11, 3)) == "plain"
    assert all_silent.evaluate(_at(11, 3, 5)) == "ended"
    assert all_silent.evaluate(_at(11, 3, 10)) is None

    [end] = _ends()
    assert end.location_id == c.pk != a.pk
    assert end.payload == {
        "since_us": ops.instant_us(_at(11, 0)),
        "first_us": ops.instant_us(_at(11, 3)),
    }
    assert _render(end) == "✅ Heartbeats are back (first: C, 14:03:00); all-silent lasted 3m."
    assert _incidents() == [(None, _at(11, 0), _at(11, 3))]
    assert [row.kind for row in _ops_rows()] == [
        outbox.KIND_OPS_ALL_SILENT_START,
        outbox.KIND_OPS_ALL_SILENT_END,
    ]


@pytest.mark.django_db(transaction=True)
def test_D04_a_location_out_of_maintenance_while_off_never_ends_all_silent_with_an_old_beat(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """The wave-2 gap when M leaves maintenance while it is off (D-02 starts no window then).

    A and B (period 60 s) fell quiet at 11:00. M (period 10 s, timeout 20 s) beat at
    11:00:10, after that start, and then lost power: its OFF is decided at 11:00:35, and
    the admin puts it into maintenance at 11:00:40, so the 11:01:05 detection leaves it
    out. At 11:05 the admin turns maintenance off with M still off: its off piece reopens
    with the same outage start and no fresh window (INV-11). M's 11:00:10 beat came before
    its outage and before the open, so it ends nothing (it ended the incident before the
    open, "lasted 10s", and opened a second one for the same silence). A's heartbeat at
    11:08 ends the incident.
    """
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    m = _silent(location_factory, "M", _at(10, 59), period_s=10, grace_s=10)
    assert transitions.record_heartbeat(m.pk, _at(11, 0, 10)) == "plain"
    detection.run_cycle(_at(11, 0, 35))
    assert LocationState.objects.get(pk=m.pk).status == "off"
    assert maintenance.set_maintenance(m.pk, True, _at(11, 0, 40)) is True
    assert all_silent.evaluate(_at(11, 1, 5)) == "started"

    assert maintenance.set_maintenance(m.pk, False, _at(11, 5)) is True
    state = LocationState.objects.get(pk=m.pk)
    assert (state.status, state.window_start_at) == ("off", None)
    for at in _steps(_at(11, 5), _at(11, 7, 55), timedelta(seconds=5)):
        assert all_silent.evaluate(at) is None
    assert (len(_starts()), _ends()) == (1, [])

    assert transitions.record_heartbeat(a.pk, _at(11, 8)) == "plain"
    assert all_silent.evaluate(_at(11, 8)) == "ended"
    [end] = _ends()
    assert end.location_id == a.pk
    assert _render(end) == "✅ Heartbeats are back (first: A, 14:08:00); all-silent lasted 8m."
    assert _incidents() == [(None, _at(11, 0), _at(11, 8))]
    assert len(_starts()) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "details",
    [{}, {"opened_us": "14:02"}, {"opened_us": True}, {"opened_us": 10**30}],
    ids=["missing", "not-an-integer", "boolean", "out-of-range"],
)
def test_D04_incident_without_an_open_time_counts_maintenance_beats_from_its_first_evaluation(
    location_factory: Callable[..., Any], ops_settings: Any, details: dict[str, Any]
) -> None:
    """The fallback for an incident opened before the open time was stored (or a bad one).

    Its real open time is unknown: it lies after the start and before the first evaluation
    that sees the incident. That evaluation stores its own time as the open time, so a
    maintenance beat before it (it may predate the detection) is ignored and a later one
    ends the incident. The stand-in is never earlier than the real open, so it can only
    ignore more beats (never a false end), and a beating device in maintenance still ends
    the incident (it never stays open for good).
    """
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    c = _silent(location_factory, "C", _at(11, 0), maintenance=True)
    legacy = OpsIncident.objects.create(
        kind=all_silent.KIND_ALL_SILENT, started_at=_at(11, 0), details=details
    )
    assert transitions.record_heartbeat(c.pk, _at(11, 4)) == "plain"

    # The first evaluation that sees it (11:05) stands in for its open time; later ones
    # keep that time.
    assert all_silent.evaluate(_at(11, 5)) is None
    assert all_silent.evaluate(_at(11, 6)) is None
    legacy.refresh_from_db()
    assert (legacy.ended_at, legacy.details) == (None, {"opened_us": ops.instant_us(_at(11, 5))})

    # C's next beat ends it, once, with the incident's real start.
    assert transitions.record_heartbeat(c.pk, _at(11, 7)) == "plain"
    assert all_silent.evaluate(_at(11, 7, 5)) == "ended"
    assert all_silent.evaluate(_at(11, 7, 10)) is None
    [end] = _ends()
    assert (end.location_id, end.payload) == (
        c.pk,
        {"since_us": ops.instant_us(_at(11, 0)), "first_us": ops.instant_us(_at(11, 7))},
    )
    assert _incidents() == [(None, _at(11, 0), _at(11, 7))]
    assert _starts() == []


@pytest.mark.django_db(transaction=True)
def test_D04_incident_without_an_open_time_still_ends_at_an_active_heartbeat(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """The open-time stand-in never applies to an active location (Phase 2 rule unchanged)."""
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    OpsIncident.objects.create(kind=all_silent.KIND_ALL_SILENT, started_at=_at(11, 0))
    assert transitions.record_heartbeat(a.pk, _at(11, 4)) == "plain"

    # A's heartbeat after the start ends it, though it came before the first evaluation.
    assert all_silent.evaluate(_at(11, 5)) == "ended"

    [end] = _ends()
    assert (end.location_id, end.payload["first_us"]) == (a.pk, ops.instant_us(_at(11, 4)))
    assert _incidents() == [(None, _at(11, 0), _at(11, 4))]


@pytest.mark.django_db(transaction=True)
def test_D04_a_silence_ended_by_a_heartbeat_never_starts_again(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """One incident per silence: Pitfall 7 costs one start and one end notice, not a flood.

    After C (in maintenance) ends the incident, A and B are still silent, so the start rule
    holds again at once, with the same start. Reopening it there would let C's stored
    heartbeat end it on the next evaluation, then reopen it, and so on: a start and an end
    notice every detection cycle for as long as A and B stay silent. A silence that was
    already reported (it starts no later than the last incident) never opens another one;
    a new silence (an active location beat and fell quiet again) does.
    """
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    b = _silent(location_factory, "B", _at(11, 0))
    c = _silent(location_factory, "C", _at(11, 0), maintenance=True)
    assert all_silent.evaluate(_at(11, 2)) == "started"

    # C's device keeps beating every 60 s, just before that step's check; the check runs
    # every 5 s for 10 minutes.
    outcomes: dict[datetime, str] = {}
    for at in _steps(_at(11, 2, 5), _at(11, 12), timedelta(seconds=5)):
        if at.second == 30:
            assert transitions.record_heartbeat(c.pk, at) == "plain"
        result = all_silent.evaluate(at)
        if result is not None:
            outcomes[at] = result

    assert outcomes == {_at(11, 2, 30): "ended"}
    assert _incidents() == [(None, _at(11, 0), _at(11, 2, 30))]
    assert (len(_starts()), len(_ends())) == (1, 1)

    # A new silence is a new incident: A and B beat, then fall quiet again.
    assert transitions.record_heartbeat(a.pk, _at(11, 12)) == "plain"
    assert transitions.record_heartbeat(b.pk, _at(11, 12, 10)) == "plain"
    assert all_silent.evaluate(_at(11, 13, 10)) is None  # B quiet for exactly its period
    assert all_silent.evaluate(_at(11, 13, 11)) == "started"
    assert transitions.record_heartbeat(c.pk, _at(11, 13, 30)) == "plain"
    assert all_silent.evaluate(_at(11, 13, 35)) == "ended"

    assert _incidents() == [
        (None, _at(11, 0), _at(11, 2, 30)),
        (None, _at(11, 12, 10), _at(11, 13, 30)),
    ]
    assert [row.payload["count"] for row in _starts()] == [2, 2]
    assert [row.location_id for row in _ends()] == [c.pk, c.pk]


@pytest.mark.django_db(transaction=True)
def test_D04_maintenance_alone_never_closes_all_silent(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    a, b = _started_with_a_and_b(location_factory)

    # The admin puts both silent locations into maintenance: no heartbeat, no end.
    Location.objects.filter(pk__in=[a.pk, b.pk]).update(maintenance=True)
    assert all_silent.evaluate(_at(11, 3)) is None
    assert all_silent.evaluate(_at(11, 30)) is None
    assert _incidents() == [(None, _at(11, 0), None)]
    assert _ends() == []

    # Only a heartbeat ends it, and one from a location in maintenance does (D-04).
    assert transitions.record_heartbeat(b.pk, _at(11, 31)) == "plain"
    assert all_silent.evaluate(_at(11, 31, 5)) == "ended"
    [end] = _ends()
    assert (end.location_id, end.payload["first_us"]) == (b.pk, ops.instant_us(_at(11, 31)))


@pytest.mark.django_db(transaction=True)
def test_D04_deletion_alone_never_closes_all_silent(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    a, b = _started_with_a_and_b(location_factory)
    # A beats after the start, then the admin deletes it before the next check.
    assert transitions.record_heartbeat(a.pk, _at(11, 2, 30)) == "plain"
    Location.objects.filter(pk=a.pk).update(deleted_at=_at(11, 2, 40))

    # A deleted location's heartbeat never ends it, and the delete closes nothing by
    # itself, even though only one active location is left.
    assert all_silent.evaluate(_at(11, 3)) is None
    assert all_silent.evaluate(_at(11, 30)) is None
    assert _incidents() == [(None, _at(11, 0), None)]
    assert _ends() == []

    assert transitions.record_heartbeat(b.pk, _at(11, 31)) == "plain"
    assert all_silent.evaluate(_at(11, 31, 5)) == "ended"
    [end] = _ends()
    assert end.location_id == b.pk


@pytest.mark.django_db(transaction=True)
def test_D04_waiting_location_never_ends_all_silent(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _started_with_a_and_b(location_factory)
    # A waiting location is not monitored yet: even with a heartbeat time on its row (none
    # is ever written while it waits), it is not an end candidate.
    waiting = location_factory(name="W")
    LocationState.objects.filter(pk=waiting.pk).update(last_heartbeat_at=_at(11, 3))

    assert all_silent.evaluate(_at(11, 3, 5)) is None
    assert _incidents() == [(None, _at(11, 0), None)]
    assert _ends() == []


@pytest.mark.django_db(transaction=True)
def test_D04_start_rule_unchanged(location_factory: Callable[..., Any], ops_settings: Any) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    m = _silent(location_factory, "M", _at(11, 0), maintenance=True)
    # One active silent location and one in maintenance: maintenance never counts.
    assert all_silent.evaluate(_at(11, 2)) is None

    _silent(location_factory, "B", _at(11, 0))
    assert all_silent.evaluate(_at(11, 2)) == "started"

    [start] = _starts()
    assert start.payload == {"since_us": ops.instant_us(_at(11, 0)), "count": 2}
    assert _render(start).startswith("⚠️ All 2 active locations silent since 14:00")
    assert LocationState.objects.get(pk=m.pk).status == "on"


# Exactly one notice each way, enforced by the database (D-11)


@pytest.mark.django_db
def test_open_and_close_incident_are_exactly_once(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    first = ops.open_incident(all_silent.KIND_ALL_SILENT, _at(11, 0))
    assert isinstance(first, int)
    # One open incident per kind and location; a NULL location counts as one value.
    assert ops.open_incident(all_silent.KIND_ALL_SILENT, _at(11, 1)) is None
    other = ops.open_incident(all_silent.KIND_ALL_SILENT, _at(11, 1), location_id=location.pk)
    assert isinstance(other, int)
    assert other != first

    assert ops.close_incident(first, _at(11, 3)) is True
    assert ops.close_incident(first, _at(11, 4)) is False
    assert OpsIncident.objects.get(pk=first).ended_at == _at(11, 3)
    assert ops.close_incident(max(first, other) + 1000, _at(11, 5)) is False  # no such row

    # Once closed, the next incident can open.
    again = ops.open_incident(all_silent.KIND_ALL_SILENT, _at(11, 6))
    assert again is not None
    assert again not in (first, other)


@pytest.mark.django_db(transaction=True)
def test_all_silent_opens_at_most_once(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    now = _at(11, 2)

    # Both evaluations see every location silent and no open incident before either
    # opens one: they meet inside ops.open_incident.
    undo = _together(monkeypatch, "open_incident")
    results = _evaluate_twice_at_once(now)
    undo()

    assert sorted(results, key=str) == [None, "started"]
    # A later evaluation in the same silent state opens nothing either.
    assert all_silent.evaluate(now + timedelta(seconds=5)) is None
    assert all_silent.evaluate(now + timedelta(minutes=5)) is None
    assert _incidents() == [(None, _at(11, 0), None)]
    assert len(_starts()) == 1
    [(incident_id,)] = OpsIncident.objects.values_list("id")
    assert ops.close_incident(incident_id, _at(11, 8)) is True
    assert ops.close_incident(incident_id, _at(11, 9)) is False


@pytest.mark.django_db(transaction=True)
def test_all_silent_closes_at_most_once(
    location_factory: Callable[..., Any], ops_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    a = _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    assert all_silent.evaluate(_at(11, 2)) == "started"
    transitions.record_heartbeat(a.pk, _at(11, 3))

    # Both evaluations see the incident open and A back; they meet before closing it.
    undo = _together(monkeypatch, "close_incident")
    results = _evaluate_twice_at_once(_at(11, 3, 5))
    undo()

    assert sorted(results, key=str) == [None, "ended"]
    assert len(_ends()) == 1
    assert _incidents() == [(None, _at(11, 0), _at(11, 3))]
    assert all_silent.evaluate(_at(11, 3, 10)) is None


# A lapse never ends it; a restart never starts it twice


@pytest.mark.django_db(transaction=True)
def test_all_silent_is_not_ended_by_a_lapse_carve(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=_at(11, 2), resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    b = _silent(location_factory, "B", _at(11, 0, 20))
    assert all_silent.evaluate(_at(11, 2)) == "started"

    # A lapse ends now: nobody counts as silent any more, yet nobody sent a heartbeat.
    assert lapse.carve_if_needed(_at(11, 5), force=True) == lapse.Gap(_at(11, 2), _at(11, 5))
    assert SystemState.objects.get(pk=1).detection_resumed_at == _at(11, 5)
    assert all_silent.evaluate(_at(11, 5)) is None
    assert all_silent.evaluate(_at(11, 7)) is None
    assert _incidents() == [(None, _at(11, 0, 20), None)]
    assert _ends() == []

    # Only a heartbeat ends it.
    transitions.record_heartbeat(b.pk, _at(11, 8))
    assert all_silent.evaluate(_at(11, 8, 5)) == "ended"
    [end] = _ends()
    assert end.location_id == b.pk
    assert end.payload["first_us"] == ops.instant_us(_at(11, 8))
    assert _incidents() == [(None, _at(11, 0, 20), _at(11, 8))]


@pytest.mark.django_db(transaction=True)
def test_all_silent_survives_a_restart_without_a_second_start(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=_at(11, 2), resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    assert all_silent.evaluate(_at(11, 2)) == "started"

    # A new worker process 3 min later: a fresh tracker and lease generation 1. Its first
    # cycle carves the gap, and the locations then stay silent for 3 more minutes.
    clock = FakeClock(_at(11, 5))
    tracker = lapse.CycleTracker()
    detection.run_detection(clock, 1, tracker)
    for _ in range(36):
        clock.advance(seconds=5)
        detection.run_detection(clock, 1, tracker)

    assert len(_starts()) == 1
    assert _incidents() == [(None, _at(11, 0), None)]
    assert OpsIncident.objects.filter(kind=lapse.KIND_MONITORING_GAP).count() == 1


# The notices without an ops chat (D-09)


@pytest.mark.django_db(transaction=True)
def test_all_silent_without_an_ops_chat_logs_both_notices(
    location_factory: Callable[..., Any], no_ops_chat: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    b = _silent(location_factory, "B <office>", _at(11, 0))
    caplog.set_level(logging.WARNING, logger=OPS_LOGGER)

    assert all_silent.evaluate(_at(11, 2)) == "started"
    transitions.record_heartbeat(b.pk, _at(11, 3))
    assert all_silent.evaluate(_at(11, 3, 5)) == "ended"

    lines = [r.getMessage() for r in caplog.records if r.name == OPS_LOGGER]
    assert lines == [
        "ops notice (ops chat not configured): ⚠️ All 2 active locations silent since 14:00: "
        "an area power/ISP outage or a server/network problem. "
        "Subscriber alerts continue as normal.",
        "ops notice (ops chat not configured): ✅ Heartbeats are back "
        "(first: B <office>, 14:03:00); all-silent lasted 3m.",
    ]
    assert _ops_rows() == []
    assert _incidents() == [(None, _at(11, 0), _at(11, 3))]


# The wiring in the detection cycle


@pytest.mark.django_db(transaction=True)
def test_all_silent_runs_inside_run_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _system(cursor=None, resumed=None)
    calls: list[str] = []
    real_cycle, real_evaluate = detection.run_cycle, all_silent.evaluate

    def decisions(now: datetime, tick: Callable[[], None] | None = None) -> int:
        calls.append("decisions")
        return real_cycle(now, tick=tick)

    def evaluate(now: datetime) -> str | None:
        calls.append("all-silent")
        return real_evaluate(now)

    monkeypatch.setattr(detection, "run_cycle", decisions)
    monkeypatch.setattr(all_silent, "evaluate", evaluate)
    clock = FakeClock(_at(11, 0))
    tracker = lapse.CycleTracker()

    detection.run_detection(clock, 1, tracker)
    clock.advance(seconds=5)
    detection.run_detection(clock, 1, tracker)
    assert calls == ["decisions", "all-silent"] * 2

    # A cycle skipped for a clock step (either way) runs neither.
    clock.set(clock.now() + timedelta(seconds=60))
    detection.run_detection(clock, 1, tracker)
    clock.set(clock.now() - timedelta(seconds=120))
    detection.run_detection(clock, 1, tracker)
    assert calls == ["decisions", "all-silent"] * 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("a bug in the all-silent check"),
        OperationalError("canceling statement due to statement timeout"),
    ],
    ids=["bug", "statement-timeout"],
)
def test_all_silent_error_never_stops_detection(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    # The tracker has seen generation 1 and the cursor is 5 s old: no carve, so the
    # location's OFF is decided in this cycle.
    _system(cursor=_at(11, 4, 55), resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))

    def failing(now: datetime) -> str | None:
        raise error

    monkeypatch.setattr(all_silent, "evaluate", failing)
    caplog.set_level(logging.ERROR, logger=detection.__name__)

    recorded = detection.run_detection(FakeClock(_at(11, 5)), 1, lapse.CycleTracker(generation=1))

    assert recorded == 1
    assert _subscriber_alerts() == [("power_off", "A", _at(11, 0))]
    [record] = [r for r in caplog.records if r.name == detection.__name__]
    assert record.getMessage() == "all-silent evaluation failed"
    assert record.exc_info is not None


@pytest.mark.django_db(transaction=True)
def test_all_silent_error_on_a_lost_connection_reaches_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The detection loop logs one WARNING per outage and forces the next carve; it must see
    # the error (D-16).
    _system(cursor=_at(11, 4, 55), resumed=_at(9, 0))

    def lost(now: datetime) -> str | None:
        connection.close()
        raise InterfaceError("connection already closed")

    monkeypatch.setattr(all_silent, "evaluate", lost)

    with pytest.raises(InterfaceError):
        detection.run_detection(FakeClock(_at(11, 5)), 1, lapse.CycleTracker(generation=1))
