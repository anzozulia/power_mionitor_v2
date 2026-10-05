"""The confirmation fragments of the modal (UI-07, D6-05, R7; TEST-STRATEGY §8.3).

The four confirmation GETs (S7 delete, S9 regenerate the key, S10 remove an outage, S11
reset the history) answer the shared ``[data-testid="confirm"]`` partial alone to a request
whose header ``X-PM-Fragment`` is exactly "1", and the full page in the app layout to every
other request. ``powermon/web/fragments.py`` is the only place that reads the header.

- Both variants run the same view code: the same pre-checks, the same status and
  ``Location`` header, the same form (action, CSRF input, the S9 marker) and the same text.
- The fragment has no ``<html>``, ``<head>``, ``<body>`` or ``<title>`` and none of the
  shell hooks; it answers with the response header ``X-PM-Fragment: 1``, which the full
  page never carries. Both carry ``Vary: X-PM-Fragment`` and ``Cache-Control: no-store``.
- Keep (``a[data-testid=keep]``) comes first and the destructive
  ``button[type=submit][data-testid=confirm-submit][data-variant=danger]`` last; every
  other button inside the form is ``type="button"``; no ``autofocus`` anywhere.

Pages are read through ``pages.py`` and the 06-UI-SPEC hooks only. Where a test needs a
fixed "now", it pins ``powermon.clock.SystemClock.now`` (the class attribute), so every
default clock (the views' clock attributes, the sidebar's and timefmt's) reads the same
instant. Histories are built through the engine (``transitions.record_heartbeat``,
``detection.run_cycle``, ``maintenance.set_maintenance``), so those tests are
``django_db(transaction=True)``. Times are asserted in Europe/Kyiv.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client
from pages import (
    all_by_testid,
    assert_page,
    breadcrumbs,
    by_testid,
    definitions,
    h1,
    hidden_value,
    main,
    messages,
    parse,
    text,
)

from powermon.alerts import ops
from powermon.clock import SystemClock
from powermon.engine import maintenance, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations import keys
from powermon.locations.models import Location
from powermon.web.history_views import OUTAGE_GONE_MESSAGE, REMOVAL_REFUSED_MESSAGE
from powermon.web.location_views import regenerate_marker
from powermon.worker import detection

User = get_user_model()

FRAGMENT = "X-PM-Fragment"
# Hooks of the app shell a fragment never carries (06-UI-SPEC Test hooks > App shell).
SHELL_HOOKS = (
    "sidebar",
    "topbar",
    "toasts-status",
    "toasts-alert",
    "breadcrumbs",
    "breadcrumbs-compact",
    "skip-link",
)
DELETE_CONSEQUENCES = [
    "stops its alerts at once and drops the alerts still queued, so they are never sent;",
    "makes its device key stop working: the device gets HTTP 401;",
    "unpins its weekly chart in the channel if the bot can still pin there; otherwise unpin "
    "it by hand in Telegram (the posted messages stay in the channel);",
    "closes its open problems, such as failing delivery, without a recovery notice;",
    "hides it from the admin panel. Its history stays in the database but is never shown again.",
]
DELETE_LEAD = "This cannot be undone. Deleting this location:"
DELETE_PAUSE = (
    "To pause this location instead, turn maintenance on or alerts off on the location page."
)
DELETE_RECREATE = (
    "To monitor this place again later, add a new location. It gets a new device key and "
    "starts with an empty history."
)
KYIV = "Europe/Kyiv"
# 06-UI-SPEC copy rows regen.*, verbatim.
REGEN_LEAD = (
    "The old key stops working at once. Until the device has the new key, it gets HTTP 401 "
    "and its heartbeats are not recorded. The history is kept."
)
REGEN_WARNING_ON = (
    "While maintenance is off, this location can be reported OFF as soon as {off_after} "
    "seconds after its last heartbeat, and subscribers then get an OFF alert if alerts are on. "
    "Turn maintenance on first on the location page, update the device, then turn maintenance "
    "off there."
)
REGEN_POWER_OFF = (
    "This location is off now. Until the device has the new key, the return of power is not "
    "seen: the outage is recorded until the first heartbeat with the new key, so the chart, "
    "the day totals and the ON alert (if alerts are on) count that time as off. Turn "
    "maintenance on first to have that time shown as not monitored instead."
)
REGEN_WAITING = (
    "This location has had no heartbeat yet, so nothing is reported while you update the "
    "device."
)
REGEN_MAINTENANCE = (
    "Maintenance is on, so OFF is not detected while you update the device. Turn maintenance "
    "off on the location page once the device sends heartbeats with the new key."
)
# 06-UI-SPEC copy rows remove.*, verbatim (remove.c3 is amendment A3).
REMOVE_LEAD = "This cannot be undone. Removing this outage:"
REMOVE_C1 = (
    "records its off time as power on, so the chart and the daily totals no longer count it "
    "as off time or as an outage;"
)
REMOVE_C2 = (
    "keeps the time inside it that was not monitored (maintenance, server downtime) as not "
    "monitored;"
)
REMOVE_C3 = (
    "sends nothing itself, and drops its queued OFF and ON alerts if the OFF alert was never "
    "sent (if it already went out, its queued ON alert is still sent, so the channel is not "
    "left at power off);"
)
REMOVE_C4 = (
    "leaves the live status unchanged, including the On since time from which the next OFF "
    'alert counts "was ON for".'
)
REMOVE_CHART = (
    "The chart updates within 15 minutes. It shows the last 7 days; charts already posted for "
    "earlier days do not change."
)
# A key whose tail has upper-case letters, so no page text or hex marker holds it by chance.
KEY = "abcdefghijklmnopqrstuvwx0123QZXK"


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _detail(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _delete(location: Any) -> str:
    return f"/locations/{location.pk}/delete/"


def _get(client: Client, url: str, value: str | None = None) -> HttpResponse:
    """GET ``url``, with the fragment header set to ``value`` (None: no header)."""
    headers = {} if value is None else {FRAGMENT: value}
    response: HttpResponse = client.get(url, headers=headers)
    return response


def _vary(response: HttpResponse) -> list[str]:
    return [part.strip().lower() for part in str(response.get("Vary", "")).split(",")]


def assert_confirmation_headers(response: HttpResponse) -> None:
    """Both variants: Vary names the fragment header, the response is never cached."""
    assert FRAGMENT.lower() in _vary(response), response.get("Vary")
    assert "no-store" in str(response.get("Cache-Control", "")), response.get("Cache-Control")
    assert response.get("Content-Security-Policy"), "no CSP header"


def fragment_root(response: HttpResponse) -> Tag:
    """The fragment response's one confirm root, after the fragment invariants."""
    assert response.status_code == 200, response.status_code
    assert response.get(FRAGMENT) == "1", "the fragment does not carry X-PM-Fragment: 1"
    assert_confirmation_headers(response)
    soup = parse(response)
    for tag in ("html", "head", "body", "title"):
        assert soup.find_all(tag) == [], f"the fragment has a <{tag}>"
    for hook in SHELL_HOOKS:
        assert all_by_testid(soup, hook) == [], f"the fragment has the {hook!r} hook"
    return by_testid(soup, "confirm")


def page_root(response: HttpResponse, title: str) -> tuple[BeautifulSoup, Tag]:
    """The full page and its one confirm root, after the page invariants."""
    soup = assert_page(response, app=True, title=title)
    assert not response.has_header(FRAGMENT), "the full page carries X-PM-Fragment"
    assert_confirmation_headers(response)
    root = by_testid(main(soup), "confirm")
    return soup, root


def _controls(root: Tag) -> list[Tag]:
    """Every link and button inside the confirm root, in document order."""
    return [found for found in root.find_all(["a", "button"]) if isinstance(found, Tag)]


def assert_confirm_form(root: Tag, *, action: str, keep_href: str, keep: str, submit: str) -> Tag:
    """Keep first, the danger submit last, one POST form with CSRF; returns the form."""
    title = by_testid(root, "confirm-title")
    assert (title.name, title.get("id")) == ("h1", "confirm-title")
    assert len(root.find_all("h1")) == 1
    assert root.find_all(autofocus=True) == [], "a confirmation has no autofocus"
    keep_link = by_testid(root, "keep")
    assert keep_link.name == "a"
    assert (keep_link.get("href"), text(keep_link)) == (keep_href, keep)
    assert keep_link.get("data-variant") == "secondary"
    button = by_testid(root, "confirm-submit")
    assert (button.name, button.get("type"), button.get("data-variant")) == (
        "button",
        "submit",
        "danger",
    )
    assert text(button) == submit
    # Keep first, the destructive button last, nothing between them.
    assert _controls(root)[-2:] == [keep_link, button]
    forms = root.find_all("form")
    assert len(forms) == 1
    form = by_testid(root, "confirm-form")
    assert form is forms[0]
    assert (str(form.get("method")).lower(), form.get("action")) == ("post", action)
    assert button.find_parent("form") is form
    tokens = [
        found
        for found in form.find_all("input")
        if found.get("name") == "csrfmiddlewaretoken" and found.get("type") == "hidden"
    ]
    assert len(tokens) == 1, "the confirm form has no CSRF input"
    # Its only submit is confirm-submit; any other button is type="button".
    for other in form.find_all("button"):
        if other is not button:
            assert other.get("type") == "button", other
    assert [found for found in form.find_all("input") if found.get("type") == "submit"] == []
    return form


def _location(location_factory: Callable[..., Any], **fields: Any) -> Location:
    location: Location = location_factory(**fields)
    return location


def _regenerate(location: Any) -> str:
    return f"/locations/{location.pk}/setup/regenerate/"


def _setup(location: Any) -> str:
    return f"/locations/{location.pk}/setup/"


def _remove(location: Any, start: datetime) -> str:
    return f"/locations/{location.pk}/outages/{ops.instant_us(start)}/remove/"


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _pin(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """Every default clock (every SystemClock instance) reads ``now``."""
    monkeypatch.setattr(SystemClock, "now", lambda self: now)


def _no_anchors() -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _two_outages(location_factory: Callable[..., Any], **fields: Any) -> Location:
    """On since 08:00, outages 09:00-10:00 and 15:00-15:30 (UTC), on again since 15:30."""
    _no_anchors()
    location = _location(location_factory, **fields)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    assert transitions.record_heartbeat(location.pk, _at(15, 0)) == "plain"
    assert detection.run_cycle(_at(15, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(15, 30)) == "restored"
    return location


def _off_since_9(location_factory: Callable[..., Any], **fields: Any) -> Location:
    """On since 08:00, OFF from 09:00 (UTC): the outage is in progress."""
    _no_anchors()
    location = _location(location_factory, **fields)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    return location


def _written(location: Any) -> tuple[Any, ...]:
    """What a confirmation GET must never change: the timeline, the state and the key."""
    intervals = PowerInterval.objects.filter(location=location).order_by("start_at")
    state = LocationState.objects.filter(location=location)
    return (
        list(intervals.values_list("state", "start_at", "end_at", "outage_start_at")),
        list(state.values_list("status", "on_since", "outage_started_at", "state_version")),
        list(Location.objects.filter(pk=location.pk).values_list("device_key", "deleted_at")),
    )


def assert_one_flash(page: HttpResponse, level: str, flash: str) -> None:
    """Exactly one flash, of ``level``: a toast, or a legacy callout until S5 is rebuilt."""
    found = messages(page)
    role = "alert" if level == "error" else "status"
    assert len(found) == 1, found
    assert (found[0].role, found[0].text) == (role, flash)
    assert found[0].level in (None, level), found


# S7 delete (UI-07): the page in the app shell and the fragment, from one partial


@pytest.mark.django_db
def test_UI07_delete_page_and_fragment(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = _location(location_factory, name="Office")
    url = _delete(location)

    page = _get(admin, url)
    fragment = _get(admin, url, "1")

    soup, page_confirm = page_root(page, "Office · Delete")
    root = fragment_root(fragment)
    # Breadcrumbs in the top bar and their compact copy under the header.
    trail = [("Locations", "/"), ("Office", _detail(location)), ("Delete", None)]
    assert breadcrumbs(soup) == trail
    assert breadcrumbs(soup, "breadcrumbs-compact") == trail
    # The page's one h1 is the partial's title.
    assert h1(soup) is by_testid(page_confirm, "confirm-title")
    # One shared partial: the same text, the same form.
    assert text(root) == text(page_confirm)
    assert text(by_testid(root, "confirm-title")) == "Delete Office?"
    for confirm in (root, page_confirm):
        consequences = by_testid(confirm, "consequences")
        assert [text(item) for item in consequences.find_all("li")] == DELETE_CONSEQUENCES
        words = text(confirm)
        assert DELETE_LEAD in words
        assert DELETE_PAUSE in words
        assert DELETE_RECREATE in words
        assert words.index(DELETE_LEAD) < words.index(DELETE_PAUSE) < words.index(DELETE_RECREATE)
    # Nothing was written by either GET.
    assert Location.objects.get(pk=location.pk).deleted_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("variant", [None, "1"], ids=["page", "fragment"])
def test_UI07_delete_buttons(
    admin: Client, location_factory: Callable[..., Any], variant: str | None
) -> None:
    location = _location(location_factory, name="Office")
    response = _get(admin, _delete(location), variant)

    root = fragment_root(response) if variant else page_root(response, "Office · Delete")[1]

    assert_confirm_form(
        root,
        action=_delete(location),
        keep_href=_detail(location),
        keep="Keep location",
        submit="Delete location",
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value", [None, "0", "true", "", "TRUE", "1 ", "01"], ids=lambda v: repr(v)
)
def test_UI07_delete_header_values(
    admin: Client, location_factory: Callable[..., Any], value: str | None
) -> None:
    location = _location(location_factory, name="Office")

    response = _get(admin, _delete(location), value)

    # Only the exact value "1" gives the fragment; anything else the full page.
    page_root(response, "Office · Delete")
    assert parse(response).find("html") is not None


@pytest.mark.django_db
def test_UI07_delete_header_exact_one_gives_the_fragment(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = _location(location_factory, name="Office")

    fragment_root(_get(admin, _delete(location), "1"))


@pytest.mark.django_db
@pytest.mark.parametrize("variant", [None, "1"], ids=["page", "fragment"])
def test_UI07_delete_refusals_match(
    admin: Client, location_factory: Callable[..., Any], variant: str | None
) -> None:
    location = _location(location_factory, name="Office")
    gone = _location(location_factory, name="Gone")
    Location.objects.filter(pk=gone.pk).update(deleted_at=location.created_at)

    # Unknown and deleted locations: 404 in both variants.
    for url in (_delete(gone), f"/locations/{gone.pk + 1000}/delete/"):
        assert _get(admin, url, variant).status_code == 404, url
    # Anonymous: the same redirect to sign in in both variants.
    admin.logout()
    anonymous = _get(admin, _delete(location), variant)
    assert anonymous.status_code == 302
    assert anonymous["Location"] == f"/login/?next={_delete(location)}"
    assert not anonymous.has_header(FRAGMENT)


# S9 regenerate the key (UI-07, R4): page and fragment, the marker and the state blocks


def _set(location: Any, *, maintenance_on: bool, status: str) -> None:
    Location.objects.filter(pk=location.pk).update(maintenance=maintenance_on)
    if status == "waiting":
        return
    LocationState.objects.filter(location=location).update(
        status=status,
        on_since=_at(8, 0),
        last_heartbeat_at=_at(8, 0),
        outage_started_at=_at(8, 0) if status == "off" else None,
    )


@pytest.mark.django_db
def test_UI07_regenerate_page_and_fragment(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = _location(location_factory, name="Office", device_key=KEY)
    url = _regenerate(location)

    page = _get(admin, url)
    fragment = _get(admin, url, "1")

    soup, page_confirm = page_root(page, "Office · Regenerate key")
    root = fragment_root(fragment)
    trail = [
        ("Locations", "/"),
        ("Office", _detail(location)),
        ("Device setup", _setup(location)),
        ("Regenerate key", None),
    ]
    assert breadcrumbs(soup) == trail
    assert breadcrumbs(soup, "breadcrumbs-compact") == trail
    assert h1(soup) is by_testid(page_confirm, "confirm-title")
    assert text(root) == text(page_confirm)
    assert text(by_testid(root, "confirm-title")) == "Regenerate the device key?"
    assert REGEN_LEAD in text(root)
    # The same form and the same marker, the HMAC of the current key (R4).
    markers = []
    for confirm in (page_confirm, root):
        form = assert_confirm_form(
            confirm,
            action=url,
            keep_href=_setup(location),
            keep="Keep current key",
            submit="Regenerate key",
        )
        markers.append(hidden_value(form, "marker"))
    assert markers == [regenerate_marker(KEY)] * 2
    # No key, key mask or key tail in either variant, outside the hidden marker too.
    for body in (page.content.decode(), fragment.content.decode()):
        for secret in (KEY, keys.mask_key(KEY), KEY[-4:]):
            assert secret not in body
    assert "•" not in str(main(soup))
    assert "•" not in fragment.content.decode()
    assert Location.objects.get(pk=location.pk).device_key == KEY


@pytest.mark.django_db
@pytest.mark.parametrize("variant", [None, "1"], ids=["page", "fragment"])
@pytest.mark.parametrize(
    ("maintenance_on", "status", "block", "tone", "copy"),
    [
        (False, "on", "warning-on", "warning", REGEN_WARNING_ON.format(off_after=65)),
        (False, "off", "power-off", "info", REGEN_POWER_OFF),
        (False, "waiting", "waiting", "info", REGEN_WAITING),
        (True, "on", "maintenance", "info", REGEN_MAINTENANCE),
        (True, "off", "maintenance", "info", REGEN_MAINTENANCE),
        (True, "waiting", "maintenance", "info", REGEN_MAINTENANCE),
    ],
)
def test_UI07_regenerate_fragment_state_blocks(
    admin: Client,
    location_factory: Callable[..., Any],
    variant: str | None,
    maintenance_on: bool,
    status: str,
    block: str,
    tone: str,
    copy: str,
) -> None:
    location = _location(location_factory, name="Office", period_s=45, grace_s=20)
    _set(location, maintenance_on=maintenance_on, status=status)

    response = _get(admin, _regenerate(location), variant)

    root = fragment_root(response) if variant else page_root(response, "Office · Regenerate key")[1]
    # Exactly one state block, chosen by maintenance and the stored status (D-15).
    [found] = all_by_testid(root, "state-block")
    assert (found.get("data-state-block"), found.get("data-tone")) == (block, tone)
    words = text(found)
    assert copy in words
    links = found.find_all("a")
    if block == "warning-on":
        # The warning starts with its visually hidden tone prefix and links to the location.
        assert words.startswith("Warning: ")
        assert [(link.get("href"), text(link)) for link in links] == [
            (_detail(location), "Open the location page")
        ]
    else:
        assert words == copy
        assert links == []
    # No consequence list on S9: the lead and the state block say it all.
    assert all_by_testid(root, "consequences") == []


# S10 remove an outage (UI-07, R12): page and fragment, the details and the refusals


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """Pin the display TZ, so the expected times do not depend on the env file."""
    settings.TIME_ZONE = KYIV
    return settings


@pytest.mark.django_db(transaction=True)
def test_UI07_remove_page_and_fragment(
    admin: Client,
    kyiv: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
) -> None:
    location = _two_outages(location_factory, name="Office")
    _pin(monkeypatch, _at(16, 0))
    url = _remove(location, _at(9, 0))
    before = _written(location)

    page = _get(admin, url)
    fragment = _get(admin, url, "1")

    soup, page_confirm = page_root(page, "Office · Remove outage")
    root = fragment_root(fragment)
    trail = [("Locations", "/"), ("Office", _detail(location)), ("Remove outage", None)]
    assert breadcrumbs(soup) == trail
    assert breadcrumbs(soup, "breadcrumbs-compact") == trail
    assert h1(soup) is by_testid(page_confirm, "confirm-title")
    assert text(root) == text(page_confirm)
    for confirm in (page_confirm, root):
        # The h1 carries no name (UI5-D13).
        assert text(by_testid(confirm, "confirm-title")) == "Remove this outage?"
        assert definitions(confirm, "outage-details") == [
            ("Start", "2026-10-01 12:00:00 EEST"),
            ("End", "2026-10-01 13:00:00 EEST"),
            ("Off time", "1h"),
        ]
        consequences = by_testid(confirm, "consequences")
        # No not-monitored time inside this outage: no consequence 2; 3 is amendment A3.
        assert [text(item) for item in consequences.find_all("li")] == [
            REMOVE_C1,
            REMOVE_C3,
            REMOVE_C4,
        ]
        words = text(confirm)
        assert words.index(REMOVE_LEAD) < words.index(REMOVE_C1) < words.index(REMOVE_CHART)
        assert_confirm_form(
            confirm,
            action=url,
            keep_href=f"{_detail(location)}#recent-outages",
            keep="Keep outage",
            submit="Remove outage",
        )
    # The GETs wrote nothing.
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("variant", [None, "1"], ids=["page", "fragment"])
def test_UI07_remove_fragment_shows_consequence_2_only_when_needed(
    admin: Client,
    kyiv: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    variant: str | None,
) -> None:
    paused = _off_since_9(location_factory, name="Paused")
    assert maintenance.set_maintenance(paused.pk, True, _at(10, 0)) is True
    assert maintenance.set_maintenance(paused.pk, False, _at(10, 10)) is True
    assert transitions.record_heartbeat(paused.pk, _at(11, 0)) == "restored"
    _pin(monkeypatch, _at(16, 0))

    response = _get(admin, _remove(paused, _at(9, 0)), variant)

    root = fragment_root(response) if variant else page_root(response, "Paused · Remove outage")[1]
    # 09:00-11:00 with 10:00-10:10 not monitored: off time is shorter than the span.
    assert definitions(root, "outage-details") == [
        ("Start", "2026-10-01 12:00:00 EEST"),
        ("End", "2026-10-01 14:00:00 EEST"),
        ("Off time", "1h 50m"),
    ]
    consequences = by_testid(root, "consequences")
    assert [text(item) for item in consequences.find_all("li")] == [
        REMOVE_C1,
        REMOVE_C2,
        REMOVE_C3,
        REMOVE_C4,
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("variant", [None, "1"], ids=["page", "fragment"])
def test_UI07_remove_form_uses_the_stored_start(
    admin: Client,
    kyiv: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    variant: str | None,
) -> None:
    location = _two_outages(location_factory, name="Office")
    _pin(monkeypatch, _at(16, 0))
    start_us = ops.instant_us(_at(9, 0))
    # Leading zeros still name the stored outage; the form never echoes them (R12).
    padded = f"/locations/{location.pk}/outages/000{start_us}/remove/"

    response = _get(admin, padded, variant)

    root = fragment_root(response) if variant else page_root(response, "Office · Remove outage")[1]
    form = by_testid(root, "confirm-form")
    assert form.get("action") == _remove(location, _at(9, 0))
    assert f"000{start_us}" not in response.content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("case", "level", "flash"),
    [("gone", "info", OUTAGE_GONE_MESSAGE), ("in-progress", "error", REMOVAL_REFUSED_MESSAGE)],
)
def test_UI07_remove_refusals(
    admin: Client,
    kyiv: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    case: str,
    level: str,
    flash: str,
) -> None:
    location = _off_since_9(location_factory, name="Office")
    _pin(monkeypatch, _at(9, 30))
    # Gone: no outage starts at 08:30. In progress: the outage from 09:00.
    url = _remove(location, _at(8, 30) if case == "gone" else _at(9, 0))
    before = _written(location)

    fragment = _get(admin, url, "1")
    page = _get(admin, url)

    # The same refusal in both variants: 302 to the location page, never cached.
    for response in (fragment, page):
        assert response.status_code == 302
        assert response["Location"] == _detail(location)
        assert not response.has_header(FRAGMENT)
        assert_confirmation_headers(response)
    # Only the full-page GET queued its flash: the location page shows it exactly once.
    assert_one_flash(admin.get(_detail(location)), level, flash)
    # The browser's sequence: the fragment GET (refused, no flash), then the full GET.
    assert _get(admin, url, "1").status_code == 302
    shown = admin.get(url, follow=True)
    assert shown.redirect_chain == [(_detail(location), 302)]
    assert_one_flash(shown, level, flash)
    # A fragment GET alone queues nothing.
    assert _get(admin, url, "1").status_code == 302
    assert messages(admin.get(_detail(location))) == []
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("variant", [None, "1"], ids=["page", "fragment"])
def test_UI07_remove_404s_match(
    admin: Client,
    kyiv: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    variant: str | None,
) -> None:
    location = _two_outages(location_factory, name="Office")
    _pin(monkeypatch, _at(16, 0))
    start_us = ops.instant_us(_at(9, 0))

    for path in (
        f"/locations/{location.pk + 1000}/outages/{start_us}/remove/",
        # Digits, but no valid instant: out of the datetime range; never echoed (R12).
        f"/locations/{location.pk}/outages/{10**20}/remove/",
    ):
        response = _get(admin, path, variant)
        assert response.status_code == 404, path
        assert not response.has_header(FRAGMENT)
        assert str(10**20) not in response.content.decode()
