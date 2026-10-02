"""The location page's one-click switches (LOC-08, LOC-10; D-02, D-05, D-06, D-17; UI-D3, UI-D4).

A switch is a POST form (CSRF) that posts its target value, never "toggle" (UI-D3), and is
answered POST -> redirect -> GET with a flash (UI-D4). The maintenance switch calls the
engine transition (``maintenance.set_maintenance``), stamped from the view's injected
clock. The alerts switch is a configuration-only write (``actions.set_flag``): exactly one
column changes, with no ``location_state`` lock and no ``state_version`` bump (D-05). No
switch makes a Telegram call (KD2).

Edges (UI-SPEC screen H, E3 error): the same state again writes nothing and gets the
"already" info flash; GET and other methods answer 405; a missing or unknown value answers
400 with an empty body; a POST without a CSRF token is refused; an unknown location is 404.
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
from django.db import connection
from django.test import Client, RequestFactory

from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.web.location_views import MaintenanceSwitchView

User = get_user_model()

MAINTENANCE_ON_FLASH = (
    "Maintenance is on. OFF is not detected and no OFF alert is sent; the chart shows this "
    "time as not monitored."
)
MAINTENANCE_OFF_FLASH = (
    "Maintenance is off. OFF detection starts again now; silence during maintenance does not count."
)
ALREADY_ON_FLASH = "Maintenance was already on. Nothing changed."
ALREADY_OFF_FLASH = "Maintenance was already off. Nothing changed."
ALERTS_OFF_FLASH = (
    "Alerts are off. Subscribers get no new alerts; alerts already queued still go out. "
    "The chart keeps updating."
)
ALERTS_ON_FLASH = "Alerts are on. Subscribers get alerts for changes recorded from now on."
ALERTS_ALREADY_OFF_FLASH = "Alerts were already off. Nothing changed."
ALERTS_ALREADY_ON_FLASH = "Alerts were already on. Nothing changed."
ALERTS_HELP = (
    "While off, subscribers get no new alerts, and none are saved for later. Alerts already "
    "queued still go out. The chart, its 15-minute refresh and the midnight re-pin carry on."
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


@pytest.mark.django_db
def test_maintenance_off_from_the_location_page(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    assert admin.post(_switch(location), {"value": "on"}).status_code == 302
    page = admin.get(_page(location)).content.decode()
    assert '<input type="hidden" name="value" value="off">' in _maintenance_form(page, location)

    response = admin.post(_switch(location), {"value": "off"})

    assert (response.status_code, response.url) == (302, _page(location))
    followed = admin.get(response.url).content.decode()
    assert re.findall(r'role="status">([^<]*)<', followed) == [MAINTENANCE_OFF_FLASH]
    assert "<h3>Maintenance is off</h3>" in followed
    assert Location.objects.get(pk=location.pk).maintenance is False
    assert _open_state(location) == "on"
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_switch_already_on_shows_the_info_flash(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    admin.post(_switch(location), {"value": "on"})
    admin.get(_page(location))
    intervals = list(PowerInterval.objects.filter(location=location).values_list("id", "end_at"))
    version = LocationState.objects.get(location=location).state_version

    again = admin.post(_switch(location), {"value": "on"}, follow=True).content.decode()

    # UI-D3: a second click (a double click, a second tab, a stale page) writes nothing.
    assert re.findall(r'role="status">([^<]*)<', again) == [ALREADY_ON_FLASH]
    assert '<p class="callout" role="status">' in again
    assert (
        list(PowerInterval.objects.filter(location=location).values_list("id", "end_at"))
        == intervals
    )
    assert LocationState.objects.get(location=location).state_version == version

    other = location_factory(name="Other")
    off = admin.post(_switch(other), {"value": "off"}, follow=True).content.decode()

    assert re.findall(r'role="status">([^<]*)<', off) == [ALREADY_OFF_FLASH]
    assert Location.objects.get(pk=other.pk).maintenance is False
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_switch_get_is_405(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = _on_since_8(location_factory)

    for method in (admin.get, admin.put, admin.delete):
        assert method(_switch(location)).status_code == 405

    assert Location.objects.get(pk=location.pk).maintenance is False


@pytest.mark.django_db
def test_switch_bad_value_is_400_and_writes_nothing(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    version = LocationState.objects.get(location=location).state_version

    for data in ({}, {"value": "toggle"}, {"value": "ON"}, {"value": ""}):
        response = admin.post(_switch(location), data)
        assert response.status_code == 400
        assert response.content == b""

    assert Location.objects.get(pk=location.pk).maintenance is False
    assert LocationState.objects.get(location=location).state_version == version
    assert _open_state(location) == "on"
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_switch_without_a_csrf_token_is_refused(location_factory: Callable[..., Any]) -> None:
    location = _on_since_8(location_factory)
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(User.objects.create_user("admin", password="not-used-here"))

    response = browser.post(_switch(location), {"value": "on"})

    assert response.status_code == 403
    assert Location.objects.get(pk=location.pk).maintenance is False


# The alerts switch (LOC-10, D-05, D-06): one configuration column, nothing else


def _form(page: str, url: str) -> str:
    """The body of the one form that posts to ``url``."""
    forms = re.findall(rf'<form method="post" action="{url}">(.*?)</form>', page, re.S)
    assert len(forms) == 1
    return str(forms[0])


def _alerts(location: Any) -> str:
    return f"/locations/{location.pk}/alerts/"


def _columns(location: Any) -> dict[str, Any]:
    """Every column of the location row, as stored."""
    return dict(Location.objects.filter(pk=location.pk).values().get())


def _ctid(location: Any) -> str:
    """The physical row version: every UPDATE that matches the row changes it, a no-op too."""
    with connection.cursor() as cur:
        cur.execute("SELECT ctid::text FROM location WHERE id = %s", [location.pk])
        return str(cur.fetchone()[0])


@pytest.mark.django_db
def test_LOC10_alerts_off_from_the_location_page(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    before = _columns(location)
    version = LocationState.objects.get(location=location).state_version

    page = admin.get(_page(location)).content.decode()

    assert "<h3>Alerts are on</h3>" in page
    assert f'<p class="help">{ALERTS_HELP}</p>' in page
    form = _form(page, _alerts(location))
    assert 'name="csrfmiddlewaretoken"' in form
    assert '<input type="hidden" name="value" value="off">' in form
    assert '<button class="btn btn--secondary" type="submit">Turn alerts off</button>' in form
    # D-05 order: Maintenance first, then Alerts.
    assert page.index("<h3>Maintenance is off</h3>") < page.index("<h3>Alerts are on</h3>")

    response = admin.post(_alerts(location), {"value": "off"})

    assert (response.status_code, response.url) == (302, _page(location))
    followed = admin.get(response.url).content.decode()
    assert re.findall(r'role="status">([^<]*)<', followed) == [ALERTS_OFF_FLASH]
    assert "<h3>Alerts are off</h3>" in followed
    after = _form(followed, _alerts(location))
    assert '<input type="hidden" name="value" value="on">' in after
    assert '<button class="btn btn--secondary" type="submit">Turn alerts on</button>' in after
    # Exactly one column changed (D-05): no other setting, no state row, no timeline.
    assert _columns(location) == {**before, "alerts_enabled": False}
    assert LocationState.objects.get(location=location).state_version == version
    assert _open_state(location) == "on"
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_alerts_switch_already_off_shows_the_info_flash(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    admin.post(_alerts(location), {"value": "off"})
    admin.get(_page(location))
    row = _ctid(location)

    again = admin.post(_alerts(location), {"value": "off"}, follow=True).content.decode()

    # UI-D3: the same state again writes nothing, not even a no-op UPDATE of the row.
    assert re.findall(r'role="status">([^<]*)<', again) == [ALERTS_ALREADY_OFF_FLASH]
    assert '<p class="callout" role="status">' in again
    assert _ctid(location) == row

    on = admin.post(_alerts(location), {"value": "on"}, follow=True).content.decode()

    assert re.findall(r'role="status">([^<]*)<', on) == [ALERTS_ON_FLASH]
    assert Location.objects.get(pk=location.pk).alerts_enabled is True
    on_again = admin.post(_alerts(location), {"value": "on"}, follow=True).content.decode()
    assert re.findall(r'role="status">([^<]*)<', on_again) == [ALERTS_ALREADY_ON_FLASH]
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_alerts_switch_refuses_a_bad_value_a_get_and_an_unknown_location(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    row = _ctid(location)

    for data in ({}, {"value": "toggle"}, {"value": "OFF"}, {"value": ""}):
        response = admin.post(_alerts(location), data)
        assert (response.status_code, response.content) == (400, b"")
    assert admin.get(_alerts(location)).status_code == 405
    for pk in (gone.pk, gone.pk + 1000):
        assert admin.post(f"/locations/{pk}/alerts/", {"value": "off"}).status_code == 404

    assert _ctid(location) == row
    assert list(Location.objects.order_by("pk").values_list("alerts_enabled", flat=True)) == [
        True,
        True,
    ]
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_set_flag_changes_one_column_once(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    before = _columns(location)

    assert actions.set_flag(location.pk, "router_grace", True) is True
    assert actions.set_flag(location.pk, "router_grace", True) is False
    assert actions.set_flag(location.pk, "alerts_enabled", False) is True

    assert _columns(location) == {**before, "router_grace": True, "alerts_enabled": False}


@pytest.mark.django_db
def test_set_flag_rejects_a_field_outside_the_switches(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    before, row = _columns(location), _ctid(location)

    # Maintenance is an engine transition (D-02), and the key, the tombstone and the
    # settings have their own paths: set_flag refuses them before any write.
    for field in ("maintenance", "device_key", "deleted_at", "name", "period_s"):
        with pytest.raises(ValueError, match="set_flag"):
            actions.set_flag(location.pk, field, True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bool"):
        actions.set_flag(location.pk, "alerts_enabled", "off")  # type: ignore[arg-type]

    assert (_columns(location), _ctid(location)) == (before, row)
    # A deleted or unknown location is never written.
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    gone_row = _ctid(gone)
    assert actions.set_flag(gone.pk, "alerts_enabled", False) is False
    assert actions.set_flag(gone.pk + 1000, "router_grace", True) is False
    assert _ctid(gone) == gone_row
    assert Location.objects.get(pk=gone.pk).alerts_enabled is True
