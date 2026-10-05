"""The location pages through the signed-in shell (D-09 surfaces 2-4, UI-SPEC screens 2-4).

The list is read-only: one row per location, sorted by name without regard to case (ties
by id), with the Phase 4 status label (Maintenance whenever the flag is on) and its
"Alerts off" / "Router grace" tags, and the last heartbeat in the display TZ
(``display_time``, P-3), then its delivery health ("OK" while no delivery-failing incident
is open; tests/web/test_delivery_display.py covers "Failing since …"). The Language column
is gone (Phase 4 UI-D1): the four columns are Name, Status, Last heartbeat and Delivery
(D-13). With no locations it shows the empty-state panel instead of the table.
While the admin ops chat is not configured, both states show a warning callout (D-09,
INV-20). Every page escapes the user-typed location name (UI-SPEC security rule 1).
"""

# class-guard: pending migration

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import DatabaseError
from django.test import Client

from powermon.engine.models import LocationState
from powermon.locations.models import Location

User = get_user_model()

XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
OPS_WARNING = '<p class="callout"><strong>Warning:</strong> The ops chat is not configured'
OPS_TOKEN = "555555555:" + "C" * 35


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """Pin the display TZ, so the expected times do not depend on the env file."""
    settings.TIME_ZONE = "Europe/Kyiv"
    return settings


def _set_state(location: Any, **fields: Any) -> None:
    LocationState.objects.filter(location=location).update(**fields)


def _text(fragment: str) -> str:
    """The visible text of an HTML fragment: tags dropped, whitespace collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def _h1_text(page: str) -> str:
    """The page's first h1 as text: tags dropped, whitespace collapsed, entities decoded.

    So an h1 with attributes or an aria-hidden icon inside still reads as its copy.
    """
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S)
    assert match is not None, "no h1 on the page"
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())


def _table_rows(html: str) -> list[list[str]]:
    """The text of every body cell, row by row."""
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    assert body is not None, "no table body in the page"
    rows = re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S)
    return [
        [_text(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)] for row in rows
    ]


def _headers(html: str) -> list[str]:
    """The column headers of the location table, in order."""
    return re.findall(r'<th scope="col">([^<]*)</th>', html)


def _status_cells(html: str) -> list[str]:
    """The markup inside each row's ``.status-cell``, in row order."""
    return re.findall(r'<div class="status-cell">(.*?)</div>', html, re.S)


# Location list


def test_list_empty_state(admin: Client) -> None:
    response = admin.get("/")

    assert response.status_code == 200
    html = response.content.decode()
    assert "<title>Locations · Power Monitor</title>" in html
    assert "<h1>Locations</h1>" in html
    assert "<h2>No locations yet</h2>" in html
    assert "Add a location to get its heartbeat URL, device key and setup examples." in html
    # One accent button only: the panel's, not the heading row's.
    assert html.count(">Add location</a>") == 1
    assert html.count("btn--primary") == 1
    assert 'href="/locations/new/"' in html
    assert "<table" not in html


@pytest.mark.django_db
def test_list_rows_sorted_with_labels(
    admin: Client, kyiv: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    beta = location_factory(name="beta", language="uk")
    alpha = location_factory(name="Alpha", language="en")
    gamma = location_factory(name="gamma", language="ru")
    delta = location_factory(name="Delta", language="en")
    _set_state(alpha, status="on", last_heartbeat_at=fixed_now, on_since=fixed_now)
    gone = fixed_now - timedelta(minutes=2)
    _set_state(gamma, status="off", last_heartbeat_at=gone, outage_started_at=gone)

    html = admin.get("/").content.decode()

    # Case-insensitive: a plain code-point sort would put "Delta" before "beta".
    assert _table_rows(html) == [
        ["Alpha", "On", "2026-10-01 11:00:00 EEST", "OK"],
        ["beta", "Waiting for first heartbeat", "Never", "OK"],
        ["Delta", "Waiting for first heartbeat", "Never", "OK"],
        ["gamma", "Off", "2026-10-01 10:58:00 EEST", "OK"],
    ]
    # Each name opens the location's page (Phase 4 UI-SPEC screen A), not its setup page.
    for location in (alpha, beta, gamma, delta):
        assert f'<a class="name" href="/locations/{location.pk}/">' in html
        assert f'href="/locations/{location.pk}/setup/"' not in html
    # UI-D1: no Language column; Delivery is the fourth (D-13).
    assert _headers(html) == ["Name", "Status", "Last heartbeat", "Delivery"]
    for label in ("English", "Ukrainian", "Russian"):
        assert label not in html
    assert '<span class="status status--on">On</span>' in html
    assert '<span class="status status--off">Off</span>' in html
    assert '<span class="status status--waiting">Waiting for first heartbeat</span>' in html
    # The table scrolls inside its wrapper on narrow screens; the heading row has the button.
    assert re.search(r'<div class="table-wrap">\s*<table>', html)
    assert html.count(">Add location</a>") == 1
    assert "No locations yet" not in html


@pytest.mark.django_db
def test_list_rows_use_the_phase4_vocabulary_and_tags(
    admin: Client, kyiv: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    # Two names that differ only in case: the lower id goes first (LOC-03 ordering).
    first = location_factory(
        name="office", maintenance=True, alerts_enabled=False, router_grace=True
    )
    second = location_factory(name="Office", alerts_enabled=False)
    location_factory(name="Basement", router_grace=True)
    _set_state(first, status="on", last_heartbeat_at=fixed_now, on_since=fixed_now)
    _set_state(second, status="off", last_heartbeat_at=fixed_now, outage_started_at=fixed_now)

    html = admin.get("/").content.decode()

    assert _headers(html) == ["Name", "Status", "Last heartbeat", "Delivery"]
    assert _table_rows(html) == [
        ["Basement", "Waiting for first heartbeat Router grace", "Never", "OK"],
        ["office", "Maintenance Alerts off Router grace", "2026-10-01 11:00:00 EEST", "OK"],
        ["Office", "Off Alerts off", "2026-10-01 11:00:00 EEST", "OK"],
    ]
    # Maintenance is shown whenever the flag is on, with the grey dot (UI-D11); the tags
    # follow the status label, "Alerts off" before "Router grace".
    assert _status_cells(html) == [
        '<span class="status status--waiting">Waiting for first heartbeat</span>'
        '<span class="tag">Router grace</span>',
        '<span class="status status--maintenance">Maintenance</span>'
        '<span class="tag">Alerts off</span><span class="tag">Router grace</span>',
        '<span class="status status--off">Off</span><span class="tag">Alerts off</span>',
    ]
    assert html.index(f'href="/locations/{first.pk}/"') < html.index(
        f'href="/locations/{second.pk}/"'
    )


@pytest.mark.django_db
def test_list_hides_deleted_locations(admin: Client, location_factory: Callable[..., Any]) -> None:
    location_factory(name="kept")
    location_factory(name="tombstoned", deleted_at=datetime(2026, 9, 1, tzinfo=UTC))

    html = admin.get("/").content.decode()

    assert [row[0] for row in _table_rows(html)] == ["kept"]
    assert "tombstoned" not in html


@pytest.mark.django_db
def test_list_last_heartbeat_uses_display_time(
    admin: Client, kyiv: Any, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    # 01:30 UTC on the fall-back day is the second 03:30 in Kyiv (P-3).
    beat = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    _set_state(location, status="on", last_heartbeat_at=beat, on_since=beat)

    html = admin.get("/").content.decode()

    assert _table_rows(html) == [["Office", "On", "2026-10-25 03:30:00 EET", "OK"]]
    assert '<td class="num">2026-10-25 03:30:00 EET</td>' in html


@pytest.mark.django_db
def test_list_location_without_state_row_shows_waiting(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # Locations are always created with their state row; a missing one must not crash.
    location = location_factory(name="Orphan")
    LocationState.objects.filter(location=location).delete()

    html = admin.get("/").content.decode()

    # No state row and no delivery incident: waiting, never, OK (E1 partial).
    assert _table_rows(html) == [["Orphan", "Waiting for first heartbeat", "Never", "OK"]]


@pytest.mark.django_db
def test_list_shows_twenty_locations_on_one_page(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    for n in range(20):
        location_factory(name=f"Location {n:02d}")

    html = admin.get("/").content.decode()

    rows = _table_rows(html)
    assert [row[0] for row in rows] == [f"Location {n:02d}" for n in range(20)]
    # No pagination and no count in the heading, whatever the number of rows.
    assert "<h1>Locations</h1>" in html
    assert "page=" not in html
    assert "20" not in _text(html.split("<tbody>")[0])


def test_list_has_no_live_refresh(admin: Client) -> None:
    html = admin.get("/").content.decode()

    assert "http-equiv" not in html
    assert "<script" not in html


@pytest.mark.django_db
def test_list_database_failure_renders_500_without_a_table(
    admin: Client, location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    location_factory(name="Office")

    def unreachable(*args: Any, **kwargs: Any) -> None:
        raise DatabaseError("could not connect to server")

    monkeypatch.setattr(Location.objects, "filter", unreachable)
    admin.raise_request_exception = False

    response = admin.get("/")

    assert response.status_code == 500
    html = response.content.decode()
    assert _h1_text(html) == "Something went wrong"
    assert "<table" not in html
    assert "Office" not in html
    assert "could not connect" not in html


@pytest.mark.django_db
def test_list_warns_when_the_ops_chat_is_not_configured(
    admin: Client, settings: Any, location_factory: Callable[..., Any]
) -> None:
    settings.CFG = replace(settings.CFG, ops_bot_token="", ops_chat_id=None)

    empty = admin.get("/").content.decode()
    location_factory(name="Office")
    table = admin.get("/").content.decode()

    assert "<h2>No locations yet</h2>" in empty
    assert "<table" in table
    for html in (empty, table):
        assert html.count(OPS_WARNING) == 1
        assert "Set OPS_BOT_TOKEN and OPS_CHAT_ID in the env file" in html
        assert "go to the worker log only." in html


@pytest.mark.django_db
def test_list_has_no_ops_warning_when_configured(
    admin: Client, settings: Any, location_factory: Callable[..., Any]
) -> None:
    settings.CFG = replace(settings.CFG, ops_bot_token=OPS_TOKEN, ops_chat_id=-1005555555555)

    empty = admin.get("/").content.decode()
    location_factory(name="Office")
    table = admin.get("/").content.decode()

    assert "<h2>No locations yet</h2>" in empty
    assert "<table" in table
    for html in (empty, table):
        assert "ops chat is not configured" not in html
        assert "<strong>Warning:</strong>" not in html
        assert OPS_TOKEN not in html


def test_anonymous_list_redirects_to_sign_in(client: Client, db: None) -> None:
    response = client.get("/")

    assert response.status_code == 302
    assert response.url == "/login/?next=/"


# Page shell


def test_signed_in_header_renders_nav_and_sign_out(admin: Client) -> None:
    html = admin.get("/").content.decode()

    nav = re.search(r'<nav class="site-nav" aria-label="Main">(.*?)</nav>', html, re.S)
    assert nav is not None
    assert '<a href="/" aria-current="page">Locations</a>' in nav.group(1)
    sign_out = re.search(r'<form class="site-header__signout"[^>]*>(.*?)</form>', html, re.S)
    assert sign_out is not None
    assert 'method="post" action="/logout/"' in sign_out.group(0)
    assert 'name="csrfmiddlewaretoken"' in sign_out.group(1)
    assert ">Sign out</button>" in sign_out.group(1)


# Escaping and wrapping of the user-typed name. The list page's half of each test is here;
# the setup page's half is *_on_setup in tests/web/test_setup_page.py (06-09).


@pytest.mark.django_db
def test_xss_name_is_escaped_everywhere(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location_factory(name=XSS_NAME)

    listing = admin.get("/").content.decode()

    assert _table_rows(listing)[0][0] == ESCAPED_XSS_NAME
    assert f">{ESCAPED_XSS_NAME}</a>" in listing
    assert "<script" not in listing


@pytest.mark.django_db
def test_long_name_has_the_wrapping_class(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "x" * 100
    location = location_factory(name=name)

    listing = admin.get("/").content.decode()

    assert f'<a class="name" href="/locations/{location.pk}/">{name}</a>' in listing


# Static assets


def test_app_css_is_in_the_manifest() -> None:
    stored = staticfiles_storage.stored_name("web/app.css")

    assert re.fullmatch(r"web/app\.[0-9a-f]{12}\.css", stored)


def test_unknown_static_file_is_not_in_the_manifest() -> None:
    with pytest.raises(ValueError, match="Missing staticfiles manifest entry"):
        staticfiles_storage.stored_name("web/missing.css")
