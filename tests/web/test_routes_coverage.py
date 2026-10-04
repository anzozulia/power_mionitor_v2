"""Default-deny over every named route (R14; TEST-STRATEGY §5.6).

- The login-exempt routes are read from the views themselves (``login_not_required``
  sets ``login_required = False``), and they are exactly {login, logout, heartbeat,
  healthz}. A new exempt route fails here until this list changes on purpose.
- Every other named route, enumerated from ``django.urls.get_resolver()``, answers an
  anonymous GET and an anonymous POST with 302 to ``/login/?next=<path>`` (a POST-only
  view may answer 405 to GET instead), so no new endpoint slips past sign-in.
- The Phase 6 server surfaces are in the route table, so they are part of the walk.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from powermon.alerts import ops

EXEMPT = {"login", "logout", "heartbeat", "healthz"}
# Phase 6 surfaces that must be in the walk (UI-02, UI-05, UI-06).
NEW_SURFACES = {"location-status-json", "theme", "location-chart"}


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
