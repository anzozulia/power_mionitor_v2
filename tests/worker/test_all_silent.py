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
- D-04: the start still counts only active locations, but the end is the first heartbeat
  after the start from any monitored, non-deleted location, in maintenance or not; putting
  locations into maintenance or deleting them never ends it by itself (Pitfall 7: a
  location in maintenance whose device keeps beating ends it at the next evaluation);
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
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import Actor, FakeClock, terminate_backends
from django.db import InterfaceError, OperationalError, connection

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import all_silent, lapse, transitions
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


def _run_inv12(locations: dict[str, Any], *, evaluate: bool) -> dict[datetime, str]:
    """A cycle (and the all-silent check) every 5 s from 11:10:05 to 11:14:00 UTC.

    A heartbeat at a step's time arrives just after that step's cycle. Returns the
    non-None results of the check by step.
    """
    outcomes: dict[datetime, str] = {}
    for at in _steps(_at(11, 10, 5), _at(11, 14), timedelta(seconds=5)):
        detection.run_cycle(at)
        if evaluate:
            result = all_silent.evaluate(at)
            if result is not None:
                outcomes[at] = result
        if at in RESUMES:
            assert transitions.record_heartbeat(locations[RESUMES[at]].pk, at) == "restored"
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_INV12_all_silent_one_alert_one_recovery(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    locations = _inv12_locations(location_factory)

    outcomes = _run_inv12(locations, evaluate=True)

    # The start at the first cycle after 14:11:00 (C silent for more than 60 s), the end
    # at the first cycle after A's heartbeat.
    assert outcomes == {_at(11, 11, 5): "started", _at(11, 13, 5): "ended"}
    [start] = _starts()
    assert (start.recorded_at, start.location_id) == (_at(11, 11, 5), None)
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


# The silence rule itself (pure)


def test_all_silent_boundary_exactly_one_period() -> None:
    last = _at(11, 10)
    rows = [all_silent.Active(1, 60, last), all_silent.Active(2, 60, last - timedelta(seconds=5))]

    # Exactly one period after the last heartbeat a location is not silent yet (strict).
    assert all_silent.silence_since(rows, None, last + timedelta(seconds=60)) is None
    assert all_silent.silence_since(rows, None, last + timedelta(seconds=60, microseconds=1)) == (
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


def test_each_location_is_silent_after_its_own_period() -> None:
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


# Which locations count, and who is named first back (integration)


@pytest.mark.django_db(transaction=True)
def test_all_silent_counts_only_active_locations(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    location_factory(name="W")  # waiting: never sent a heartbeat
    maintenance = _silent(location_factory, "M", _at(11, 0))
    Location.objects.filter(pk=maintenance.pk).update(maintenance=True)
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


# D-04 (Phase 4): maintenance and deletion. The start still counts only active locations;
# the end is the first heartbeat after the start from any monitored, non-deleted location,
# in maintenance or not, because a heartbeat proves the server and network path work.
# Putting locations into maintenance or deleting them never closes the incident by itself.


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

    Pitfall 7, D-04 taken literally: a heartbeat from a location in maintenance proves the
    server and network path work, so it ends the incident like any other heartbeat.
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
def test_D04_pitfall7_a_beating_location_in_maintenance_ends_all_silent_at_once(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    """Pitfall 7: the admin gets a start and an end notice one evaluation apart.

    D-04 is applied literally (a locked decision, T-04-14 accepted): C is in maintenance
    and its device keeps beating, so its last heartbeat already lies after the start when
    the incident opens, and the next evaluation ends it at that heartbeat.
    """
    _system(cursor=None, resumed=_at(9, 0))
    _silent(location_factory, "A", _at(11, 0))
    _silent(location_factory, "B", _at(11, 0))
    c = _silent(location_factory, "C", _at(11, 0), maintenance=True)
    assert transitions.record_heartbeat(c.pk, _at(11, 1, 30)) == "plain"

    assert all_silent.evaluate(_at(11, 2)) == "started"
    assert all_silent.evaluate(_at(11, 2, 5)) == "ended"

    [end] = _ends()
    assert end.location_id == c.pk
    assert end.payload["first_us"] == ops.instant_us(_at(11, 1, 30))
    assert _incidents() == [(None, _at(11, 0), _at(11, 1, 30))]
    assert [row.kind for row in _ops_rows()] == [
        outbox.KIND_OPS_ALL_SILENT_START,
        outbox.KIND_OPS_ALL_SILENT_END,
    ]


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
    b = _silent(location_factory, "B", _at(11, 0, 30))
    assert all_silent.evaluate(_at(11, 2)) == "started"

    # A lapse ends now: nobody counts as silent any more, yet nobody sent a heartbeat.
    assert lapse.carve_if_needed(_at(11, 5), force=True) == lapse.Gap(_at(11, 2), _at(11, 5))
    assert SystemState.objects.get(pk=1).detection_resumed_at == _at(11, 5)
    assert all_silent.evaluate(_at(11, 5)) is None
    assert all_silent.evaluate(_at(11, 7)) is None
    assert _incidents() == [(None, _at(11, 0, 30), None)]
    assert _ends() == []

    # Only a heartbeat ends it.
    transitions.record_heartbeat(b.pk, _at(11, 8))
    assert all_silent.evaluate(_at(11, 8, 5)) == "ended"
    [end] = _ends()
    assert end.location_id == b.pk
    assert end.payload["first_us"] == ops.instant_us(_at(11, 8))
    assert _incidents() == [(None, _at(11, 0, 30), _at(11, 8))]


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
