"""The Phase 4 location pages: the location page and its one-click switches (D-13, D-05).

- Every view here needs the signed-in admin: LoginRequiredMiddleware denies by default and
  none of them is ``login_not_required``.
- Every action is a POST with CSRF, answered POST -> redirect -> GET with a flash (UI-D4),
  so a reload never repeats it.
- A switch posts its target value, never "toggle" (UI-D3): the same state again writes
  nothing and gets the "already" info flash, so a double click, a second tab or a stale page
  can never flip it back. Each switch changes exactly one flag (D-05).
- No view here does network I/O (KD2). The only admin action that will is the test message
  (04-08).
- Every location URL answers 404 for an unknown or deleted location (UI-SPEC screen H).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from powermon.clock import Clock, SystemClock
from powermon.engine import maintenance
from powermon.locations import validators
from powermon.locations.models import LANGUAGE_CHOICES, Location
from powermon.web.status import location_status

LANGUAGE_LABELS = dict(LANGUAGE_CHOICES)
SWITCH_VALUES = ("on", "off")

# UI-SPEC Copywriting › Switches, verbatim.
MAINTENANCE_HELP = (
    "While on, OFF is not detected, so no OFF alert is sent, and the chart shows the time as "
    "not monitored. Heartbeats are still recorded: if an outage was already in progress, its "
    "ON alert is sent as usual when power returns. Turning maintenance off starts a fresh "
    "detection window."
)
MAINTENANCE_COPY = {
    "on": (
        "Maintenance is on. OFF is not detected and no OFF alert is sent; the chart shows "
        "this time as not monitored."
    ),
    "off": (
        "Maintenance is off. OFF detection starts again now; silence during maintenance does "
        "not count."
    ),
    "already_on": "Maintenance was already on. Nothing changed.",
    "already_off": "Maintenance was already off. Nothing changed.",
}


def location_or_404(pk: int) -> Location:
    """The location with its state row, or 404 when it is unknown or deleted."""
    return get_object_or_404(
        Location.objects.select_related("state"), pk=pk, deleted_at__isnull=True
    )


def settings_context(location: Location) -> dict[str, Any]:
    """The values of the shared settings panel (``web/_settings_panel.html``).

    The bot token only ever goes out masked (SEC-04, D-11).
    """
    return {
        "language_label": LANGUAGE_LABELS[location.language],
        "period_s": location.period_s,
        "grace_s": location.grace_s,
        "off_after_s": location.period_s + location.grace_s,
        "masked_token": validators.mask_token(location.bot_token),
    }


@dataclass(frozen=True)
class SwitchRow:
    """One row of the location page's Switches list, in its current state."""

    # The URL name the row's form posts to.
    url_name: str
    # The current state, e.g. "Maintenance is off".
    heading: str
    # The switch's single effect, the same in both states.
    help: str
    # The action, e.g. "Turn maintenance on".
    button: str
    # The value the form posts: the state the switch moves to (UI-D3).
    target: str


def switch_rows(location: Location) -> list[SwitchRow]:
    """The location page's switches, in UI-SPEC order."""
    on = location.maintenance
    return [
        SwitchRow(
            url_name="location-maintenance",
            heading="Maintenance is on" if on else "Maintenance is off",
            help=MAINTENANCE_HELP,
            button="Turn maintenance off" if on else "Turn maintenance on",
            target="off" if on else "on",
        ),
    ]


class SwitchView(View):
    """POST ``value=on|off``: set one switch of the location, then back to its page.

    A missing or unknown value answers 400 with an empty body and changes nothing (only a
    hand-made request can send one). GET and every other method answer 405.
    """

    http_method_names = ["post"]
    # Tests inject a FakeClock with SomeSwitchView.as_view(clock=...).
    clock: Clock = SystemClock()
    # The flashes: "on" / "off" after a change, "already_on" / "already_off" when not.
    copy: ClassVar[dict[str, str]] = {}

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        """Set the switch; True if it changed, False if it already had that value."""
        raise NotImplementedError

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        value = request.POST.get("value")
        if value not in SWITCH_VALUES:
            return HttpResponseBadRequest()
        location_or_404(pk)
        if self.apply(pk, value == "on", self.clock.now()):
            messages.success(request, self.copy[value])
        else:
            messages.info(request, self.copy[f"already_{value}"])
        return redirect("location-detail", pk=pk)


class MaintenanceSwitchView(SwitchView):
    """``/locations/<pk>/maintenance/``: the maintenance switch (LOC-08, D-02).

    The engine owns the timeline write: the view only calls ``maintenance.set_maintenance``.
    """

    copy = MAINTENANCE_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return maintenance.set_maintenance(pk, on, now)


class LocationDetailView(View):
    """``/locations/<pk>/``: the location page (UI-SPEC screen B, D-13).

    Read-only: the status, the switches, the settings and the way to the device setup. It
    never shows the device key, not even masked, and the bot token only masked (SEC-04).
    """

    template_name = "web/location_detail.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        context = {
            "location": location,
            "status": location_status(location),
            "switch_rows": switch_rows(location),
            **settings_context(location),
        }
        return render(request, self.template_name, context)
