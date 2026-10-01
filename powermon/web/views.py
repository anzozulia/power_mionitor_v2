"""Web views: admin sign-in and sign-out, the device heartbeat endpoint and the health check."""

import logging
import re
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.common import no_append_slash
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from powermon.clock import Clock, SystemClock
from powermon.engine import transitions
from powermon.locations.models import Location
from powermon.web.forms import SignInForm

log = logging.getLogger(__name__)

SIGNED_OUT_MESSAGE = "You are signed out."


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
