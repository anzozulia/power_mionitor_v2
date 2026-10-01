"""``manage.py run_worker``: the single active worker process (D-14, D-18, KD2, KD4).

One process, three threads:
- main: takes the single-worker lock (``Lease``: ``pg_try_advisory_lock`` on a dedicated
  connection), activates, starts the two loops, then checks every few seconds that the
  lock session and both loops are alive;
- ``detection``: ``detection.run_cycle(now)`` every DETECTION_INTERVAL_S seconds;
- ``telegram-io``: ``io_loop.run_iteration(now, state)`` back to back while there is work,
  waiting IO_IDLE_WAIT_S seconds when a pass sent nothing.

A second instance (a deploy overlap, or one started by mistake) polls the lock in
standby and never runs a loop; it does not block and does not exit (Pitfall 2).
Activation opens a fresh detection window (``system_state.detection_resumed_at`` = start,
D-14) and turns sends interrupted by the previous process into "uncertain" (INV-16).

If the lock session dies or a loop thread dies, the worker stops its loops and exits with
code 3; Docker's restart policy brings it back, and the new process re-takes the lock and
opens a fresh window (D-18). There is no in-process reacquisition before Phase 2.

SIGTERM and SIGINT set the stop event (Python as PID 1 ignores SIGTERM without a handler,
Pitfall 14). The loops notice it at their next wait, so ``docker compose stop`` finishes
well inside the 30 s stop_grace_period; joins are bounded by JOIN_TIMEOUT_S.
"""

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from types import FrameType
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from powermon.alerts import outbox
from powermon.clock import Clock, SystemClock
from powermon.engine.models import SystemState
from powermon.worker.detection import run_cycle
from powermon.worker.io_loop import RelayState, run_iteration
from powermon.worker.lease import Lease

log = logging.getLogger(__name__)

STANDBY_POLL_S = 5.0
LEASE_CHECK_INTERVAL_S = 5.0
DETECTION_INTERVAL_S = 5.0
IO_IDLE_WAIT_S = 1.0
# Both joins share this budget: below compose's stop_grace_period of 30 s, above the
# Telegram client's 5 s connect + 10 s read timeouts.
JOIN_TIMEOUT_S = 15.0
EXIT_LEASE_LOST = 3


def activate(now: datetime) -> None:
    """Start the active term: interrupted sends become uncertain, detection restarts at now."""
    recovered = outbox.recover_interrupted()
    # update_or_create: a missing singleton row must not stop the worker.
    SystemState.objects.update_or_create(pk=1, defaults={"detection_resumed_at": now})
    log.info(
        "worker active since %s; %d interrupted send(s) marked uncertain",
        now.isoformat(),
        recovered,
    )


def detection_loop(stop: threading.Event, clock: Clock, interval: float) -> None:
    """Run a detection cycle every ``interval`` seconds until ``stop`` is set."""
    try:
        while not stop.is_set():
            started = clock.monotonic()
            try:
                run_cycle(clock.now())
            except Exception:
                # A database error never kills the thread; the next cycle reconnects.
                log.exception("detection cycle failed")
            stop.wait(max(0.0, interval - (clock.monotonic() - started)))
    finally:
        connection.close()


def io_thread(stop: threading.Event, clock: Clock, idle_wait: float) -> None:
    """Drain the outbox until ``stop`` is set; wait ``idle_wait`` s after an idle pass."""
    state = RelayState()
    try:
        while not stop.is_set():
            try:
                busy = run_iteration(clock.now(), state)
            except Exception:
                log.exception("telegram I/O iteration failed")
                busy = False
            if not busy:
                stop.wait(idle_wait)
    finally:
        connection.close()


def serve(
    stop: threading.Event,
    clock: Clock,
    lease: Lease,
    *,
    standby_poll: float = STANDBY_POLL_S,
    check_interval: float = LEASE_CHECK_INTERVAL_S,
    detection_interval: float = DETECTION_INTERVAL_S,
    io_idle_wait: float = IO_IDLE_WAIT_S,
) -> int:
    """Run the worker until ``stop`` is set (0) or the lock or a loop is lost (3)."""
    try:
        if not _wait_for_lease(stop, lease, standby_poll):
            return 0
        activate(clock.now())
        threads = [
            _start("detection", detection_loop, stop, clock, detection_interval),
            _start("telegram-io", io_thread, stop, clock, io_idle_wait),
        ]
        while not stop.wait(check_interval):
            if not lease.alive() or not all(t.is_alive() for t in threads):
                log.critical("worker lock lost or a loop died; exiting for a clean restart")
                stop.set()
                _join(threads, clock)
                return EXIT_LEASE_LOST
        _join(threads, clock)
        log.info("worker stopped")
        return 0
    finally:
        lease.close()
        connection.close()


def _wait_for_lease(stop: threading.Event, lease: Lease, poll: float) -> bool:
    """Poll for the lock without blocking; False if ``stop`` is set first."""
    announced = False
    while not stop.is_set():
        if lease.try_acquire():
            return True
        if not announced:
            log.info("standby: waiting for the worker lock")
            announced = True
        stop.wait(poll)
    return False


def _start(
    name: str, target: Callable[[threading.Event, Clock, float], None], *args: Any
) -> threading.Thread:
    # Daemon threads: a loop stuck in a call cannot keep the process alive after serve.
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


def _join(threads: list[threading.Thread], clock: Clock) -> None:
    deadline = clock.monotonic() + JOIN_TIMEOUT_S
    for thread in threads:
        thread.join(max(0.0, deadline - clock.monotonic()))
        if thread.is_alive():
            log.warning("thread %s did not stop within %.0f s", thread.name, JOIN_TIMEOUT_S)


class Command(BaseCommand):
    help = "Run the worker: OFF detection and alert delivery, one active instance at a time."

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.CFG.build:
            raise CommandError("run_worker cannot run in build mode (APP_BUILD=1)")
        stop = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, request_stop)
        code = serve(stop, SystemClock(), Lease(connection.settings_dict))
        if code != 0:
            # Exit at once with the code, so Docker's restart policy restarts the process;
            # os._exit skips interpreter shutdown, so flush the log lines first.
            for handler in logging.getLogger().handlers:
                handler.flush()
            sys.stdout.flush()
            os._exit(code)
