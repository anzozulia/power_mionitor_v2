"""Breadcrumb trails for the app layout (UI-01; 06-UI-SPEC Layout Shell › Top bar).

``{% load crumbs %}{% breadcrumb_trail as trail %}`` gives the page's trail as a list of
``Crumb(label, href)``, from the resolved URL name and the context's ``location``. The
last item is the current page: its ``href`` is None and the template renders it as a span
with ``aria-current="page"``. ``partials/_crumbs.html`` renders a trail in the top bar
and, under the h1, as the compact copy.

The trails, one per app page (06-UI-SPEC page contracts):

- ``location-list``: Locations
- ``location-create``: Locations › Add location
- ``location-detail``: Locations › {name}
- ``location-edit``: Locations › {name} › Edit
- ``location-setup``: Locations › {name} › Device setup
- ``location-delete``: Locations › {name} › Delete
- ``location-regenerate``: Locations › {name} › Device setup › Regenerate key
- ``outage-remove``: Locations › {name} › Remove outage
- ``location-reset``: Locations › {name} › Reset history

A POST answered with another page shows that page's trail (``POST_TRAILS``): the
Regenerate POST answers with the revealed setup page (D-14), so it gets
Locations › {name} › Device setup, while its GET, the confirmation, keeps its own trail.

Every href comes from ``reverse()`` with a route name and the location's pk, never from
the request (T-06-42). The name is the stored name, escaped by the template (R1).

Any other page, and every case where the trail cannot be built, gets the one-item trail
"Locations" and never an exception: a context without ``request``, a request without a
resolver match (a bare RequestFactory request has ``resolver_match`` None), an unknown or
missing URL name, and a location page whose context holds no location.
"""

from typing import NamedTuple

from django import template
from django.template import Context
from django.urls import reverse

register = template.Library()

LOCATIONS = "Locations"
LIST_ROUTE = "location-list"
DETAIL_ROUTE = "location-detail"
SETUP_ROUTE = "location-setup"


class Crumb(NamedTuple):
    """One breadcrumb: its label and its link (None for the current page)."""

    label: str
    href: str | None


class _Name:
    """The marker for the location's name in a trail, linked to the location page."""


NAME = _Name()

# Each trail after "Locations": (label or NAME, route to link to or None). The last step
# is the current page and is never linked, whatever its route.
type Step = tuple[str | _Name, str | None]

TRAILS: dict[str, tuple[Step, ...]] = {
    "location-list": (),
    "location-create": (("Add location", None),),
    "location-detail": ((NAME, DETAIL_ROUTE),),
    "location-edit": ((NAME, DETAIL_ROUTE), ("Edit", None)),
    "location-setup": ((NAME, DETAIL_ROUTE), ("Device setup", None)),
    "location-delete": ((NAME, DETAIL_ROUTE), ("Delete", None)),
    "location-regenerate": (
        (NAME, DETAIL_ROUTE),
        ("Device setup", SETUP_ROUTE),
        ("Regenerate key", None),
    ),
    "outage-remove": ((NAME, DETAIL_ROUTE), ("Remove outage", None)),
    "location-reset": ((NAME, DETAIL_ROUTE), ("Reset history", None)),
}

# The routes whose POST answers with another page, mapped to that page's route: the
# Regenerate POST renders the setup page (location_views.RegenerateKeyView.post).
POST_TRAILS: dict[str, str] = {"location-regenerate": SETUP_ROUTE}


def fallback() -> list[Crumb]:
    """The one-item trail: Locations, as the current page."""
    return [Crumb(LOCATIONS, None)]


def trail_for(url_name: object, location: object) -> list[Crumb]:
    """The trail of the page named ``url_name`` about ``location`` (see the module doc).

    ``location`` needs a ``pk`` and a string ``name`` when the trail shows its name; any
    other object, None included, gives the one-item trail. So does an unknown name.
    """
    steps = TRAILS.get(url_name) if isinstance(url_name, str) else None
    if steps is None:
        return fallback()
    pk = getattr(location, "pk", None)
    raw_name = getattr(location, "name", None)
    known = isinstance(pk, int) and isinstance(raw_name, str)
    if not known and any(label is NAME for label, _ in steps):
        return fallback()
    name = raw_name if isinstance(raw_name, str) else ""
    crumbs = [Crumb(LOCATIONS, reverse(LIST_ROUTE))]
    for label, route in steps:
        # A step that is not a string is NAME: the location's stored name.
        text = label if isinstance(label, str) else name
        href = None if route is None else reverse(route, kwargs={"pk": pk})
        crumbs.append(Crumb(text, href))
    # The last item is the current page: never a link.
    crumbs[-1] = Crumb(crumbs[-1].label, None)
    return crumbs


@register.simple_tag(takes_context=True)
def breadcrumb_trail(context: Context) -> list[Crumb]:
    """``{% breadcrumb_trail as trail %}``: the trail of the page being rendered.

    Reads ``request.resolver_match.url_name`` and ``location`` from the context,
    defensively: either may be missing, and then the trail is the one item Locations. A
    POST on a ``POST_TRAILS`` route gets the trail of the page it answers with.
    """
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", None)
    if getattr(request, "method", None) == "POST" and isinstance(url_name, str):
        url_name = POST_TRAILS.get(url_name, url_name)
    return trail_for(url_name, context.get("location"))
