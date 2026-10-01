"""RED-phase interface stub of ``manage.py run_worker`` (replaced in GREEN)."""

import threading
from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand

from powermon.clock import Clock
from powermon.worker.detection import run_cycle
from powermon.worker.io_loop import run_iteration
from powermon.worker.lease import Lease

STANDBY_POLL_S = 5.0
LEASE_CHECK_INTERVAL_S = 5.0
DETECTION_INTERVAL_S = 5.0
IO_IDLE_WAIT_S = 1.0
JOIN_TIMEOUT_S = 15.0
EXIT_LEASE_LOST = 3

__all__ = ["run_cycle", "run_iteration"]


def activate(now: datetime) -> None:
    return None


def detection_loop(stop: threading.Event, clock: Clock, interval: float) -> None:
    return None


def io_thread(stop: threading.Event, clock: Clock, idle_wait: float) -> None:
    return None


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
    return -1


class Command(BaseCommand):
    help = "Run the worker (stub)."

    def handle(self, *args: Any, **options: Any) -> None:
        return None
