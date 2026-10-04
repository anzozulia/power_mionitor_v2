"""Web views: admin sign-in and sign-out, the location pages, the device heartbeat
endpoint and the health check.

Every admin page relies on LoginRequiredMiddleware; only sign-in, sign-out, ``/hb`` and
``/healthz`` are login-exempt.
"""

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.db import (
    DatabaseError,
    Error,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    connection,
    transaction,
)
from django.db.models.functions import Lower
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import add_never_cache_headers
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.common import no_append_slash
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from powermon.alerts import delivery
from powermon.clock import Clock, SystemClock
from powermon.engine import transitions
from powermon.engine.models import LocationState
from powermon.locations import examples, keys, validators
from powermon.locations.models import LANGUAGE_CHOICES, Location
from powermon.throttle import rules, store
from powermon.web.forms import LocationForm, SignInForm
from powermon.web.status import failing_since_text, location_status

log = logging.getLogger(__name__)

SIGNED_OUT_MESSAGE = "You are signed out."
LOCATION_CREATED_MESSAGE = (
    "Location created. Reveal the key below, then copy an example to the device."
)
LANGUAGE_LABELS = dict(LANGUAGE_CHOICES)
# Before the engine has written anything, a location waits for its first heartbeat.
WAITING = "waiting"


class SignInView(LoginView):
    """``/login/``: the env-defined admin signs in (LOC-01, D-09), throttled per IP (SEC-03).

    LoginView is already login-exempt and CSRF-protected. It honours ``next`` only when
    ``url_has_allowed_host_and_scheme`` accepts it (same host), else it goes to
    LOGIN_REDIRECT_URL. A signed-in admin who opens the page is sent there directly.

    The login throttle (D-16, UI-D13): every failed sign-in POST is recorded against the
    client IP (``throttle.rules.client_ip``). Five within 60 s start a 5-minute cool-down,
    during which every sign-in POST from that IP answers 429 with the throttle message and
    ``Retry-After: 300``, even one with the right password. The throttled page renders an
    unbound form, so ``authenticate()`` never runs and the page cannot tell whether the
    credentials were right (UI rule 6). Throttled POSTs are not recorded, so the cool-down
    cannot be extended. A GET stays a normal page, and a successful sign-in clears its IP's
    failures.
    """

    template_name = "web/login.html"
    authentication_form = SignInForm
    redirect_authenticated_user = True
    # Tests inject a FakeClock with SignInView.as_view(clock=...).
    clock: Clock = SystemClock()
    # Set by post() for form_invalid() and form_valid(): one IP and one time per attempt.
    _client_ip: str
    _attempt_at: datetime

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self._client_ip = rules.client_ip(request.META)
        self._attempt_at = self.clock.now()
        if store.is_blocked(self._client_ip, self._attempt_at):
            return self._throttled(request)
        return super().post(request, *args, **kwargs)

    def _throttled(self, request: HttpRequest) -> HttpResponse:
        """The 429 page: the sign-in form, unbound, with only the throttle callout.

        Unbound, so ``full_clean()`` and ``authenticate()`` never run: the credentials are
        not checked and nothing is recorded. The username keeps its submitted value; the
        password input is never rendered back.
        """
        form: AuthenticationForm = self.get_form_class()(
            request, initial={"username": request.POST.get("username", "")[:150]}
        )
        response = self.render_to_response(
            self.get_context_data(
                form=form,
                throttled=True,
                throttle_message=rules.THROTTLE_MESSAGE,
                # The N11 countdown's start: the same value as the Retry-After header.
                retry_after=rules.RETRY_AFTER,
            )
        )
        response.status_code = 429
        response["Retry-After"] = rules.RETRY_AFTER
        return response

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        # Blank, wrong and inactive credentials all count as one failed sign-in.
        store.record_failure(self._client_ip, self._attempt_at)
        if store.is_blocked(self._client_ip, self._attempt_at):
            # The client IP only: never the username or the password.
            log.warning(
                "sign-in: %s failed sign-ins within %s s from %s; its sign-in POSTs get "
                "429 for %s s",
                rules.MAX_FAILURES,
                int(rules.WINDOW.total_seconds()),
                self._client_ip,
                rules.RETRY_AFTER,
            )
        return super().form_invalid(form)

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        store.clear(self._client_ip)
        return super().form_valid(form)


# P-23: login-exempt, so a signed-out tab's Sign out lands on /login/ and not on
# /login/?next=/logout/, which would GET the POST-only /logout/ (405) after sign-in.
@method_decorator(login_not_required, name="dispatch")
class SignOutView(LogoutView):
    """``/logout/``: POST only (GET is 405), CSRF-protected, then back to the sign-in page.

    The response tells the browser to drop its cache (``Clear-Site-Data: "cache"``), so
    cached admin pages and the private chart image do not outlive the session. It is the
    only response that sends this header.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response = super().post(request, *args, **kwargs)
        # Added after the logout: the session is flushed by then, and the default message
        # storage keeps a short message in its own cookie, so the flash survives.
        messages.info(request, SIGNED_OUT_MESSAGE)
        # R6 as amended by the maintainer on 2026-10-04 (brief §12 Q11); the quotes are
        # part of the value. Only "cache": cookies stay, so the flash above survives.
        response["Clear-Site-Data"] = '"cache"'
        return response


@dataclass(frozen=True)
class LocationRow:
    """One row of the location list (Phase 4 UI-SPEC screen A, D-13, UI-D1)."""

    pk: int
    name: str
    # The Phase 4 status (``status.location_status``): "maintenance" whenever the flag is
    # on, else the stored status "on", "off" or "waiting".
    status: str
    status_label: str
    last_heartbeat_at: datetime | None
    # The Status cell's tags, shown in this order: "Alerts off", "Router grace".
    alerts_off: bool
    router_grace: bool
    # The Delivery cell: None for "OK", else "Failing since {time} ({code})" (D-13, UI-D6).
    delivery: str | None = None


def delivery_text(failing: delivery.Failing | None, now: datetime) -> str | None:
    """The list's Delivery cell text for an open failing incident, or None ("OK").

    ``{time}`` is HH:MM in the display TZ when the incident started today, else with its
    date (UI-D6); ``{code}`` is ``http_{status}`` of the refusal the incident describes.
    """
    if failing is None:
        return None
    since = failing_since_text(failing.started_at, now, settings.TIME_ZONE)
    return f"Failing since {since} (http_{failing.http_status})"


class LocationListView(View):
    """``/``: every location that is not deleted, sorted by name (Phase 4 UI-SPEC screen A).

    Read-only, one server-rendered response with no live refresh: the admin reloads to
    see a new status. Names sort without regard to case, ties by the lower id. The status
    uses the one Phase 4 vocabulary of the admin pages (``status.location_status``), with
    the switch tags after it; the Language column is gone (UI-D1). The last column is the
    delivery health (D-13): "OK", or "Failing since …" while the location's
    ``delivery_failing`` incident is open, the badge's single source (D-10), read for
    every row in one query. There is no pagination (at most about 20 locations). While
    the ops chat is not configured, the page says so (Phase 2 D-09, INV-20).
    """

    template_name = "web/location_list.html"
    # Tests inject a FakeClock with LocationListView.as_view(clock=...): "today" (UI-D6).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest) -> HttpResponse:
        locations = list(
            Location.objects.filter(deleted_at__isnull=True)
            .select_related("state")
            .order_by(Lower("name"), "pk")
        )
        failing = delivery.failing_incidents([location.pk for location in locations])
        now = self.clock.now()
        rows = []
        for location in locations:
            status = location_status(location)
            rows.append(
                LocationRow(
                    pk=location.pk,
                    name=location.name,
                    status=status.key,
                    status_label=status.label,
                    last_heartbeat_at=status.last_heartbeat_at,
                    alerts_off=not location.alerts_enabled,
                    router_grace=location.router_grace,
                    delivery=delivery_text(failing.get(location.pk), now),
                )
            )
        context = {"rows": rows, "ops_configured": settings.CFG.ops_configured}
        return render(request, self.template_name, context)


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
        # Instructive, so sticky (UI-09): it says what to do next on the setup page.
        messages.success(request, LOCATION_CREATED_MESSAGE, extra_tags="sticky")
        return redirect("location-setup", pk=location.pk)


SETUP_TEMPLATE = "web/location_setup.html"


def render_setup(request: HttpRequest, pk: int, *, revealed: bool) -> HttpResponse:
    """The device setup page of the location (UI-SPEC screen E): masked, or revealed.

    Shared by the setup view (GET masked, the Reveal POST revealed) and the Regenerate
    POST (D-14), which answers with this page revealed. Revealed, it carries the full
    device key (SEC-04), so the response is always ``Cache-Control: no-store``, whatever
    the caller. 404 for an unknown or deleted location. The meta line uses the one Phase 4
    status vocabulary (``status.location_status``: Maintenance whenever the flag is on).
    The examples are the exact strings the 01-09 generators return, the ones the INV-24 #3
    test runs verbatim against ``/hb``.
    """
    location = get_object_or_404(
        Location.objects.select_related("state"), pk=pk, deleted_at__isnull=True
    )
    key = location.device_key
    shown_key = key if revealed else keys.mask_key(key)
    # The configured base URL only, never the request's Host header.
    url = examples.heartbeat_url(settings.PUBLIC_BASE_URL)
    # Only the public bot id before the colon, the digits the mask shows, enters the
    # context; never the secret part (R3). A token without a colon has no public part.
    bot_id, colon, _secret = location.bot_token.partition(":")
    context = {
        "location": location,
        "status": location_status(location),
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
        "token_bot_id": bot_id if colon else "",
    }
    response = render(request, SETUP_TEMPLATE, context)
    add_never_cache_headers(response)
    return response


@method_decorator(never_cache, name="dispatch")
class LocationSetupView(View):
    """``/locations/<pk>/setup/``: device setup (UI-SPEC screen E; LOC-05, HB-01, D-11).

    GET shows the device key masked. POST (CSRF) is the explicit reveal, answered with the
    revealed page itself (200, no redirect). Only it and the Regenerate POST ever carry
    the full key. Every response is ``Cache-Control: no-store``, so neither the Back
    button nor a cache shows a revealed key again.
    """

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return render_setup(request, pk, revealed=False)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        return render_setup(request, pk, revealed=True)


# D-07: exactly 32 characters from [A-Za-z0-9]. Explicit ASCII classes with fullmatch:
# \w would accept non-ASCII letters and digits, and $ a trailing newline (P-10).
KEY_RE = re.compile(r"[A-Za-z0-9]{32}")


def _extract_key(request: HttpRequest) -> str | None:
    """The key from ``Authorization: Bearer <key>``, else from ``?key=``; None if malformed."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    key = value.strip() if scheme.lower() == "bearer" else request.GET.get("key", "")
    return key if KEY_RE.fullmatch(key) else None


def _db_unreachable(exc: Error) -> bool:
    """True when ``exc`` means the database cannot be reached, not that the code is wrong.

    OperationalError covers a refused or dropped connection and a statement_timeout
    (QueryCanceled); InterfaceError a connection the driver can no longer use. psycopg
    raises ProgrammingError ("can't change 'autocommit' now: connection in transaction
    status UNKNOWN") when atomic() meets a connection that already died, so a
    ProgrammingError counts only while this thread's connection is gone. Any other
    database error (IntegrityError, DataError, a genuine ProgrammingError) is a bug.
    """
    if isinstance(exc, OperationalError | InterfaceError):
        return True
    if isinstance(exc, ProgrammingError):
        raw = connection.connection
        return raw is None or bool(raw.closed)
    return False


class _DbOutageLog:
    """One WARNING when heartbeats start failing on the database, one when it answers again.

    Per process (D-16): a device beats every few seconds, so a WARNING or a traceback per
    failed heartbeat would flood the log during an outage. gunicorn's gthread workers
    serve heartbeats on several threads, hence the lock. Only an outage
    (``_db_unreachable``) sets the flag; a bug never touches it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failing = False

    def failed(self, exc: Error) -> None:
        with self._lock:
            first = not self._failing
            self._failing = True
        if first:
            # The class name only: a driver message can carry the host, the user and
            # more (OPS-08).
            log.warning(
                "heartbeat: database unavailable (%s); answering 503 until it is back",
                type(exc).__name__,
            )

    def ok(self) -> None:
        with self._lock:
            recovered = self._failing
            self._failing = False
        if recovered:
            log.warning("heartbeat: database reachable again")


_HEARTBEAT_DB = _DbOutageLog()


def _db_unavailable() -> HttpResponse:
    """503 ``db unavailable``; the device simply retries on its next period."""
    response = HttpResponse("db unavailable", status=503, content_type="text/plain")
    # Django's log_response skips a response with this flag. Without it, django.request
    # writes one "Service Unavailable: /hb" ERROR line per heartbeat, the flood D-16 removes.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


@method_decorator([login_not_required, csrf_exempt, no_append_slash], name="dispatch")
class HeartbeatView(View):
    """``/hb``: a device reports that mains power (and internet) is up (HB-01, HB-02, D-06).

    GET and POST behave the same: 200 ``ok`` for a valid key, 401 for a missing, malformed
    or unknown key, 405 for any other method, never a redirect. The key is looked up before
    any write, a malformed key costs no query, and the request does no network I/O (KD2).
    While the database cannot be reached, the answer is 503 ``db unavailable`` with one
    WARNING per outage per process (D-16). Any other database error is a bug: it
    propagates, so Django answers 500 and logs the traceback through the redacting
    formatter, and the outage flag stays as it was.
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
        try:
            location_id = (
                None
                if key is None
                else Location.objects.filter(device_key=key, deleted_at__isnull=True)
                .values_list("id", flat=True)
                .first()
            )
            result = None if location_id is None else transitions.record_heartbeat(location_id, now)
        except Error as exc:
            if not _db_unreachable(exc):
                # A bug, not an outage (Django's 500 path logs it once per request).
                raise
            # No exception text and never the key: driver errors can carry connection
            # details (OPS-08).
            _HEARTBEAT_DB.failed(exc)
            return _db_unavailable()
        if location_id is None:
            # Nothing is written for a rejected request (HB-02). Never log the key.
            log.debug("heartbeat rejected: missing, malformed or unknown key")
            response = HttpResponse("unauthorized", status=401, content_type="text/plain")
            response["WWW-Authenticate"] = 'Bearer realm="heartbeat"'
            return response
        _HEARTBEAT_DB.ok()
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
