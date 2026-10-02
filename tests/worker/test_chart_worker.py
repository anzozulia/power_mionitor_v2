"""The production worker draws the chart, and never draws a downtime as on (INV-10 #1, DoD 2).

``manage.py run_worker`` runs the chart lifecycle in its Telegram I/O thread
(``run_worker.serve(..., charts=True)``), behind the first-cycle gate
(``supervision.CarveGate``): after a worker start or any new lease generation, chart work
waits until that generation's detection cycle, whose lapse carve a new generation always
forces, has completed. So the first chart after a restart already shows the downtime as
not monitored, never as on, not even for one refresh (INV-10 #1 chart part, DoD 2 chart
part, 03-CONTEXT "first-cycle gate").

- ``test_INV10_1_downtime_is_never_drawn_as_on`` runs the real ``serve`` after a 10-min
  stack downtime and records every week the worker builds for a chart
  (``source.load_week`` wrapped as the module attribute the lifecycle calls). The
  restart's carve (``lapse.carve_window``) is held until the I/O thread has made a few
  passes for the new generation, so a worker without the gate would surely draw its
  chart before the carve, and the test would see the downtime drawn as on.
- The gate itself: ready only for the marked generation, never for generation 0; the
  I/O thread makes no chart call while its generation is not marked.

Time: the worker gets a ``FakeClock`` that never moves (each wait is its full interval),
so no location times out; real ``time.monotonic()`` only bounds the waits. The watchdog
checks only every 30 s, so it never runs during a test, and every thread is stopped and
joined in ``finally``. Renders are real (Pillow); Telegram is faked at the HTTP boundary.
The few chart helpers are copied from tests/chart/chart_fixtures.py instead of imported:
tests have no ``__init__.py``, so tests/chart is on ``sys.path`` only once one of its
modules was collected, and this file must also run on its own.
"""

import dataclasses
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import DEFAULT_BOT_TOKEN, FakeClock, wait_for
from django.db import connection
from django.db.models import F

from powermon.alerts.models import OpsIncident
from powermon.chart import model, source
from powermon.engine import lapse
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.worker import io_loop, supervision
from powermon.worker.lease import Lease
from powermon.worker.management.commands import run_worker

_KYIV = "Europe/Kyiv"
# Worker intervals: detection every 0.1 s; the watchdog never checks during a test.
INTERVALS = {"detection_interval": 0.1, "io_idle_wait": 0.05, "check_interval": 30.0}
HOUR_US = 3_600_000_000
MINUTE_US = 60_000_000


def _kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-01 12:00"`` (fold 0) as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(_KYIV)).astimezone(UTC)


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


# Thu 2026-10-01 12:00 local; the stack was down for the 10 min before it.
T = _kyiv("2026-10-01 12:00")
DOWN_AT = T - timedelta(minutes=10)
GAP_START_US = 11 * HOUR_US + 50 * MINUTE_US  # 11:50 wall time
GAP_END_US = 12 * HOUR_US  # 12:00 wall time


class _Serve:
    """``run_worker.serve`` in a thread with a FakeClock, an injected stall action, charts on."""

    def __init__(self, lease: Lease, clock: FakeClock, health_path: Path) -> None:
        self.stop = threading.Event()
        self.code: int | None = None
        self.stalls: list[str] = []
        health = supervision.HealthFile(health_path)

        def target() -> None:
            self.code = run_worker.serve(
                self.stop,
                clock,
                lease,
                health=health,
                on_stall=self.stalls.append,
                charts=True,
                **INTERVALS,
            )

        self.thread = threading.Thread(target=target, name="serve-under-test")
        self.thread.start()

    def finish(self) -> int | None:
        self.stop.set()
        # A call in flight ends within its own timeouts; the joins inside serve wait for it.
        self.thread.join(30)
        return self.code


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=_KYIV)
    return settings


@pytest.fixture
def lease() -> Iterator[Lease]:
    made = Lease(connection.settings_dict)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def weeks(monkeypatch: pytest.MonkeyPatch) -> list[model.Week]:
    """Every week ``source.load_week`` returns to the chart (the real one still runs)."""
    seen: list[model.Week] = []
    real = source.load_week

    def recording(*args: Any, **kwargs: Any) -> model.Week:
        week = real(*args, **kwargs)
        seen.append(week)
        return week

    monkeypatch.setattr(source, "load_week", recording)
    return seen


# The gate


def test_carve_gate_ready_only_for_the_marked_generation() -> None:
    gate = supervision.CarveGate()
    # Nothing has completed yet: no generation is ready.
    assert [gate.ready(g) for g in (0, 1, 2)] == [False, False, False]

    gate.mark(1)
    assert gate.ready(1) is True
    assert gate.ready(2) is False

    # A new generation closes the gate for the old one.
    gate.mark(2)
    assert gate.ready(1) is False
    assert gate.ready(2) is True

    # Generation 0 (no lease held) is never ready, even marked.
    gate.mark(0)
    assert gate.ready(0) is False
    assert gate.ready(2) is False


@pytest.mark.django_db(transaction=True)
def test_io_thread_waits_for_the_gate(
    lease: Lease,
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _monitor(location_factory(), _kyiv("2026-10-01 08:00"))
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    passes: list[bool] = []
    real = io_loop.run_iteration

    def counting(*args: Any, **kwargs: Any) -> bool:
        passes.append(bool(kwargs.get("charts", False)))
        return real(*args, **kwargs)

    monkeypatch.setattr(io_loop, "run_iteration", counting)
    clock = FakeClock(T)
    status = lease.ensure_held()
    assert status.state == "held"
    gate = supervision.CarveGate()
    stop = threading.Event()
    progress = supervision.Progress(clock)
    thread = threading.Thread(
        target=run_worker.io_thread,
        args=(stop, clock, 0.01, lease, progress),
        kwargs={"gate": gate, "charts": True},
        name="io-under-test",
    )
    thread.start()
    try:
        # Passes run, but this generation's cycle has not completed: no chart work.
        assert wait_for(lambda: len(passes) >= 5, 10)
        # Another generation's mark does not open the gate for this one.
        gate.mark(status.generation + 1)
        seen = len(passes)
        assert wait_for(lambda: len(passes) >= seen + 5, 10)
        assert not wait_for(lambda: len(fake_telegram.chart_calls) > 0, 0.5)
        assert not any(passes)

        gate.mark(status.generation)
        assert wait_for(lambda: fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") >= 1, 10)
    finally:
        stop.set()
        thread.join(30)

    assert not thread.is_alive()
    assert fake_telegram.chart_calls[0].method == "sendPhoto"
    assert any(passes)


@pytest.mark.django_db(transaction=True)
def test_io_thread_without_a_gate_makes_no_chart_call(
    lease: Lease,
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # charts=True alone is not enough: with no gate there is no first-cycle fact to trust.
    _monitor(location_factory(), _kyiv("2026-10-01 08:00"))
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    passes: list[bool] = []
    real = io_loop.run_iteration

    def counting(*args: Any, **kwargs: Any) -> bool:
        passes.append(bool(kwargs.get("charts", False)))
        return real(*args, **kwargs)

    monkeypatch.setattr(io_loop, "run_iteration", counting)
    clock = FakeClock(T)
    assert lease.ensure_held().state == "held"
    stop = threading.Event()
    thread = threading.Thread(
        target=run_worker.io_thread,
        args=(stop, clock, 0.01, lease, supervision.Progress(clock)),
        kwargs={"charts": True},
        name="io-under-test",
    )
    thread.start()
    try:
        assert wait_for(lambda: len(passes) >= 10, 10)
    finally:
        stop.set()
        thread.join(30)

    assert not thread.is_alive()
    assert not any(passes)
    assert fake_telegram.chart_calls == []


# INV-10 #1 chart part (DoD 2): the first chart after a 10-min stack downtime


@pytest.mark.django_db(transaction=True)
def test_INV10_1_downtime_is_never_drawn_as_on(
    lease: Lease,
    weeks: list[model.Week],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The restart's carve is held until the I/O thread has served the new generation a
    # few times, so a chart drawn before the carve (no gate) would surely be drawn here.
    passes: list[bool] = []
    real_pass = io_loop.run_iteration
    real_carve = lapse.carve_window

    def counting(*args: Any, **kwargs: Any) -> bool:
        passes.append(bool(kwargs.get("charts", False)))
        return real_pass(*args, **kwargs)

    def held_carve(*args: Any, **kwargs: Any) -> int:
        wait_for(lambda: len(passes) >= 3, 5)
        return real_carve(*args, **kwargs)

    monkeypatch.setattr(io_loop, "run_iteration", counting)
    monkeypatch.setattr(lapse, "carve_window", held_carve)
    # The last cycle before the stop and the last heartbeat were 10 min ago (11:50 local).
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "last_cycle_completed_at": DOWN_AT,
            "detection_resumed_at": DOWN_AT,
            "web_started_at": None,
        },
    )
    location = location_factory(created_at=_kyiv("2026-10-01 07:00"))
    _monitor(location, _kyiv("2026-10-01 08:00"))
    LocationState.objects.filter(location=location).update(last_heartbeat_at=DOWN_AT)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(T)

    serving = _Serve(lease, clock, tmp_path / "health")
    try:
        assert wait_for(lambda: fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") >= 1, 30)
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    # The restart carved the downtime: one gap, stored as not monitored.
    gap = OpsIncident.objects.get(kind=lapse.KIND_MONITORING_GAP)
    assert (gap.started_at, gap.ended_at) == (DOWN_AT, T)
    assert PowerInterval.objects.filter(
        location=location, state="not_monitored", start_at=DOWN_AT, end_at=T
    ).exists()
    # Every week the worker drew shows the downtime as not monitored and never as on.
    assert weeks, "the worker built no week for its chart"
    for week in weeks:
        assert week.now == T
        segments = week.today_row.segments
        assert any(
            s.state == "not_monitored" and s.start_us <= GAP_START_US and s.end_us >= GAP_END_US
            for s in segments
        ), segments
        assert not any(
            s.state == "on" and s.start_us < GAP_END_US and s.end_us > GAP_START_US
            for s in segments
        ), segments
    # The I/O thread served the generation before its carve, with no chart work then.
    assert passes[:3] == [False, False, False]
    # The chart posted is today's live chart (the downtime is not off time either).
    assert fake_telegram.chart_calls[0].method == "sendPhoto"
    assert fake_telegram.chart_calls[0].fields["caption"].startswith("No outages today")
