"""Default-deny over every named route, and matrix completeness (R14; TEST-STRATEGY §5.6).

- The login-exempt routes are read from the views themselves (``login_not_required``
  sets ``login_required = False``), and they are exactly {login, logout, heartbeat,
  healthz}. A new exempt route fails here until this list changes on purpose.
- Every other named route, enumerated from ``django.urls.get_resolver()``, answers an
  anonymous GET and an anonymous POST with 302 to ``/login/?next=<path>`` (a POST-only
  view may answer 405 to GET instead), so no new endpoint slips past sign-in.
- The Phase 6 server surfaces are in the route table, so they are part of the walk.
- Matrix completeness: every named admin route is rendered by the render matrix
  (``test_render_matrix.MATRIX_ROUTES``) or is in the explicit POST-only list, whose
  routes answer a signed-in GET with 405; the status JSON and the chart PNG are the two
  admin surfaces that are not HTML pages. The device endpoint and the health check are not
  admin routes. A new route on none of these lists, and a listed name that is no route,
  fail here.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from test_render_matrix import MATRIX_ROUTES

from powermon.alerts import ops

EXEMPT = {"login", "logout", "heartbeat", "healthz"}
# Phase 6 surfaces that must be in the walk (UI-02, UI-05, UI-06).
NEW_SURFACES = {"location-status-json", "theme", "location-chart"}
# Not admin routes: the device's heartbeat and the container health check (no page).
NOT_ADMIN = frozenset({"heartbeat", "healthz"})
# The admin surfaces that are not HTML pages (the poll's JSON and the chart preview PNG):
# their own suites and the INV-23 matrix cover them, the render matrix cannot.
NOT_HTML = frozenset({"location-status-json", "location-chart"})
# The admin routes that take only a POST and answer with a redirect; the matrix renders the
# page each redirects to (the switch and test-message flashes on S5, sign-in after sign-out,
# the list after the theme switch). The POSTs of create, edit, delete, reveal, regenerate,
# remove and reset share a route with a page the matrix renders.
POST_ONLY = frozenset(
    {
        "logout",
        "theme",
        "location-maintenance",
        "location-alerts",
        "location-router-grace",
        "location-test-message",
    }
)


def _named_routes(patterns: Iterable[Any] | None = None) -> list[URLPattern]:
    """Every named URL pattern, included URLconfs too."""
    found: list[URLPattern] = []
    for entry in get_resolver().url_patterns if patterns is None else patterns:
        if isinstance(entry, URLResolver):
            found.extend(_named_routes(entry.url_patterns))
        elif entry.name:
            found.append(entry)
    return found


def _kwargs(pattern: URLPattern, values: dict[str, int]) -> dict[str, int]:
    """A real value for each of the route's parameters; fail on a parameter with none."""
    names = list(getattr(pattern.pattern, "converters", {}))
    missing = [name for name in names if name not in values]
    assert not missing, f"no sample value for {missing} of the route {pattern.name!r}"
    return {name: values[name] for name in names}


def _is_exempt(pattern: URLPattern) -> bool:
    return getattr(pattern.callback, "login_required", True) is False


@pytest.mark.django_db
def test_R14_exempt_routes_are_exactly_the_four() -> None:
    routes = _named_routes()
    names = [pattern.name for pattern in routes]

    assert len(names) == len(set(names))
    assert {pattern.name for pattern in routes if _is_exempt(pattern)} == EXEMPT
    assert NEW_SURFACES <= set(names)


@pytest.mark.django_db
def test_R14_every_named_route_is_login_required(
    client: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(name="Office")
    values = {"pk": location.pk, "start_us": ops.instant_us(fixed_now)}
    checked = []

    for pattern in _named_routes():
        url = reverse(pattern.name, kwargs=_kwargs(pattern, values))
        get = client.get(url)
        if _is_exempt(pattern):
            # Exempt routes never send an anonymous visitor to sign-in.
            assert not get.get("Location", "").startswith("/login/"), url
            continue
        assert get.status_code in {302, 405}, url
        if get.status_code == 302:
            assert get.url == f"/login/?next={url}", url
        post = client.post(url)
        assert post.status_code == 302, url
        assert post.url == f"/login/?next={url}", url
        checked.append(pattern.name)

    assert NEW_SURFACES <= set(checked)
    assert not EXEMPT & set(checked)


def matrix_gaps(names: set[str], rendered: Iterable[str], post_only: Iterable[str]) -> list[str]:
    """What keeps the route table and the matrix lists apart (TEST-STRATEGY §5.6)."""
    rendered, post_only = set(rendered), set(post_only)
    listed = rendered | post_only | NOT_HTML | NOT_ADMIN
    gaps = [
        f"{name}: neither rendered by the matrix nor POST-only" for name in sorted(names - listed)
    ]
    gaps += [f"{name}: listed but not a route" for name in sorted(listed - names)]
    gaps += [f"{name}: both rendered and POST-only" for name in sorted(rendered & post_only)]
    return gaps


def test_R14_matrix_completeness() -> None:
    names = {str(pattern.name) for pattern in _named_routes()}

    # Expected: every named admin route is rendered by the matrix or is POST-only.
    assert matrix_gaps(names, MATRIX_ROUTES, POST_ONLY) == []
    assert NOT_ADMIN | {"login", "logout"} == EXEMPT
    # Failure: a new route that no list names fails the check.
    assert matrix_gaps(names | {"location-export"}, MATRIX_ROUTES, POST_ONLY) == [
        "location-export: neither rendered by the matrix nor POST-only"
    ]
    # Edge: a listed name that is no route fails too, and so does a route on two lists.
    assert matrix_gaps(names - {"theme"}, MATRIX_ROUTES, POST_ONLY) == [
        "theme: listed but not a route"
    ]
    assert matrix_gaps(names, MATRIX_ROUTES | {"theme"}, POST_ONLY) == [
        "theme: both rendered and POST-only"
    ]


@pytest.mark.django_db
def test_R14_post_only_routes_refuse_get(
    client: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    client.force_login(get_user_model().objects.create_user("admin", password="not-used-here"))
    location = location_factory(name="Office")
    values = {"pk": location.pk, "start_us": ops.instant_us(fixed_now)}
    routes = {str(pattern.name): pattern for pattern in _named_routes()}

    # Expected: a signed-in GET of every POST-only route answers 405 (no page to render).
    for name in sorted(POST_ONLY):
        url = reverse(name, kwargs=_kwargs(routes[name], values))
        assert client.get(url).status_code == 405, name
    # Failure: a route with a page is not POST-only: the same client gets the list.
    assert client.get(reverse("location-list")).status_code == 200
