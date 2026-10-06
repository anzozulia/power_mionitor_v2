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

The location page is read through tests/web/pages.py and the 06-UI-SPEC switch hooks
(UI-10): each switch is ``form[data-testid=switch][data-switch][data-state]`` posting the
hidden ``value`` (the target) with ``button[role=switch][aria-checked]`` (the current
state), named by its sr-only action and described by its help; the state heading is
``[data-testid=switch-state]``.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import Client, RequestFactory
from pages import all_by_testid, by_testid, hidden_value, messages, parse, post_form, text

from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.web.location_views import (
    ALERTS_COPY,
    MAINTENANCE_COPY,
    ROUTER_GRACE_COPY,
    MaintenanceSwitchView,
)

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
    "queued still go out. The chart, its regular updates and the midnight re-pin carry on."
)
ROUTER_GRACE_ON_FLASH = (
    "Router grace is on. From now on, OFF waits 180 seconds longer right after power returns."
)
ROUTER_GRACE_ALREADY_ON_FLASH = "Router grace was already on. Nothing changed."
ROUTER_GRACE_ALREADY_OFF_FLASH = "Router grace was already off. Nothing changed."
ROUTER_GRACE_HELP = (
    "While on, OFF waits 180 seconds longer when the last heartbeat came within 5 minutes "
    "after power returned, so a router that restarts after a blackout is not reported as a "
    "second outage. It changes only decisions made from now on."
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


def _flashes(page: Any) -> list[tuple[str, str]]:
    """Each flash on the page as (role, text), read from its toasts (UI-09)."""
    return [(message.role, message.text) for message in messages(page)]


def _is_info(page: Any) -> bool:
    """Every flash on the page is an info flash."""
    return all(message.level == "info" for message in messages(page))


def _maintenance_form(page: Any, location: Any) -> Tag:
    """The one form that posts to the location's maintenance switch."""
    return post_form(page, _switch(location))


def _has_csrf(form: Tag) -> bool:
    return form.find("input", attrs={"name": "csrfmiddlewaretoken"}) is not None


def _button(form: Tag) -> Tag:
    """The switch's one submit button, ``button[type=submit][role=switch]``."""
    buttons = form.select('button[type="submit"][role="switch"]')
    assert len(buttons) == 1, f"expected one switch button, found {len(buttons)}"
    return buttons[0]


def _action(form: Tag) -> str:
    """The switch button's accessible name: its sr-only action, e.g. "Turn maintenance on"."""
    return text(_button(form))


def _state(form: Tag) -> str:
    """The switch's state heading, e.g. "Maintenance is off"."""
    return text(by_testid(form, "switch-state"))


def _help(form: Tag) -> str:
    """The text of the help the switch button is described by, inside its own form."""
    help_id = str(_button(form)["aria-describedby"])
    found = form.find(id=help_id)
    assert isinstance(found, Tag), f"no help element {help_id!r} in the switch form"
    return text(found)


def _get(admin: Client, location: Any) -> Tag:
    """The location page, parsed."""
    response = admin.get(_page(location))
    assert response.status_code == 200
    return parse(response)


def _status_pill(page: Any) -> Tag:
    """The header's status pill."""
    return by_testid(by_testid(page, "location-header"), "status-pill")


@pytest.mark.django_db
def test_LOC08_maintenance_on_from_the_location_page(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = _on_since_8(location_factory)

    page = _get(admin, location)

    form = _maintenance_form(page, location)
    assert _state(form) == "Maintenance is off"
    assert _has_csrf(form)
    assert hidden_value(form, "value") == "on"
    assert (form["data-switch"], form["data-state"]) == ("maintenance", "off")
    assert (_button(form)["aria-checked"], _action(form)) == ("false", "Turn maintenance on")

    response = admin.post(_switch(location), {"value": "on"})

    assert response.status_code == 302
    assert response.url == _page(location)
    followed = parse(admin.get(response.url))
    assert _flashes(followed) == [("status", MAINTENANCE_ON_FLASH)]
    pill = _status_pill(followed)
    assert (pill["data-status"], text(pill)) == ("maintenance", "Maintenance")
    after = _maintenance_form(followed, location)
    assert _state(after) == "Maintenance is on"
    assert (_button(after)["aria-checked"], _action(after)) == ("true", "Turn maintenance off")
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
    page = _get(admin, location)
    assert hidden_value(_maintenance_form(page, location), "value") == "off"

    response = admin.post(_switch(location), {"value": "off"})

    assert (response.status_code, response.url) == (302, _page(location))
    followed = parse(admin.get(response.url))
    assert _flashes(followed) == [("status", MAINTENANCE_OFF_FLASH)]
    assert _state(_maintenance_form(followed, location)) == "Maintenance is off"
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
    assert _flashes(again) == [("status", ALREADY_ON_FLASH)]
    assert _is_info(again)
    assert (
        list(PowerInterval.objects.filter(location=location).values_list("id", "end_at"))
        == intervals
    )
    assert LocationState.objects.get(location=location).state_version == version

    other = location_factory(name="Other")
    off = admin.post(_switch(other), {"value": "off"}, follow=True).content.decode()

    assert _flashes(off) == [("status", ALREADY_OFF_FLASH)]
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


def _form(page: Any, url: str) -> Tag:
    """The one form that posts to ``url``."""
    return post_form(page, url)


def _switch_states(page: Any) -> list[tuple[str, str]]:
    """Every switch on the page as (data-switch, state heading), in DOM order."""
    return [(str(form["data-switch"]), _state(form)) for form in all_by_testid(page, "switch")]


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

    page = _get(admin, location)

    form = _form(page, _alerts(location))
    assert _state(form) == "Alerts are on"
    assert _help(form) == ALERTS_HELP
    assert _has_csrf(form)
    assert hidden_value(form, "value") == "off"
    assert (form["data-switch"], form["data-state"]) == ("alerts", "on")
    assert (_button(form)["aria-checked"], _action(form)) == ("true", "Turn alerts off")
    # D-05 order: Maintenance first, then Alerts.
    assert _switch_states(page)[:2] == [
        ("maintenance", "Maintenance is off"),
        ("alerts", "Alerts are on"),
    ]

    response = admin.post(_alerts(location), {"value": "off"})

    assert (response.status_code, response.url) == (302, _page(location))
    followed = parse(admin.get(response.url))
    assert _flashes(followed) == [("status", ALERTS_OFF_FLASH)]
    after = _form(followed, _alerts(location))
    assert _state(after) == "Alerts are off"
    assert hidden_value(after, "value") == "on"
    assert (_button(after)["aria-checked"], _action(after)) == ("false", "Turn alerts on")
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
    assert _flashes(again) == [("status", ALERTS_ALREADY_OFF_FLASH)]
    assert _is_info(again)
    assert _ctid(location) == row

    on = admin.post(_alerts(location), {"value": "on"}, follow=True).content.decode()

    assert _flashes(on) == [("status", ALERTS_ON_FLASH)]
    assert Location.objects.get(pk=location.pk).alerts_enabled is True
    on_again = admin.post(_alerts(location), {"value": "on"}, follow=True).content.decode()
    assert _flashes(on_again) == [("status", ALERTS_ALREADY_ON_FLASH)]
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


# The router-grace switch (LOC-09, D-05): one configuration column, future decisions only


def _router_grace(location: Any) -> str:
    return f"/locations/{location.pk}/router-grace/"


@pytest.mark.django_db
def test_router_grace_switch_flashes(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office", period_s=45, grace_s=20)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    before = _columns(location)
    version = LocationState.objects.get(location=location).state_version

    page = _get(admin, location)

    form = _form(page, _router_grace(location))
    assert _state(form) == "Router grace is off"
    assert _help(form) == ROUTER_GRACE_HELP
    assert _has_csrf(form)
    assert hidden_value(form, "value") == "on"
    assert (form["data-switch"], form["data-state"]) == ("router-grace", "off")
    assert (_button(form)["aria-checked"], _action(form)) == ("false", "Turn router grace on")
    # D-05 order: Maintenance, Alerts, Router grace.
    assert _switch_states(page) == [
        ("maintenance", "Maintenance is off"),
        ("alerts", "Alerts are on"),
        ("router-grace", "Router grace is off"),
    ]

    on = admin.post(_router_grace(location), {"value": "on"})

    assert (on.status_code, on.url) == (302, _page(location))
    followed = parse(admin.get(on.url))
    assert _flashes(followed) == [("status", ROUTER_GRACE_ON_FLASH)]
    after = _form(followed, _router_grace(location))
    assert _state(after) == "Router grace is on"
    assert (_button(after)["aria-checked"], _action(after)) == ("true", "Turn router grace off")
    assert _columns(location) == {**before, "router_grace": True}
    assert LocationState.objects.get(location=location).state_version == version

    # Edge (idempotency, UI-D3): on twice writes nothing the second time.
    row = _ctid(location)
    again = admin.post(_router_grace(location), {"value": "on"}, follow=True).content.decode()
    assert _flashes(again) == [("status", ROUTER_GRACE_ALREADY_ON_FLASH)]
    assert _ctid(location) == row

    off = admin.post(_router_grace(location), {"value": "off"}, follow=True).content.decode()

    # The off flash names the plain timeout of this location: P + G = 45 + 20 seconds.
    assert _flashes(off) == [
        (
            "status",
            "Router grace is off. From now on, OFF is reported after 65 seconds without a "
            "heartbeat.",
        )
    ]
    assert _columns(location) == before
    off_again = admin.post(_router_grace(location), {"value": "off"}, follow=True).content.decode()
    assert _flashes(off_again) == [("status", ROUTER_GRACE_ALREADY_OFF_FLASH)]
    assert LocationState.objects.get(location=location).state_version == version
    assert _open_state(location) == "on"
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_router_grace_switch_refuses_a_bad_value_a_get_and_an_unknown_location(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    row = _ctid(location)

    for data in ({}, {"value": "1"}, {"value": "On"}):
        response = admin.post(_router_grace(location), data)
        assert (response.status_code, response.content) == (400, b"")
    assert admin.get(_router_grace(location)).status_code == 405
    for pk in (gone.pk, gone.pk + 1000):
        assert admin.post(f"/locations/{pk}/router-grace/", {"value": "on"}).status_code == 404

    assert _ctid(location) == row
    assert not Location.objects.filter(router_grace=True).exists()
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_alerts_and_router_grace_switches_need_the_signed_in_admin(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    # T-04-20: LoginRequiredMiddleware denies by default; neither switch is login-exempt.
    location = location_factory()
    row = _ctid(location)

    for url, value in ((_alerts(location), "off"), (_router_grace(location), "on")):
        response = client.post(url, {"value": value})
        assert response.status_code == 302
        assert response.url == f"/login/?next={url}"

    assert _ctid(location) == row


# UI-10 end to end on the page: each switch posts its target, flips, and repeats safely


# (route segment, data-switch, the flashes)
SWITCHES = {
    "maintenance": ("maintenance", MAINTENANCE_COPY),
    "alerts": ("alerts", ALERTS_COPY),
    "router-grace": ("router-grace", ROUTER_GRACE_COPY),
}


@pytest.mark.django_db
@pytest.mark.parametrize("name", list(SWITCHES))
def test_UI10_switch_flip_and_repeat(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram, name: str
) -> None:
    switch, copy = SWITCHES[name]
    location = location_factory(name="Office", period_s=45, grace_s=20)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    url = f"/locations/{location.pk}/{name}/"

    def form_on(page: Any) -> Tag:
        found = [form for form in all_by_testid(page, "switch") if form["data-switch"] == switch]
        assert len(found) == 1
        assert found[0] is post_form(page, url)
        return found[0]

    def toasts(response: Any) -> list[tuple[str | None, str]]:
        return [(message.level, message.text) for message in messages(response)]

    def flash(key: str) -> str:
        return copy[key].format(off_after_s=65)

    # The form posts the target (the opposite of data-state); aria-checked is the current state.
    form = form_on(_get(admin, location))
    state = str(form["data-state"])
    target = "off" if state == "on" else "on"
    assert hidden_value(form, "value") == target
    assert _button(form)["aria-checked"] == ("true" if state == "on" else "false")

    # POST the target: 302 to the page, which shows the flipped state and a success toast.
    response = admin.post(url, {"value": target})
    assert (response.status_code, response.url) == (302, _page(location))
    followed = admin.get(response.url)
    assert toasts(followed) == [("success", flash(target))]
    flipped = form_on(parse(followed))
    assert (flipped["data-state"], hidden_value(flipped, "value")) == (target, state)
    assert _button(flipped)["aria-checked"] == ("true" if target == "on" else "false")

    # The same value again (a double click, a stale page): nothing changes, an info toast.
    again = admin.post(url, {"value": target}, follow=True)
    assert toasts(again) == [("info", flash(f"already_{target}"))]
    assert flash(f"already_{target}").endswith("Nothing changed.")
    assert form_on(parse(again))["data-state"] == target

    # Two tabs loaded before the change, both posting the old target back: one change only,
    # the second gets the "already" flash (R16).
    first = admin.post(url, {"value": state}, follow=True)
    second = admin.post(url, {"value": state}, follow=True)
    assert toasts(first) == [("success", flash(state))]
    assert toasts(second) == [("info", flash(f"already_{state}"))]
    assert form_on(parse(second))["data-state"] == state

    # Failure: GET is 405, a bad value 400 with an empty body, a missing CSRF token 403.
    assert admin.get(url).status_code == 405
    bad = admin.post(url, {"value": "toggle"})
    assert (bad.status_code, bad.content) == (400, b"")
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(User.objects.get(username="admin"))
    assert browser.post(url, {"value": target}).status_code == 403
    assert form_on(_get(admin, location))["data-state"] == state
    assert len(fake_telegram.calls) == 0
