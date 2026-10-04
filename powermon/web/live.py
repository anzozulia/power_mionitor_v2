"""Live status of every location: one row source for the list, the sidebar and the status
JSON (UI-04, UI-05, R4).

- ``live_rows(now)`` runs the location list's two queries (the non-deleted locations with
  their state rows, sorted by name without regard to case, ties by the lower id, then one
  query for every open ``delivery_failing`` incident; none for an empty list) and turns
  each location into a frozen ``LiveRow``. The status vocabulary is the one of the admin
  pages (``status.location_status``) and the delivery text the list's own
  (``views.delivery_text``), so every surface agrees.
- ``fleet_counts(rows)`` counts each location once under its status key and once more
  under ``failing`` while its delivery is failing: an Off location with failing delivery
  counts as off and as failing (UI-04).
- ``GET /locations/status.json`` (``LocationStatusJsonView``) is the live-refresh payload
  (UI-05). It carries fixed vocabulary, times formatted from stored instants and short
  codes only: never a location name, a chat ID, a bot token or its mask, a device key, its
  mask or its tail, nor any Telegram text (R4, R10). Locations are keyed by their id, so a
  consumer never relies on order. The view is login-required (default-deny), GET only and
  never cached; it touches neither the messages nor a template, so it sets no cookie,
  runs no context processor and a pending flash survives a poll.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models.functions import Lower
from django.http import HttpRequest, JsonResponse
from django.utils.cache import patch_vary_headers
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from powermon.alerts import delivery
from powermon.clock import Clock, SystemClock
from powermon.locations.models import Location

# The module, not the function: views may import this module in turn (the list view moves
# to live_rows later), and a module reference keeps that import cycle-safe.
from powermon.web import views
from powermon.web.status import location_status
from powermon.web.templatetags.display_time import display_time, display_time_compact

# The fleet count keys, in the fleet tiles' order (= the tiles' data-metric values).
FLEET_KEYS = ("on", "off", "maintenance", "waiting", "failing")
# The Delivery cell while no delivery_failing incident is open.
DELIVERY_OK = "OK"


@dataclass(frozen=True)
class LiveRow:
    """One location's live status: the list row's fields plus what the live surfaces need."""

    pk: int
    name: str
    # The Phase 4 status key: "maintenance" whenever the flag is on, else "on", "off" or
    # "waiting" (``status.location_status``).
    status: str
    status_label: str
    last_heartbeat_at: datetime | None
    alerts_off: bool
    router_grace: bool
    # "Failing since {time} ({code})" while delivery is failing, else None ("OK").
    delivery: str | None
    # The stored power state underneath maintenance: "on", "off" or "waiting".
    power: str
    on_since: datetime | None
    outage_started_at: datetime | None
    delivery_failing: bool


def live_rows(now: datetime) -> list[LiveRow]:
    """Every non-deleted location's live row, in list order, from exactly two queries.

    ``now`` decides only the delivery text's "today" (UI-D6).
    """
    locations = list(
        Location.objects.filter(deleted_at__isnull=True)
        .select_related("state")
        .order_by(Lower("name"), "pk")
    )
    failing = delivery.failing_incidents([location.pk for location in locations])
    rows = []
    for location in locations:
        status = location_status(location)
        incident = failing.get(location.pk)
        rows.append(
            LiveRow(
                pk=location.pk,
                name=location.name,
                status=status.key,
                status_label=status.label,
                last_heartbeat_at=status.last_heartbeat_at,
                alerts_off=not location.alerts_enabled,
                router_grace=location.router_grace,
                delivery=views.delivery_text(incident, now),
                power=status.power_key,
                on_since=status.on_since,
                outage_started_at=status.outage_started_at,
                delivery_failing=incident is not None,
            )
        )
    return rows


def fleet_counts(rows: Iterable[LiveRow]) -> dict[str, int]:
    """How many locations have each status, and how many have failing delivery (UI-04).

    Every key is present, zero included. A location counts once under its status and once
    more under ``failing`` while its delivery is failing.
    """
    counts = dict.fromkeys(FLEET_KEYS, 0)
    for row in rows:
        counts[row.status] += 1
        if row.delivery_failing:
            counts["failing"] += 1
    return counts


def iso_local(value: datetime) -> str:
    """ISO 8601 with the offset, to the second, in the display TZ.

    A naive datetime has no defined instant: ValueError.
    """
    if value.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return value.astimezone(ZoneInfo(settings.TIME_ZONE)).isoformat(timespec="seconds")


def _instant(value: datetime) -> dict[str, str]:
    """One stored instant as the page shows it: ISO, full and compact display strings."""
    return {
        "iso": iso_local(value),
        "display": display_time(value),
        "compact": display_time_compact(value),
    }


def _since(row: LiveRow) -> dict[str, str] | None:
    """Since when the location is on, or since when its outage runs; None while waiting."""
    if row.power == "on" and row.on_since is not None:
        return {"kind": "on", **_instant(row.on_since)}
    if row.power == "off" and row.outage_started_at is not None:
        return {"kind": "outage", **_instant(row.outage_started_at)}
    return None


def status_payload(row: LiveRow) -> dict[str, Any]:
    """One location's entry in the status JSON: fixed keys, no name and no secret."""
    last_heartbeat = None if row.last_heartbeat_at is None else _instant(row.last_heartbeat_at)
    # The text is None exactly when no incident is open (``views.delivery_text``).
    if row.delivery is None:
        delivery_state = {"state": "ok", "text": DELIVERY_OK}
    else:
        delivery_state = {"state": "failing", "text": row.delivery}
    return {
        "status": row.status,
        "label": row.status_label,
        "power": row.power,
        "last_heartbeat": last_heartbeat,
        "since": _since(row),
        "delivery": delivery_state,
    }


@method_decorator(never_cache, name="dispatch")
class LocationStatusJsonView(View):
    """``GET /locations/status.json``: the live status of every location (UI-05, UI-04).

    200 ``application/json`` with ``generated_at``, ``ops_configured``, the fleet
    ``counts`` and ``locations`` keyed by id. Login-required; every other method, HEAD
    included, answers 405. ``Cache-Control: no-store, private`` and ``Vary: Cookie``. The
    query string is ignored. Two queries whatever the number of locations; nothing is
    written.
    """

    http_method_names = ["get"]
    # Tests inject a FakeClock with LocationStatusJsonView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest) -> JsonResponse:
        now = self.clock.now()
        rows = live_rows(now)
        payload = {
            "generated_at": iso_local(now),
            "ops_configured": settings.CFG.ops_configured,
            "counts": fleet_counts(rows),
            "locations": {str(row.pk): status_payload(row) for row in rows},
        }
        response = JsonResponse(payload)
        # Explicit, so the header never depends on whether a middleware read the session.
        patch_vary_headers(response, ("Cookie",))
        return response
