"""Web views: admin sign-in and sign-out, the location pages, the device heartbeat
endpoint and the health check.

Every admin page relies on LoginRequiredMiddleware; only sign-in, sign-out, ``/hb`` and
``/healthz`` are login-exempt.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import DatabaseError, connection, transaction
from django.db.models.functions import Lower
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.common import no_append_slash
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from powermon.clock import Clock, SystemClock
from powermon.engine import transitions
from powermon.engine.models import LocationState
from powermon.locations import examples, keys, validators
from powermon.locations.models import LANGUAGE_CHOICES, Location
from powermon.web.forms import LocationForm, SignInForm

log = logging.getLogger(__name__)

SIGNED_OUT_MESSAGE = "You are signed out."
LOCATION_CREATED_MESSAGE = (
    "Location created. Reveal the key below, then copy an example to the device."
)
STATUS_LABELS = {"waiting": "Waiting for first heartbeat", "on": "On", "off": "Off"}
LANGUAGE_LABELS = dict(LANGUAGE_CHOICES)
# Before the engine has written anything, a location waits for its first heartbeat.
WAITING = "waiting"


class SignInView(LoginView):
    """``/login/``: the env-defined admin signs in (LOC-01, D-09).

    LoginView is already login-exempt and CSRF-protected. It honours ``next`` only when
    ``url_has_allowed_host_and_scheme`` accepts it (same host), else it goes to
    LOGIN_REDIRECT_URL. A signed-in admin who opens the page is sent there directly.
    """

    template_name = "web/login.html"
    authentication_form = SignInForm
    redirect_authenticated_user = True


# P-23: login-exempt, so a signed-out tab's Sign out lands on /login/ and not on
# /login/?next=/logout/, which would GET the POST-only /logout/ (405) after sign-in.
@method_decorator(login_not_required, name="dispatch")
class SignOutView(LogoutView):
    """``/logout/``: POST only (GET is 405), CSRF-protected, then back to the sign-in page."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response = super().post(request, *args, **kwargs)
        # Added after the logout: the session is flushed by then, and the default message
        # storage keeps a short message in its own cookie, so the flash survives.
        messages.info(request, SIGNED_OUT_MESSAGE)
        return response


@dataclass(frozen=True)
class LocationRow:
    """One row of the location list."""

    pk: int
    name: str
    status: str
    status_label: str
    last_heartbeat_at: datetime | None
    language_label: str


def _state_of(location: Location) -> tuple[str, datetime | None]:
    """The status and last heartbeat time; a location with no state row counts as waiting."""
    state: LocationState | None = getattr(location, "state", None)
    if state is None:
        return WAITING, None
    return state.status, state.last_heartbeat_at


class LocationListView(View):
    """``/``: every location that is not deleted, sorted by name (UI-SPEC screen 2, D-09).

    Read-only, one server-rendered response with no live refresh: the admin reloads to
    see a new status. There is no pagination (at most about 20 locations).
    """

    template_name = "web/location_list.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        locations = (
            Location.objects.filter(deleted_at__isnull=True)
            .select_related("state")
            .order_by(Lower("name"), "pk")
        )
        rows = []
        for location in locations:
            status, last_heartbeat_at = _state_of(location)
            rows.append(
                LocationRow(
                    pk=location.pk,
                    name=location.name,
                    status=status,
                    status_label=STATUS_LABELS[status],
                    last_heartbeat_at=last_heartbeat_at,
                    language_label=LANGUAGE_LABELS[location.language],
                )
            )
        return render(request, self.template_name, {"rows": rows})


def _create_location(data: dict[str, Any], now: datetime) -> Location:
    """The location and its waiting state in one transaction: both rows or neither.

    No network I/O: nothing is sent to Telegram on save (D-12).
    """
    with transaction.atomic():
        location = Location.objects.create(
            name=data["name"],
            period_s=data["period_s"],
            grace_s=data["grace_s"],
            bot_token=data["bot_token"],
            chat_id=data["chat_id"],
            language=data["language"],
            device_key=keys.generate_device_key(),
            created_at=now,
        )
        LocationState.objects.create(location=location, status=WAITING)
    return location


class LocationCreateView(View):
    """``/locations/new/``: the add-location form (UI-SPEC screen 3; LOC-02, D-10, D-12).

    A valid POST creates the location and redirects to its setup page with the success
    flash (POST, redirect, GET). An invalid POST re-renders the form with its errors; the
    bot token input comes back empty. A double submit creates a second location, which
    stays waiting and sends nothing.
    """

    template_name = "web/location_form.html"
    # Tests inject a FakeClock with LocationCreateView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, self.template_name, {"form": LocationForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        form = LocationForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})
        location = _create_location(form.cleaned_data, self.clock.now())
        messages.success(request, LOCATION_CREATED_MESSAGE)
        return redirect("location-setup", pk=location.pk)


@method_decorator(never_cache, name="dispatch")
class LocationSetupView(View):
    """``/locations/<pk>/setup/``: device setup (UI-SPEC screen 4; LOC-05, HB-01, D-11).

    GET shows the device key masked. POST (CSRF) is the explicit reveal, answered with the
    revealed page itself (200, no redirect): the only response that ever carries the full
    key. Every response is ``Cache-Control: no-store``, so neither the Back button nor a
    cache shows a revealed key again. The examples are the exact strings the 01-09
    generators return, the ones the INV-24 #3 test runs verbatim against ``/hb``.
    """

    template_name = "web/location_setup.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return self._render(request, pk, revealed=False)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        return self._render(request, pk, revealed=True)

    def _render(self, request: HttpRequest, pk: int, *, revealed: bool) -> HttpResponse:
        location = get_object_or_404(
            Location.objects.select_related("state"), pk=pk, deleted_at__isnull=True
        )
        status, last_heartbeat_at = _state_of(location)
        key = location.device_key
        shown_key = key if revealed else keys.mask_key(key)
        # The configured base URL only, never the request's Host header.
        url = examples.heartbeat_url(settings.PUBLIC_BASE_URL)
        context = {
            "location": location,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "last_heartbeat_at": last_heartbeat_at,
            "language_label": LANGUAGE_LABELS[location.language],
            "revealed": revealed,
            "shown_key": shown_key,
            "key_tail": key[-keys.MASK_VISIBLE :],
            "heartbeat_url": url,
            "curl_example": examples.curl_cmd(url, shown_key, multiline=True),
            "cron_example": "\n".join(examples.cron_lines(url, shown_key, location.period_s)),
            "wget_gnu_example": examples.wget_gnu(url, shown_key),
            "wget_busybox_example": examples.wget_busybox(url, shown_key),
            "period_s": location.period_s,
            "grace_s": location.grace_s,
            "off_after_s": location.period_s + location.grace_s,
            "masked_token": validators.mask_token(location.bot_token),
        }
        return render(request, self.template_name, context)


# D-07: exactly 32 characters from [A-Za-z0-9]. Explicit ASCII classes with fullmatch:
# \w would accept non-ASCII letters and digits, and $ a trailing newline (P-10).
KEY_RE = re.compile(r"[A-Za-z0-9]{32}")


def _extract_key(request: HttpRequest) -> str | None:
    """The key from ``Authorization: Bearer <key>``, else from ``?key=``; None if malformed."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    key = value.strip() if scheme.lower() == "bearer" else request.GET.get("key", "")
    return key if KEY_RE.fullmatch(key) else None


@method_decorator([login_not_required, csrf_exempt, no_append_slash], name="dispatch")
class HeartbeatView(View):
    """``/hb``: a device reports that mains power (and internet) is up (HB-01, HB-02, D-06).

    GET and POST behave the same: 200 ``ok`` for a valid key, 401 for a missing, malformed
    or unknown key, 405 for any other method, never a redirect. The key is looked up before
    any write, a malformed key costs no query, and the request does no network I/O (KD2).
    """

    # Everything else, HEAD and OPTIONS included, gets 405.
    http_method_names = ["get", "post"]
    # Tests inject a FakeClock with HeartbeatView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._beat(request)

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._beat(request)

    def _beat(self, request: HttpRequest) -> HttpResponse:
        # The server receive time is the only timestamp; device clocks never matter.
        now = self.clock.now()
        key = _extract_key(request)
        location_id = (
            None
            if key is None
            else Location.objects.filter(device_key=key, deleted_at__isnull=True)
            .values_list("id", flat=True)
            .first()
        )
        if location_id is None:
            # Nothing is written for a rejected request (HB-02). Never log the key.
            log.debug("heartbeat rejected: missing, malformed or unknown key")
            response = HttpResponse("unauthorized", status=401, content_type="text/plain")
            response["WWW-Authenticate"] = 'Bearer realm="heartbeat"'
            return response
        result = transitions.record_heartbeat(location_id, now)
        log.debug("heartbeat: location %s %s", location_id, result)
        return HttpResponse("ok", content_type="text/plain")


@login_not_required
@require_GET
@never_cache
def healthz(request: HttpRequest) -> HttpResponse:
    """Container health check: 200 ``ok`` after one real query, 503 if the database fails."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        # No exception text: driver errors can carry connection details.
        log.warning("healthz: database query failed")
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
