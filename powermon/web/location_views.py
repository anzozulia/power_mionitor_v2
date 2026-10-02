"""The Phase 4 location pages: the location page, its one-click switches, the edit form and
the key rotation (D-13, D-05, D-07, D-14).

- Every view here needs the signed-in admin: LoginRequiredMiddleware denies by default and
  none of them is ``login_not_required``.
- Every switch is a POST with CSRF, answered POST -> redirect -> GET with a flash (UI-D4),
  so a reload never repeats it. The edit save is too. Regenerate key is a GET
  confirmation, then a POST answered with the revealed setup page itself (D-14, D-17),
  guarded against a resubmit (UI-D7).
- The edit save writes only the configuration columns, through ``actions.update_config``,
  never ``form.save()`` or ``Location.save()``: a form loaded earlier can never revert the
  status, a switch or the device key (D-07, INV-02 #3).
- A switch posts its target value, never "toggle" (UI-D3): the same state again writes
  nothing and gets the "already" info flash, so a double click, a second tab or a stale page
  can never flip it back. Each switch changes exactly one flag (D-05).
- No view here does network I/O (KD2). The only admin action that will is the test message
  (04-08).
- Every location URL answers 404 for an unknown or deleted location (UI-SPEC screen H).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from powermon.clock import Clock, SystemClock
from powermon.engine import maintenance
from powermon.locations import actions, validators
from powermon.locations.models import LANGUAGE_CHOICES, Location
from powermon.web import views
from powermon.web.forms import LocationEditForm
from powermon.web.status import location_status

log = logging.getLogger(__name__)

LANGUAGE_LABELS = dict(LANGUAGE_CHOICES)
SWITCH_VALUES = ("on", "off")

# UI-D7: the regenerate form's marker is a salted HMAC (SECRET_KEY) of the key it replaces.
REGENERATE_SALT = "powermon.regenerate-key"
# UI-SPEC Copywriting › Device setup and key regeneration, verbatim.
REGENERATED_MESSAGE = (
    "New key saved. The old key no longer works. Copy the new key or an example below to "
    "the device."
)
ALREADY_REGENERATED_MESSAGE = "The key was already regenerated. The key below is the current one."

# UI-SPEC Copywriting › Edit form, verbatim.
CHANGES_SAVED_MESSAGE = "Changes saved."
CHANNEL_CHANGED_MESSAGE = (
    "Changes saved. The weekly chart is posted again with the new bot or chat. The old one "
    "is unpinned if this location's bot is an admin of the old channel."
)

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
ALERTS_HELP = (
    "While off, subscribers get no new alerts, and none are saved for later. Alerts already "
    "queued still go out. The chart, its 15-minute refresh and the midnight re-pin carry on."
)
ALERTS_COPY = {
    "on": "Alerts are on. Subscribers get alerts for changes recorded from now on.",
    "off": (
        "Alerts are off. Subscribers get no new alerts; alerts already queued still go out. "
        "The chart keeps updating."
    ),
    "already_on": "Alerts were already on. Nothing changed.",
    "already_off": "Alerts were already off. Nothing changed.",
}
ROUTER_GRACE_HELP = (
    "While on, OFF waits 180 seconds longer when the last heartbeat came within 5 minutes "
    "after power returned, so a router that restarts after a blackout is not reported as a "
    "second outage. It changes only decisions made from now on."
)
# "off" names the location's plain timeout: {off_after_s} is period + grace, an integer.
ROUTER_GRACE_COPY = {
    "on": (
        "Router grace is on. From now on, OFF waits 180 seconds longer right after power returns."
    ),
    "off": (
        "Router grace is off. From now on, OFF is reported after {off_after_s} seconds "
        "without a heartbeat."
    ),
    "already_on": "Router grace was already on. Nothing changed.",
    "already_off": "Router grace was already off. Nothing changed.",
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
    """The location page's switches, in UI-SPEC order (D-05): Maintenance, Alerts, Router grace."""
    maintenance_on = location.maintenance
    alerts_on = location.alerts_enabled
    grace_on = location.router_grace
    return [
        SwitchRow(
            url_name="location-maintenance",
            heading="Maintenance is on" if maintenance_on else "Maintenance is off",
            help=MAINTENANCE_HELP,
            button="Turn maintenance off" if maintenance_on else "Turn maintenance on",
            target="off" if maintenance_on else "on",
        ),
        SwitchRow(
            url_name="location-alerts",
            heading="Alerts are on" if alerts_on else "Alerts are off",
            help=ALERTS_HELP,
            button="Turn alerts off" if alerts_on else "Turn alerts on",
            target="off" if alerts_on else "on",
        ),
        SwitchRow(
            url_name="location-router-grace",
            heading="Router grace is on" if grace_on else "Router grace is off",
            help=ROUTER_GRACE_HELP,
            button="Turn router grace off" if grace_on else "Turn router grace on",
            target="off" if grace_on else "on",
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

    def flash(self, location: Location, key: str) -> str:
        """The flash for ``key`` ("on", "off", "already_on", "already_off")."""
        return self.copy[key]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        value = request.POST.get("value")
        if value not in SWITCH_VALUES:
            return HttpResponseBadRequest()
        location = location_or_404(pk)
        if self.apply(pk, value == "on", self.clock.now()):
            messages.success(request, self.flash(location, value))
        else:
            messages.info(request, self.flash(location, f"already_{value}"))
        return redirect("location-detail", pk=pk)


class MaintenanceSwitchView(SwitchView):
    """``/locations/<pk>/maintenance/``: the maintenance switch (LOC-08, D-02).

    The engine owns the timeline write: the view only calls ``maintenance.set_maintenance``.
    """

    copy = MAINTENANCE_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return maintenance.set_maintenance(pk, on, now)


class AlertsSwitchView(SwitchView):
    """``/locations/<pk>/alerts/``: the alerts switch (LOC-10, D-05, D-06).

    A configuration-only write (``actions.set_flag``). With alerts off, a transition
    recorded from then on queues no alert and nothing is held for later; alerts already
    queued still go out, and the chart carries on (INV-05).
    """

    copy = ALERTS_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return actions.set_flag(pk, "alerts_enabled", on)


class RouterGraceSwitchView(SwitchView):
    """``/locations/<pk>/router-grace/``: the router-reconnect grace switch (LOC-09, D-05).

    A configuration-only write (``actions.set_flag``): no state row lock and no
    ``state_version`` bump, so a detector snapshot read before the click keeps its
    decision, and every cycle after it reads the new setting (K-4). An OFF already
    recorded keeps its start and its totals (INV-05, INV-06).
    """

    copy = ROUTER_GRACE_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return actions.set_flag(pk, "router_grace", on)

    def flash(self, location: Location, key: str) -> str:
        # Integers only in a flash: the location's period + grace in seconds.
        return self.copy[key].format(off_after_s=location.period_s + location.grace_s)


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


def stored_settings(location: Location) -> dict[str, Any]:
    """The edit form's initial values: the stored configuration, never the bot token."""
    return {
        "name": location.name,
        "period_s": location.period_s,
        "grace_s": location.grace_s,
        "chat_id": location.chat_id,
        "language": location.language,
    }


class LocationEditView(View):
    """``/locations/<pk>/edit/``: edit the location's settings (LOC-04, D-07, D-08; screen C).

    GET shows the form with the stored values; "New bot token" is always empty and its
    help shows the current token masked (SEC-04). An invalid POST answers 200 with the
    form, every value kept except the token; the title and the breadcrumbs keep the stored
    name. A valid POST is one ``actions.update_config``, the view's only write, then a
    redirect to the location page with "Changes saved." (UI-D4), extended by the channel
    sentence when the chat ID or the token changed (D-08): the save made that location's
    queued alerts due, and the worker moves the chart. No network I/O (KD2).
    """

    template_name = "web/location_edit.html"
    # Tests inject a FakeClock with LocationEditView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        form = LocationEditForm(initial=stored_settings(location), current_token=location.bot_token)
        return self._render(request, location, form)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        form = LocationEditForm(request.POST, current_token=location.bot_token)
        if not form.is_valid():
            return self._render(request, location, form)
        result = actions.update_config(pk, form.cleaned_data, self.clock.now())
        if not result.found:
            # Deleted between the lookup and the save: nothing was written.
            raise Http404
        # The id and a flag only: never a token or a chat ID (OPS-08).
        log.info("settings saved for location %s (channel changed: %s)", pk, result.channel_changed)
        # A chat or token change also moves the chart: the flash says so (D-08).
        flash = CHANNEL_CHANGED_MESSAGE if result.channel_changed else CHANGES_SAVED_MESSAGE
        messages.success(request, flash)
        return redirect("location-detail", pk=pk)

    def _render(
        self, request: HttpRequest, location: Location, form: LocationEditForm
    ) -> HttpResponse:
        return render(request, self.template_name, {"location": location, "form": form})


def regenerate_marker(key: str) -> str:
    """The UI-D7 marker of ``key``: a salted HMAC-SHA256 (hex), with no key characters.

    The confirmation form carries it, and the POST regenerates only while it is still the
    marker of the location's current key. It is exact (unlike the last 4 characters, which
    two keys can share) and reveals nothing about the key (T-04-18).
    """
    return salted_hmac(REGENERATE_SALT, key, algorithm="sha256").hexdigest()


def regenerate_block(location: Location) -> str:
    """The one D-15 state block of the regenerate confirmation (UI-SPEC screen F).

    "maintenance" whenever the flag is on; else by the stored status: "warning" (on: an
    OFF can be recorded while the device has the old key), "off" or "waiting".
    """
    if location.maintenance:
        return "maintenance"
    power_key = location_status(location).power_key
    return {"on": "warning", "off": "off"}.get(power_key, "waiting")


@method_decorator(never_cache, name="dispatch")
class RegenerateKeyView(View):
    """``/locations/<pk>/setup/regenerate/``: rotate the device key (LOC-06, D-14, D-15, D-17).

    GET is the confirmation page (UI-SPEC screen F): it changes nothing, shows exactly one
    D-15 state block and one form, the destructive POST, and never the key, not even masked.
    The form's hidden marker is ``regenerate_marker`` of the key it replaces (UI-D7).

    POST regenerates only while the posted marker is the current key's
    (``constant_time_compare``) and the conditional UPDATE still finds that key
    (``actions.regenerate_key``). Either way it answers with the setup page revealed:
    200, ``Cache-Control: no-store``, with the new key and the success flash, or, for a
    resubmitted, stale or raced POST, with the current key and the UI-D7 info flash. So a
    reload or a double click never replaces the key a second time, and the response is
    the only one besides Reveal that carries the full key (SEC-04). No network I/O (KD2).
    """

    template_name = "web/location_regenerate.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        context = {
            "location": location,
            # Not "block": inside {% block %} the template engine binds that name itself.
            "state_block": regenerate_block(location),
            "off_after_s": location.period_s + location.grace_s,
            "marker": regenerate_marker(location.device_key),
        }
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        current = location_or_404(pk).device_key
        posted = request.POST.get("marker", "")
        if constant_time_compare(posted, regenerate_marker(current)) and actions.regenerate_key(
            pk, current
        ):
            # The id only: never a key (OPS-08).
            log.info("device key regenerated for location %s", pk)
            messages.success(request, REGENERATED_MESSAGE)
        else:
            messages.info(request, ALREADY_REGENERATED_MESSAGE)
        return views.render_setup(request, pk, revealed=True)
