"""``manage.py run_worker``: the worker process that keeps itself running (D-15, D-16, KD2).

One process, three threads, all started at once and always running:

- ``detection``: every DETECTION_INTERVAL_S seconds (on the monotonic clock) it asks the
  lease ``ensure_held()``. Only this loop polls the lock. HELD runs a detection cycle
  (``detection.run_cycle``); STANDBY and DB_DOWN are idle iterations.
- ``telegram-io``: reads the status the detection loop last published
  (``lease.current()``). While HELD it drains the outbox (``io_loop.run_iteration``) back
  to back while there is work, waiting IO_IDLE_WAIT_S seconds after a pass that sent
  nothing; otherwise it idles.
- main: the watchdog (below), until SIGTERM or SIGINT.

The lease is HELD, STANDBY or DB_DOWN, with a generation counter that rises on every
successful acquisition (``powermon.worker.lease``). Each loop activates once per new
generation and marks it seen only after its activation returned, so a database error
retries it: the detection loop opens a fresh detection window (``activate_detection``;
02-06 replaces it with the lapse carve), and the I/O loop turns sends left in "sending"
into "uncertain" (``io_loop.activate``, INV-16). A lost lease session is reacquired in
process on a fresh connection, and a standby never blocks, never exits and never writes
or sends (MON-04). This replaces Phase 1's exit with code 3 (D-15 replaces Phase 1 D-18).

Every worker DB entry point (``activate_detection``, ``detection.run_cycle``,
``io_loop.activate``, ``io_loop.run_iteration``) starts with ``close_old_connections()``.
Django health-checks a connection, and so replaces one the database dropped, only after
that call, so either thread's next activation or pass replaces a terminated session
before its first statement instead of failing on it forever while its progress stamps
keep the watchdog quiet (D-16, MON-06). A database error in a loop logs one WARNING when
the database goes away and one when it is back (``DbOutageLog``); any other error is
logged with its traceback, and the loop goes on (INV-13).

The worker's Django connections are bounded and persistent (D-16, RESEARCH Pitfall 3):
before any connection opens, ``apply_worker_db_settings`` puts ``WORKER_PG_OPTIONS``
(statement_timeout 10 s, lock_timeout 5 s), application_name powermon-worker and
CONN_MAX_AGE None into the settings dict that every thread's connection shares. The web's
connect_timeout, keepalives and tcp_user_timeout stay.

Supervision (D-15, OPS-05, INV-13 #3): each loop stamps its progress at the top of every
iteration (DB-down iterations included), after every location and after every outbox row.
The main thread checks every WATCHDOG_CHECK_S seconds. A loop that has not stamped for
DETECTION_STALL_S / IO_STALL_S seconds, or whose thread died, runs the stall action:
by default one CRITICAL line, a stack dump of every thread, a flush and exit 70, after
which Docker's restart policy restarts the worker. A loop blocked inside a call that never
returns ends this way. The worker touches the health file (``supervision.HEALTH_FILE``)
after every successful HELD cycle and on every STANDBY cycle, never while the database is
down; the compose healthcheck that reads it arrives in 02-10.

SIGTERM and SIGINT set the stop event (Python as PID 1 ignores SIGTERM without a handler,
Pitfall 14). The loops notice it at their next wait, and the relay also checks it before
claiming each row, so no new send starts after a stop request. The joins wait up to
JOIN_TIMEOUT_S for a send already in flight, which ends inside the 30 s stop_grace_period.
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
from django.db import (
    InterfaceError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
)

from powermon.clock import Clock, SystemClock
from powermon.engine.models import SystemState
from powermon.worker import detection, io_loop
from powermon.worker.lease import Lease, LeaseState
from powermon.worker.supervision import (
    DETECTION_STALL_S,
    EXIT_STALL,
    IO_STALL_S,
    DbOutageLog,
    HealthFile,
    Progress,
    Watchdog,
    default_on_stall,
)

log = logging.getLogger(__name__)

DETECTION_INTERVAL_S = 5.0
IO_IDLE_WAIT_S = 1.0
WATCHDOG_CHECK_S = 5.0
# Both joins share this budget. After a stop the relay claims no new row, so the joins
# only wait for the send in flight: above the Telegram client's 5 s connect + 10 s read
# timeouts, below compose's 30 s stop_grace_period, after which Docker sends SIGKILL.
# tests/test_compose.py ties the three together.
JOIN_TIMEOUT_S = 20.0
# The worker's Django sessions in pg_stat_activity; the lease session is
# powermon-worker-lease.
WORKER_APPLICATION_NAME = "powermon-worker"
# DETECTION_STALL_S (60 s), IO_STALL_S (180 s) and EXIT_STALL (70) come from supervision,
# where the default stall action also reads them.

# Connectivity errors: one WARNING per outage, never a traceback per cycle (D-16).
_DB_ERRORS = (OperationalError, InterfaceError)


def apply_worker_db_settings(settings_dict: dict[str, Any]) -> None:
    """Make ``settings_dict`` the worker's: bounded sessions, persistent connections.

    OPTIONS is replaced by a copy, never changed in place; CONN_HEALTH_CHECKS stays on.
    """
    settings_dict["CONN_MAX_AGE"] = None
    settings_dict["OPTIONS"] = {
        **settings_dict.get("OPTIONS", {}),
        "options": settings.WORKER_PG_OPTIONS,
        "application_name": WORKER_APPLICATION_NAME,
    }


def activate_detection(now: datetime, generation: int) -> None:
    """Open a fresh detection window at ``now`` for a new lease generation."""
    close_old_connections()
    # update_or_create: a missing singleton row must not stop the worker.
    SystemState.objects.update_or_create(pk=1, defaults={"detection_resumed_at": now})
    log.info("worker active since %s (generation %d)", now.isoformat(), generation)


def detection_loop(
    stop: threading.Event,
    clock: Clock,
    interval: float,
    lease: Lease,
    progress: Progress,
    health: HealthFile,
) -> None:
    """Keep the lease and run a detection cycle every ``interval`` s until ``stop`` is set."""
    outage = DbOutageLog(log, "detection", clock)
    activated = 0  # the last generation this loop activated

    def tick() -> None:
        progress.stamp("detection")

    try:
        while not stop.is_set():
            started = clock.monotonic()
            tick()
            try:
                status = lease.ensure_held()
                if status.state is LeaseState.HELD:
                    if status.generation != activated:
                        activate_detection(clock.now(), status.generation)
                        activated = status.generation
                    detection.run_cycle(clock.now(), tick=tick)
                    health.touch()
                    outage.ok()
                elif status.state is LeaseState.STANDBY:
                    health.touch()
            except _DB_ERRORS as exc:
                outage.failed(exc)
            except Exception:
                log.exception("detection cycle failed")
            stop.wait(max(0.0, interval - (clock.monotonic() - started)))
    finally:
        connection.close()


def io_thread(
    stop: threading.Event,
    clock: Clock,
    idle_wait: float,
    lease: Lease,
    progress: Progress,
) -> None:
    """Drain the outbox while HELD until ``stop`` is set; wait ``idle_wait`` s when idle."""
    state = io_loop.RelayState()
    outage = DbOutageLog(log, "telegram-io", clock)
    activated = 0  # the last generation this loop activated

    def tick() -> None:
        progress.stamp("telegram-io")

    try:
        while not stop.is_set():
            tick()
            busy = False
            try:
                status = lease.current()
                if status.state is LeaseState.HELD:
                    if status.generation != activated:
                        io_loop.activate(state, clock)
                        activated = status.generation
                    busy = io_loop.run_iteration(clock, state, stop, tick=tick)
                    outage.ok()
            except _DB_ERRORS as exc:
                outage.failed(exc)
            except Exception:
                log.exception("telegram I/O iteration failed")
            if not busy:
                stop.wait(idle_wait)
    finally:
        connection.close()


def serve(
    stop: threading.Event,
    clock: Clock,
    lease: Lease,
    *,
    detection_interval: float = DETECTION_INTERVAL_S,
    io_idle_wait: float = IO_IDLE_WAIT_S,
    check_interval: float = WATCHDOG_CHECK_S,
    health: HealthFile | None = None,
    on_stall: Callable[[str], None] | None = None,
) -> int:
    """Run both loops under the watchdog until ``stop`` is set (0).

    Returns EXIT_STALL only when an injected ``on_stall`` returns; the default one exits
    the process itself.
    """
    progress = Progress(clock)
    health_file = HealthFile() if health is None else health
    try:
        threads = {
            "detection": _start(
                "detection",
                detection_loop,
                stop,
                clock,
                detection_interval,
                lease,
                progress,
                health_file,
            ),
            "telegram-io": _start(
                "telegram-io", io_thread, stop, clock, io_idle_wait, lease, progress
            ),
        }
        watchdog = Watchdog(
            clock,
            progress,
            {"detection": DETECTION_STALL_S, "telegram-io": IO_STALL_S},
            threads,
            default_on_stall if on_stall is None else on_stall,
        )
        while not stop.wait(check_interval):
            if watchdog.check() is not None:
                stop.set()
                _join(list(threads.values()), clock)
                return EXIT_STALL
        _join(list(threads.values()), clock)
        log.info("worker stopped")
        return 0
    finally:
        lease.close()
        connection.close()


def _start(name: str, target: Callable[..., None], *args: Any) -> threading.Thread:
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
        # Before any worker connection opens: the dict is shared by every thread's wrapper.
        apply_worker_db_settings(connection.settings_dict)
        connections.close_all()
        if not settings.CFG.ops_configured:
            log.warning(
                "ops chat not configured (OPS_BOT_TOKEN, OPS_CHAT_ID unset): "
                "ops notices go to this log only"
            )
        stop = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, request_stop)
        clock = SystemClock()
        code = serve(stop, clock, Lease(connection.settings_dict, clock))
        if code != 0:
            # Exit at once with the code, so Docker's restart policy restarts the process;
            # os._exit skips interpreter shutdown, so flush the log lines first.
            for handler in logging.getLogger().handlers:
                handler.flush()
            sys.stdout.flush()
            os._exit(code)
