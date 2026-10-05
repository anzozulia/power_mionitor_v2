"""Test-only URLconf: every route of powermon.urls plus the app-shell probes (UI-01, UI-05;
the brief §13 tracer, app-layout slice).

Used with ``@pytest.mark.urls("urls_shell")``. The probes prove the shell end to end before
any page template extends it: a request goes through the real middleware (CSP, login
required, CSRF, messages) and the real context processors (theme, sidebar) into a template
string that extends ``layouts/app.html``. ``engines["django"].from_string(...).render(
request=request)`` builds a RequestContext, so every processor runs as on a real page.

- ``shell-probe`` (``/_shell/``): the layout with a fixed title and h1 and nothing else.
  Its URL name has no breadcrumb trail, so the trail is the one item "Locations".
- ``shell-probe-live`` (``/_shell/live/``): the same, as a polling page fills it: the
  ``live_indicator`` block holds ``partials/_live.html`` and the meta line the live-chip
  slot, whose reload chips link to the probe's own URL.
"""

from django.http import HttpRequest, HttpResponse
from django.template import engines
from django.urls import path

from powermon.urls import urlpatterns as app_urlpatterns

PROBE_PATH = "/_shell/"
PROBE_TITLE = "Shell probe"
LIVE_PROBE_PATH = "/_shell/live/"
LIVE_PROBE_TITLE = "Live probe"

PROBE_TEMPLATE = (
    '{% extends "layouts/app.html" %}'
    "{% block title %}Shell probe{% endblock %}"
    "{% block page_title %}Shell probe{% endblock %}"
    "{% block content %}<p>The probe page has no content of its own.</p>{% endblock %}"
)

LIVE_PROBE_TEMPLATE = (
    '{% extends "layouts/app.html" %}'
    "{% block title %}Live probe{% endblock %}"
    '{% block live_indicator %}{% include "partials/_live.html" %}{% endblock %}'
    "{% block page_title %}Live probe{% endblock %}"
    "{% block page_meta %}{% url 'shell-probe-live' as reload_url %}"
    '{% include "partials/_live_chip.html" with reload_url=reload_url only %}{% endblock %}'
    "{% block content %}<p>The live probe page has no content of its own.</p>{% endblock %}"
)


def render_probe(
    request: HttpRequest, source: str, context: dict[str, object] | None = None
) -> str:
    """``source`` rendered with ``request`` and every context processor."""
    return engines["django"].from_string(source).render(context, request=request)


def shell_probe(request: HttpRequest) -> HttpResponse:
    """The app layout with nothing but a title and an h1."""
    return HttpResponse(render_probe(request, PROBE_TEMPLATE))


def shell_probe_live(request: HttpRequest) -> HttpResponse:
    """The app layout with the LIVE indicator and the live-chip slot filled."""
    return HttpResponse(render_probe(request, LIVE_PROBE_TEMPLATE))


urlpatterns = [
    *app_urlpatterns,
    path(PROBE_PATH.strip("/") + "/", shell_probe, name="shell-probe"),
    path(LIVE_PROBE_PATH.strip("/") + "/", shell_probe_live, name="shell-probe-live"),
]
