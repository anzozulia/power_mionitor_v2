"""The one Phase 4 status vocabulary of the admin pages (UI-SPEC Page Shell › Status vocabulary).

Maintenance is a flag, not a status (ARCHITECTURE › Location Status Machine), but the admin
sees it as one: "Maintenance" whenever the flag is on, else the stored status ("On", "Off"
or "Waiting for first heartbeat"). A location with no state row counts as waiting (Phase 1
behaviour). The stored status underneath maintenance stays visible as the power state.

Delivery health has one text here too: the list's "Failing since {time}" (UI-D6).
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from powermon.engine.models import LocationState
from powermon.locations.models import Location

STATUS_LABELS = {
    "on": "On",
    "off": "Off",
    "waiting": "Waiting for first heartbeat",
    "maintenance": "Maintenance",
}
# Before the engine has written anything, a location waits for its first heartbeat.
WAITING = "waiting"
MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class LocationStatus:
    """What a status label, its dot and the status panel show for one location."""

    # "maintenance" whenever the flag is on, else the stored status.
    key: str
    label: str
    # The stored status underneath: "on", "off" or "waiting".
    power_key: str
    power_label: str
    last_heartbeat_at: datetime | None
    on_since: datetime | None
    outage_started_at: datetime | None


def location_status(location: Location) -> LocationStatus:
    """The location's status in the Phase 4 vocabulary, from its (prefetched) state row."""
    state: LocationState | None = getattr(location, "state", None)
    power_key = WAITING if state is None else state.status
    key = MAINTENANCE if location.maintenance else power_key
    return LocationStatus(
        key=key,
        label=STATUS_LABELS[key],
        power_key=power_key,
        power_label=STATUS_LABELS[power_key],
        last_heartbeat_at=None if state is None else state.last_heartbeat_at,
        on_since=None if state is None else state.on_since,
        outage_started_at=None if state is None else state.outage_started_at,
    )


def failing_since_text(started_at: datetime, now: datetime, tz: str) -> str:
    """The time in the list's "Failing since {time} ({code})" (D-13, UI-D6).

    ``HH:MM`` in the display TZ ``tz`` when the incident started on the local date of
    ``now``, else ``YYYY-MM-DD HH:MM``: a bot can stay removed for days, and a bare time
    would then point to the wrong day. Seconds are cut off, never rounded. A naive
    ``started_at`` or ``now`` has no defined instant: ValueError.
    """
    if started_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    zone = ZoneInfo(tz)
    local = started_at.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return local.strftime("%H:%M")
    return local.strftime("%Y-%m-%d %H:%M")
