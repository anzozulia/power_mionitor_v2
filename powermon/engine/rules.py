"""The engine's pure rules: the one home of the OFF timing rule (INV-03, MON-02).

PURE: no Django import and no clock read. Every function takes its inputs, including
``now``, as arguments, so the detector, the heartbeat gate and later phases share one
definition and the tests need no database.

The OFF rule (PROJECT.md, docs/v1-lessons.md section 2):
- A location that is on is OFF when ``now - anchor > effective timeout`` (strict ``>``,
  K-2: "after 10:06:30, not at it").
- The effective timeout is period + grace, plus ``ROUTER_GRACE`` when router-reconnect
  grace is on and the last heartbeat came at most ``ROUTER_WINDOW`` after the location
  turned on (inclusive, K-4).
- The anchor is the latest of the last heartbeat, the location's window start and the
  system anchors (worker start, web start): after a restart every location gets a fresh
  detection window (D-14), and the outage then starts at that anchor (INV-04/INV-11).
- "Was ON for" is always last heartbeat - on time (the literal rule).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

ROUTER_GRACE = timedelta(seconds=180)
# Inclusive: a last heartbeat exactly 300 s after on still gets the grace (K-4 case 3).
ROUTER_WINDOW = timedelta(seconds=300)

STATUSES_WITH_POWER_STATE = ("on", "off")


@dataclass(frozen=True)
class Snapshot:
    """A location that is on, as the detector read it (with its CAS token)."""

    location_id: int
    state_version: int
    last_heartbeat_at: datetime
    on_since: datetime
    # Maintenance-exit window start (Phase 4); None when unused.
    window_start_at: datetime | None
    period_s: int
    grace_s: int
    router_grace: bool


@dataclass(frozen=True)
class Anchors:
    """Process-level window starts from ``system_state``."""

    # The worker's activation time (D-14).
    detection_resumed_at: datetime | None
    # The web app's start time (Phase 2 lapse carve).
    web_started_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """Whether the location is OFF now and, if so, the outage it starts."""

    off: bool
    outage_start: datetime | None = None
    was_on: timedelta | None = None


def effective_timeout(snap: Snapshot) -> timedelta:
    """Period + grace, plus the router-reconnect grace when it applies."""
    applies = snap.router_grace and snap.last_heartbeat_at - snap.on_since <= ROUTER_WINDOW
    return longest_timeout(snap.period_s, snap.grace_s, applies)


def longest_timeout(period_s: int, grace_s: int, router_grace: bool) -> timedelta:
    """The longest effective timeout a location can have.

    Period + grace, plus ``ROUTER_GRACE`` when router-reconnect grace is on.
    ``effective_timeout`` adds the router grace only while its window applies, so it is
    never longer: an outage that started before ``t`` is recorded by the first detection
    cycle after ``t`` + this (the chart's final edit waits for it, INV-03).
    """
    timeout = timedelta(seconds=period_s + grace_s)
    return timeout + ROUTER_GRACE if router_grace else timeout


def anchor(snap: Snapshot, anchors: Anchors) -> datetime:
    """The instant the silence is counted from: the latest known start, ignoring None."""
    candidates = (
        snap.last_heartbeat_at,
        snap.window_start_at,
        anchors.detection_resumed_at,
        anchors.web_started_at,
    )
    return max(c for c in candidates if c is not None)


def decide(snap: Snapshot, anchors: Anchors, now: datetime) -> Decision:
    """OFF only when more than the effective timeout has passed since the anchor."""
    start = anchor(snap, anchors)
    if now - start > effective_timeout(snap):
        return Decision(off=True, outage_start=start, was_on=snap.last_heartbeat_at - snap.on_since)
    return Decision(off=False)


def desired_open_state(status: str, maintenance: bool) -> str | None:
    """The timeline state a location in ``status`` should have open.

    "waiting" has no interval (no data before the first heartbeat). "on" and "off" are
    stored as themselves, or as "not_monitored" while the location is in maintenance
    (LOC-08: the chart shows maintenance as not monitored, never as an outage).
    """
    if status == "waiting":
        return None
    if status not in STATUSES_WITH_POWER_STATE:
        raise ValueError(f"unknown location status: {status!r}")
    return "not_monitored" if maintenance else status
