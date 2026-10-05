"""Test-only URLconf with one view that raises, for E3 500 through the whole middleware stack.

Used with ``@pytest.mark.urls("urls_raise")`` and ``Client(raise_request_exception=False)``:
Django's own 500 handler then renders 500.html inside the middleware, so the response carries
the CSP header (TEST-STRATEGY §7.3). Calling ``server_error`` directly bypasses the middleware.
The exception text is distinctive so a test can prove the page never echoes it (R11).
"""

from django.http import HttpRequest, HttpResponse
from django.urls import path

RAISE_PATH = "/raise/"
EXCEPTION_MESSAGE = "E3 probe failure <b>echo-me</b> /secret/path"


def explode(request: HttpRequest) -> HttpResponse:
    """Always fails, the way an unexpected bug in a view would."""
    raise RuntimeError(EXCEPTION_MESSAGE)


urlpatterns = [path(RAISE_PATH.strip("/") + "/", explode, name="raise")]
