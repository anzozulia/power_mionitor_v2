"""The location page's one-click switches (LOC-08; D-02, D-05, D-17; UI-D3, UI-D4).

A switch is a POST form (CSRF) that posts its target value, never "toggle" (UI-D3), and is
answered POST -> redirect -> GET with a flash (UI-D4). The maintenance switch calls the
engine transition (``maintenance.set_maintenance``), stamped from the view's injected
clock. No switch makes a Telegram call (KD2).
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory

from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations.models import Location
from powermon.web.location_views import MaintenanceSwitchView

User = get_user_model()

MAINTENANCE_ON_FLASH = (
    "Maintenance is on. OFF is not detected and no OFF alert is sent; the chart shows this "
    "time as not monitored."
)


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _page(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _switch(location: Any) -> str:
    return f"/locations/{location.pk}/maintenance/"


def _open_state(location: Any) -> str | None:
    piece = PowerInterval.objects.filter(location=location, end_at__isnull=True).first()
    return None if piece is None else piece.state


def _on_since_8(location_factory: Callable[..., Any]) -> Any:
    location = location_factory(name="Office")
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    return location


def _maintenance_form(page: str, location: Any) -> str:
    """The body of the one form that posts to the location's maintenance switch."""
    forms = re.findall(
        rf'<form method="post" action="{_switch(location)}">(.*?)</form>', page, re.S
    )
    assert len(forms) == 1
    return str(forms[0])


@pytest.mark.django_db
def test_LOC08_maintenance_on_from_the_location_page(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)

    page = admin.get(_page(location))

    assert page.status_code == 200
    html = page.content.decode()
    assert "<h3>Maintenance is off</h3>" in html
    form = _maintenance_form(html, location)
    assert 'name="csrfmiddlewaretoken"' in form
    assert '<input type="hidden" name="value" value="on">' in form
    assert '<button class="btn btn--secondary" type="submit">Turn maintenance on</button>' in form

    response = admin.post(_switch(location), {"value": "on"})

    assert response.status_code == 302
    assert response.url == _page(location)
    followed = admin.get(response.url).content.decode()
    assert re.findall(r'role="status">([^<]*)<', followed) == [MAINTENANCE_ON_FLASH]
    assert '<span class="status status--maintenance">Maintenance</span>' in followed
    assert "<h3>Maintenance is on</h3>" in followed
    assert '<button class="btn btn--secondary" type="submit">Turn maintenance off</button>' in (
        _maintenance_form(followed, location)
    )
    assert Location.objects.get(pk=location.pk).maintenance is True
    assert _open_state(location) == "not_monitored"
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_switch_view_stamps_the_toggle_from_its_clock(
    rf: RequestFactory, location_factory: Callable[..., Any]
) -> None:
    location = _on_since_8(location_factory)
    clicked = _at(10, 15, 7)
    request = rf.post(_switch(location), {"value": "on"})
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]

    response = MaintenanceSwitchView.as_view(clock=FakeClock(clicked))(request, pk=location.pk)

    assert response.status_code == 302
    piece = PowerInterval.objects.get(location=location, end_at__isnull=True)
    assert (piece.state, piece.start_at) == ("not_monitored", clicked)


@pytest.mark.django_db
def test_switch_for_an_unknown_location_is_404_and_writes_nothing(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    version = LocationState.objects.get(location=location).state_version
    unknown = location.pk + 1000

    response = admin.post(f"/locations/{unknown}/maintenance/", {"value": "on"})

    assert response.status_code == 404
    assert list(Location.objects.values_list("maintenance", flat=True)) == [False]
    assert LocationState.objects.get(location=location).state_version == version
    assert _open_state(location) == "on"
    assert len(fake_telegram.calls) == 0
