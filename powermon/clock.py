"""The time source for engine and view code (docs/v1-lessons.md section 1: injectable clock).

Engine and view code never read the system time themselves. They receive a ``Clock``
(views by constructor injection) or an explicit ``now``, so tests control time.
``SystemClock`` is the only place that reads the system clock.
"""

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Wall time for the timeline, monotonic time for cadence and timeouts."""

    def now(self) -> datetime:
        """The current time as an aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never decreases."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
