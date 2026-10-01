"""Worker supervision: progress stamps, the watchdog, the health file, the DB-outage log.

Docker restarts a container only when its process exits. An "unhealthy" status alone
restarts nothing (INV-13). So a stalled loop must end the worker process, and the main
thread watches the loops (D-15, OPS-05):

- Each loop stamps ``Progress`` at the top of every iteration, after every location of a
  detection cycle and after every outbox row.
- ``Watchdog.check()`` finds a loop whose last stamp is more than its limit old
  (detection 60 s, Telegram I/O 180 s), or whose thread has died while the worker is not
  stopping, and runs the stall action once. A loop thread that ended because of the stop
  event is a clean shutdown (E1). ``default_on_stall`` logs CRITICAL, dumps every
  thread's stack with ``faulthandler`` (file, line, function and thread names only,
  never local values), flushes, and exits with ``EXIT_STALL`` so Docker's restart policy
  restarts the worker.
  A loop blocked inside a call that never returns is exactly the case this catches.
  Tests inject the stall action instead.
- An unreachable database is progress, not a stall: a loop that retries the database
  still stamps every iteration. The health file (``HEALTH_FILE``) is the signal for
  Docker's health status: the worker touches it after every successful cycle while it
  holds the lock and on every standby cycle, never while the database is down, so the
  container shows unhealthy during an outage without being restarted (INV-13). The
  worker's compose healthcheck requires it to be under 30 s old.
- ``DbOutageLog`` turns a run of database errors into one WARNING when the database goes
  away and one when it is back, with the error class only (D-16, OPS-08).

Everything reads time from the injected ``Clock`` (monotonic time for every age), so
tests drive it with ``FakeClock``. The health file's mtime is the one exception: the
healthcheck compares it with the system clock, so it is set by the filesystem.
"""

import contextlib
import faulthandler
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from powermon.clock import Clock

log = logging.getLogger(__name__)

# The worker's exit status after a stall: not 0 (Docker restarts it under unless-stopped
# either way, and a non-zero code tells the operator something went wrong) and not the
# interpreter's own 1.
EXIT_STALL = 70
# Inside the worker container; /app is read-only for the app user, /tmp is writable.
HEALTH_FILE = Path("/tmp/powermon-worker.health")  # noqa: S108 - container-private path
# How long each loop may go without stamping (D-15). A detection cycle's worst bounded
# chain (lease check, reconnect, one location's statements) and an I/O row's (claim,
# send with connect and read timeouts, apply) fit well inside (RESEARCH timing budget).
DETECTION_STALL_S = 60.0
IO_STALL_S = 180.0
STALL_LIMITS_S = {"detection": DETECTION_STALL_S, "telegram-io": IO_STALL_S}


class Progress:
    """The monotonic time each loop last showed progress; shared by the loop threads."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._stamps: dict[str, float] = {}

    def stamp(self, name: str) -> None:
        """Record that loop ``name`` made progress now."""
        now = self._clock.monotonic()
        with self._lock:
            self._stamps[name] = now

    def last(self, name: str) -> float | None:
        """The monotonic time of loop ``name``'s last stamp, or None if it never stamped."""
        with self._lock:
            return self._stamps.get(name)


class Watchdog:
    """Run ``on_stall(name)`` once when a loop is stale or its thread has died.

    ``stop`` is the worker's stop event (SIGTERM, SIGINT). The loops return once it is
    set, so a loop thread that has ended while it is set is a shutdown, never a stall
    (E1): a check that began just before the stop then finds nothing, and the worker
    exits 0. A loop that dies or hangs while the stop is not set is a stall, as before.
    """

    def __init__(
        self,
        clock: Clock,
        progress: Progress,
        limits: Mapping[str, float],
        threads: Mapping[str, threading.Thread],
        on_stall: Callable[[str], None],
        stop: threading.Event | None = None,
    ) -> None:
        self._clock = clock
        self._progress = progress
        self._limits = dict(limits)
        self._threads = dict(threads)
        self._on_stall = on_stall
        self._stop = stop
        # A loop that never stamped counts from here.
        self._started = clock.monotonic()
        self._fired = False

    def check(self) -> str | None:
        """The first stalled loop in the limits' order, or None; acts only the first time."""
        now = self._clock.monotonic()
        for name, limit in self._limits.items():
            thread = self._threads.get(name)
            # The thread's state first, then the stop's: a loop thread returns normally
            # only after the stop is set, so a thread seen ended with the stop seen set
            # after it ended because of the stop. Read the other way round, a loop that
            # ends between the two reads would look dead (E1).
            dead = thread is not None and not thread.is_alive()
            if dead and self._stop is not None and self._stop.is_set():
                continue
            last = self._progress.last(name)
            since = self._started if last is None else last
            if dead or now - since > limit:
                if not self._fired:
                    self._fired = True
                    self._on_stall(name)
                return name
        return None


def default_on_stall(name: str) -> None:
    """Log CRITICAL, dump every thread's stack, flush and exit ``EXIT_STALL``; never returns."""
    limit = STALL_LIMITS_S.get(name, 0.0)
    log.critical(
        "worker loop %s made no progress for over %d s or ended; exiting for a restart",
        name,
        limit,
    )
    # Neither the dump nor a flush may keep the process from exiting.
    with contextlib.suppress(Exception):
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    for handler in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            handler.flush()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    # os._exit skips interpreter shutdown, which could wait forever on the stuck thread.
    os._exit(EXIT_STALL)


class HealthFile:
    """The file whose mtime tells Docker's healthcheck that the worker is healthy."""

    def __init__(self, path: Path = HEALTH_FILE) -> None:
        self.path = path
        # True from a failed touch until a touch works again.
        self._failing = False

    def touch(self) -> None:
        """Set the file's mtime to now, creating it; a failure is logged once per run."""
        try:
            self.path.touch(exist_ok=True)
            os.utime(self.path)
        except OSError as exc:
            if not self._failing:
                log.warning(
                    "worker health file %s cannot be written (%s)", self.path, type(exc).__name__
                )
                self._failing = True
            return
        self._failing = False


class DbOutageLog:
    """One WARNING when a loop's database goes away and one when it is back (D-16)."""

    def __init__(self, logger: logging.Logger, what: str, clock: Clock) -> None:
        self._log = logger
        self._what = what
        self._clock = clock
        # The monotonic time of the first failure of the current run, or None.
        self._since: float | None = None

    def failed(self, exc: BaseException) -> None:
        """Note a database error; only the first of a run is logged, by class name."""
        if self._since is None:
            self._since = self._clock.monotonic()
            self._log.warning(
                "%s: database unreachable (%s); retrying", self._what, type(exc).__name__
            )

    def ok(self) -> None:
        """Note a success; after a run of failures, log how long the outage lasted."""
        if self._since is None:
            return
        seconds = self._clock.monotonic() - self._since
        self._since = None
        self._log.warning("%s: database reachable again after %d s", self._what, seconds)
