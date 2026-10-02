"""The midnight chart job for 20 locations never slows detection (INV-14 #3, D-05).

"Given the midnight chart job for 20 locations, then detection cycles keep their normal
cadence during it" (docs/v1-lessons.md INV-14). The worker runs for real
(``run_worker.serve(..., charts=True)``: both loops, the lease on the test database, the
first-cycle gate), with real renders (Pillow) and Telegram faked at the HTTP boundary.
At 00:02:00 local on Fri 2026-10-02, each of 20 locations has its Thu 2026-10-01 chart
pinned and not finalized. That is past every location's settle point (midnight + longest
effective timeout + lapse threshold, 00:01:45 with the defaults), so yesterday's final
edit is due too, and the Telegram I/O thread runs the whole midnight job: for
each location a post (live render), a pin, a final edit (finished render) and an unpin,
80 calls and 40 renders, one call per pass (D-02, D-05). Meanwhile
``detection.run_detection`` (spied on as the module attribute run_worker calls) must keep
its cadence: 10 cycles within 2 s at a 0.1 s interval, and no gap over 1 s between
consecutive cycles while the job runs. Rendering runs only in the I/O thread, and Pillow
releases the GIL in its heavy operations, so detection is never starved.

Why every heartbeat and the detection cursor sit at the frozen clock time: the worker's
``FakeClock`` never moves, so with ``last_cycle_completed_at`` = now no cycle sees a gap
(``lapse.carve_if_needed`` carves nothing when now is not after the cursor), and with
every ``last_heartbeat_at`` = now no location times out. The chart job is then the only
work in the window: no OFF transition, no lapse carve, no subscriber alert, which the end
state checks. Real ``time.monotonic()`` measures cadence and each render's duration (a spy
around ``render.render_png``, the module attribute the lifecycle calls); the lifecycle's
own ``render_ms``/``call_ms`` log values are 0 here because the FakeClock's monotonic
time is frozen. Each fake call answers after 50 ms, like a fast real answer, so the job
surely outlasts both cadence windows; the max-gap check covers the job from its first
call to its last. The watchdog checks every 0.1 s: with the frozen clock it cannot see a
slow loop (the cadence assertions do), but it does see a loop thread that died.

The real per-render time and the worker's memory around midnight are measured once on the
VPS at /gsd-verify-work 3 (README section 12, check e): from 23:55 to 00:10 local, sample
``docker stats`` for the worker every second, read the worker's INFO chart lines with
``render_ms`` and ``call_ms``, and confirm there was no monitoring-gap notice.

The few chart helpers are copied from tests/chart/chart_fixtures.py instead of imported:
tests have no ``__init__.py``, so tests/chart is on ``sys.path`` only once one of its
modules was collected, and this file must also run on its own.
"""

import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import DEFAULT_BOT_TOKEN, FakeClock, wait_for
from django.db import connection
from django.db.models import F

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart import lifecycle, render
from powermon.chart.models import ChartMessage
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.worker import detection, io_loop, supervision
from powermon.worker.lease import Lease
from powermon.worker.management.commands import run_worker

_KYIV = "Europe/Kyiv"
# test_never_blocked.py's worker intervals; the watchdog checks often (see the docstring).
INTERVALS = {"detection_interval": 0.1, "io_idle_wait": 0.05, "check_interval": 0.1}
CYCLES = 10
CADENCE_WINDOW_S = 2.0
MAX_CYCLE_GAP_S = 1.0
LOCATIONS = 20
CHART_CALLS = 4 * LOCATIONS  # post, pin, final edit, unpin per location
RENDERS = 2 * LOCATIONS  # the live post and the finished edit per location
JOB_BUDGET_S = 60.0
# Each fake chart call answers after this long: a fast real Telegram answer (real ones take
# 0.1-0.5 s). It keeps the job (80 calls, 40 renders) well longer than the two cadence
# windows, so both windows surely fall inside the job on any machine; the I/O thread waits
# in the call as it would on the network, without the GIL.
CALL_LATENCY_S = 0.05
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LIFECYCLE_LOGGER = lifecycle.__name__


def _kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-02 00:00:30"`` (fold 0) as an aware UTC instant."""
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


TODAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)
NOW = _kyiv("2026-10-02 00:02:00")
SINCE = _kyiv("2026-09-28 00:00")
# Yesterday's chart was last refreshed at 23:45, as the 15-min cadence leaves it.
LAST_REFRESH = _kyiv("2026-10-01 23:45")


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
def cycles(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The real time of every ``detection.run_detection`` call (the real one still runs)."""
    calls: list[float] = []
    real = detection.run_detection

    def spy(*args: Any, **kwargs: Any) -> int:
        calls.append(time.monotonic())
        return real(*args, **kwargs)

    monkeypatch.setattr(detection, "run_detection", spy)
    return calls


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, bytes]]:
    """Each ``render.render_png`` call's real duration and its PNG (the real one still runs)."""
    seen: list[tuple[float, bytes]] = []
    real = render.render_png

    def timed(*args: Any, **kwargs: Any) -> bytes:
        started = time.monotonic()
        png = real(*args, **kwargs)
        seen.append((time.monotonic() - started, png))
        return png

    monkeypatch.setattr(render, "render_png", timed)
    return seen


def _cycles_within(calls: list[float], count: int, seconds: float) -> bool:
    """True once ``count`` more detection cycles ran within ``seconds`` real seconds."""
    seen = len(calls)
    return wait_for(lambda: len(calls) >= seen + count, seconds)


def _midnight(location_factory: Callable[..., Any]) -> list[Any]:
    """20 locations monitored since Mon 28.09, each with yesterday's chart pinned, not final."""
    locations = []
    for n in range(1, LOCATIONS + 1):
        chat_id = -1001000000000 - n
        location = location_factory(name=f"Location {n}", chat_id=chat_id, created_at=SINCE)
        _monitor(location, SINCE)
        ChartMessage.objects.create(
            location=location,
            local_date=YESTERDAY,
            chat_id=chat_id,
            # Posted by the location's own bot, so it is not released (D-08).
            bot_key=io_loop.bot_key(location.bot_token),
            message_id=500 + n,
            pinned=True,
            last_rendered_at=LAST_REFRESH,
            created_at=LAST_REFRESH,
        )
        locations.append(location)
    # Anchored at the frozen clock time: no gap to carve and no location timing out.
    LocationState.objects.update(last_heartbeat_at=NOW)
    SystemState.objects.update_or_create(
        pk=1,
        defaults={
            "last_cycle_completed_at": NOW,
            "detection_resumed_at": NOW,
            "web_started_at": None,
        },
    )
    return locations


@pytest.mark.django_db(transaction=True)
def test_INV14_3_midnight_chart_job_keeps_detection_cadence(
    lease: Lease,
    cycles: list[float],
    renders: list[tuple[float, bytes]],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    locations = _midnight(location_factory)
    # Every chart call is accepted and recorded, after CALL_LATENCY_S.
    for method in fake_telegram.CHART_METHODS:
        fake_telegram.answer_method(DEFAULT_BOT_TOKEN, method, lambda: time.sleep(CALL_LATENCY_S))
    caplog.set_level(logging.INFO, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOW)

    serving = _Serve(lease, clock, tmp_path / "health")
    try:
        # The job starts with the first location's post.
        assert wait_for(lambda: len(fake_telegram.chart_calls) >= 1, 30)
        job_start = time.monotonic()
        # Detection keeps its cadence while the job runs, twice over.
        assert _cycles_within(cycles, CYCLES, CADENCE_WINDOW_S)
        assert _cycles_within(cycles, CYCLES, CADENCE_WINDOW_S)
        windows_done = len(fake_telegram.chart_calls)
        assert wait_for(lambda: len(fake_telegram.chart_calls) >= CHART_CALLS, JOB_BUDGET_S)
        job_end = time.monotonic()
        # Nothing more is due once the job is done (the frozen clock makes no refresh due).
        assert not wait_for(lambda: len(fake_telegram.chart_calls) > CHART_CALLS, 0.5)
    finally:
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []

    # Both cadence windows fell inside the job, not after it.
    assert windows_done < CHART_CALLS
    # No gap over 1 s between consecutive detection cycles from the job's start to its end.
    during = [t for t in cycles if job_start <= t <= job_end]
    gaps = [b - a for a, b in pairwise([job_start, *during, job_end])]
    max_gap = max(gaps)
    assert max_gap <= MAX_CYCLE_GAP_S, f"detection paused {max_gap:.2f} s during the chart job"

    # The job: exactly one post, pin, final edit and unpin per location, nothing else.
    methods = [call.method for call in fake_telegram.chart_calls]
    assert {m: methods.count(m) for m in set(methods)} == {
        "sendPhoto": LOCATIONS,
        "pinChatMessage": LOCATIONS,
        "editMessageMedia": LOCATIONS,
        "unpinChatMessage": LOCATIONS,
    }
    for location in locations:
        records = ChartMessage.objects.filter(location=location)
        pinned = records.filter(pinned=True)
        assert [r.local_date for r in pinned] == [TODAY]
        older = records.get(local_date=YESTERDAY)
        assert older.finalized_at is not None
        assert older.pinned is False
    # The chart job was the only work: no outage, no carve, no subscriber alert.
    assert not PowerInterval.objects.filter(state__in=("off", "not_monitored")).exists()
    assert not OutboxMessage.objects.filter(channel=outbox.CHANNEL_SUBSCRIBER).exists()

    # The in-process render timing: 40 real renders, each a PNG.
    assert len(renders) == RENDERS
    assert all(png.startswith(PNG_SIGNATURE) for _, png in renders)
    slowest = max(duration for duration, _ in renders)
    # The worker logs one INFO line per post with render_ms (0 under the frozen clock).
    posts = [
        record.getMessage()
        for record in caplog.records
        if record.name == LIFECYCLE_LOGGER and "chart post for location" in record.getMessage()
    ]
    assert len(posts) == LOCATIONS
    assert all("render_ms=0 call_ms=0" in line for line in posts)
    # The measurements the SUMMARY records (pytest -rP shows them).
    print(
        f"INV-14 #3: job {job_end - job_start:.2f} s for {CHART_CALLS} calls, "
        f"{len(during)} detection cycles, max gap {max_gap:.3f} s, "
        f"slowest render {slowest:.3f} s, mean render "
        f"{sum(d for d, _ in renders) / len(renders):.3f} s"
    )
