"""The Locations page S3 in the app shell (06-UI-SPEC Page Contracts > S3; UI-01, UI-11, UI-12).

- The page extends the app layout: h1 "Locations", the meta count "{N} locations" ("1
  location"), the primary "Add location" in the page actions, and the table
  ``locations-table`` with its caption and the columns Name, Status, Last heartbeat and
  Delivery. Each ``location-row`` carries its id, status and delivery as data hooks; the
  name is the row's only link and opens the location's page.
- Rows sort by name without regard to case, ties by the lower id. The status is the Phase 4
  label (Maintenance whenever the flag is on) with the tags "Alerts off" and "Router grace"
  after it, in that order. Deleted locations are never listed; there is no pagination.
- The last heartbeat is a ``<time datetime>`` holding the unchanged ``display_time`` text,
  next to one ``[data-relative]`` with the same instant (UI-11); "Never" has no time.
- With no locations the page shows the empty state and no table, no meta count and no
  header button. While the ops chat is not configured, the warning banner sits above the
  page header (Phase 2 D-09, INV-20). Every page escapes the user-typed name (R1).

Rows and cells are read only through ``pages.table(page, "locations-table")`` or inside
each ``tr[data-testid=location-row]``, never page-wide, because 06-18 adds the phone cards
with the same hooks. ``_shown`` drops every element carrying the ``hidden`` attribute
first: a hidden variant (the inactive delivery variant the poll switches) is not shown.
tests/web/test_delivery_display.py covers the Delivery column ("Failing since …").
"""

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import DatabaseError
from django.http.response import HttpResponseBase
from django.test import Client
from pages import (
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    assert_page,
    breadcrumbs,
    by_testid,
    h1,
    main,
    parse,
    table,
    text,
)

from powermon.engine.models import LocationState
from powermon.locations.models import Location
from powermon.web.templatetags.display_time import display_time, display_time_compact
from powermon.web.templatetags.timefmt import relative_text
from powermon.web.views import LocationListView

User = get_user_model()

XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
OPS_TOKEN = "555555555:" + "C" * 35
HEADERS = ["Name", "Status", "Last heartbeat", "Delivery"]
# Copy rows list.ops_title and list.ops_body, after the alert's sr-only "Warning: ".
OPS_BANNER_TEXT = (
    "Warning: The ops chat is not configured. Set OPS_BOT_TOKEN and OPS_CHAT_ID in the env "
    "file and run the deploy command; until then, ops notices (monitoring gaps, all-silent, "
    "expired or unconfirmed alerts, database outages) go to the worker log only."
)
EMPTY_TITLE = "No locations yet"
EMPTY_BODY = "Add a location to get its heartbeat URL, device key and setup examples."


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


@pytest.fixture
def list_clock(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> FakeClock:
    """The list view's clock, at ``fixed_now``: the relative times do not depend on today."""
    clock = FakeClock(fixed_now)
    monkeypatch.setattr(LocationListView, "clock", clock)
    return clock


def _set_state(location: Any, **fields: Any) -> None:
    LocationState.objects.filter(location=location).update(**fields)


def _shown(page: HttpResponseBase | str) -> BeautifulSoup:
    """The page parsed, without the elements that carry the ``hidden`` attribute."""
    soup = parse(page)
    for element in [found for found in soup.find_all(True) if found.has_attr("hidden")]:
        element.extract()
    return soup


def _rows(page: HttpResponseBase | str) -> list[list[str]]:
    """The cell texts of each shown row of the locations table."""
    headers, rows = table(_shown(page), "locations-table")
    assert headers == HEADERS
    return rows


def _row_elements(page: HttpResponseBase | str) -> list[Tag]:
    """The ``location-row`` elements of the locations table, in order."""
    return all_by_testid(by_testid(_shown(page), "locations-table"), "location-row")


def _in(row: Tag, testid: str) -> list[Tag]:
    """The elements of the row that carry ``testid``."""
    return all_by_testid(row, testid)


def _count(page: HttpResponseBase | str) -> str | None:
    """The text of the meta count item ("3 locations"), or None when the page has none."""
    values = main(parse(page)).find_all(attrs={"data-count-value": True})
    if not values:
        return None
    assert len(values) == 1, f"{len(values)} meta counts"
    item = values[0].find_parent("li")
    assert isinstance(item, Tag), "the meta count is not inside a meta line item"
    return text(item)


# The page in the shell (UI-01)


@pytest.mark.django_db
def test_UI01_list_in_the_shell(admin: Client, location_factory: Callable[..., Any]) -> None:
    location_factory(name="Office")

    soup = assert_page(admin.get("/"), title="Locations", app=True)

    assert text(h1(soup)) == "Locations"
    nav = by_testid(soup, "nav-locations")
    assert nav.get("aria-current") == "page"
    assert by_testid(soup, "nav-add-location").get("aria-current") is None
    # The one-item trail: in the top bar only, no compact copy under the h1.
    assert breadcrumbs(soup) == [("Locations", None)]
    assert all_by_testid(soup, "breadcrumbs-compact") == []
    # The header action: the primary link to the add form.
    add = by_testid(soup, "add-location")
    assert add.name == "a"
    assert add["href"] == "/locations/new/"
    assert add.get("data-variant") == "primary"
    assert text(add) == "Add location"
    assert add.find_parent(attrs={"data-testid": "page-actions"}) is not None
    # The table has its caption and scoped headers (UI-12).
    locations = by_testid(soup, "locations-table")
    caption = locations.find("caption")
    assert isinstance(caption, Tag)
    assert text(caption) == "Locations"
    assert [th.get("scope") for th in locations.find_all("th")] == ["col"] * 4 + ["row"]


def test_list_empty_state(admin: Client) -> None:
    response = admin.get("/")

    soup = assert_page(response, title="Locations", app=True)
    assert text(h1(soup)) == "Locations"
    empty = by_testid(soup, "empty-state")
    assert EMPTY_TITLE in text(empty)
    assert EMPTY_BODY in text(empty)
    # One "Add location" link only: the empty state's; the header has no button.
    add = by_testid(soup, "add-location")
    assert add["href"] == "/locations/new/"
    assert add.find_parent(attrs={"data-testid": "empty-state"}) is not None
    assert text(by_testid(soup, "page-actions")) == ""
    assert soup.find("table") is None


@pytest.mark.django_db
def test_UI01_empty_list(admin: Client, location_factory: Callable[..., Any]) -> None:
    # A deleted location is not listed: the list is still empty.
    location_factory(name="tombstoned", deleted_at=datetime(2026, 9, 1, tzinfo=UTC))

    response = admin.get("/")

    soup = parse(response)
    empty = by_testid(soup, "empty-state")
    assert [text(p) for p in empty.find_all("p")] == [EMPTY_TITLE, EMPTY_BODY]
    add = by_testid(empty, "add-location")
    assert add["href"] == "/locations/new/"
    assert add.get("data-variant") == "primary"
    assert text(add) == "Add location"
    # Nothing to list: no table, no fleet card, no cards, no meta count.
    for hook in ("locations-table", "location-row", "fleet-summary", "location-card"):
        assert all_by_testid(soup, hook) == [], hook
    assert _count(response) is None
    assert "tombstoned" not in response.content.decode()


# Rows (UI-01)


@pytest.mark.django_db
def test_list_rows_sorted_with_labels(
    admin: Client,
    kyiv: Any,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    beta = location_factory(name="beta", language="uk")
    alpha = location_factory(name="Alpha", language="en")
    gamma = location_factory(name="gamma", language="ru")
    delta = location_factory(name="Delta", language="en")
    _set_state(alpha, status="on", last_heartbeat_at=fixed_now, on_since=fixed_now)
    gone = fixed_now - timedelta(minutes=2)
    _set_state(gamma, status="off", last_heartbeat_at=gone, outage_started_at=gone)

    response = admin.get("/")

    # Case-insensitive: a plain code-point sort would put "Delta" before "beta".
    assert _rows(response) == [
        ["Alpha", "On", "just now 2026-10-01 11:00:00 EEST", "OK"],
        ["beta", "Waiting for first heartbeat", "Never", "OK"],
        ["Delta", "Waiting for first heartbeat", "Never", "OK"],
        ["gamma", "Off", "2 min ago 2026-10-01 10:58:00 EEST", "OK"],
    ]
    rows = _row_elements(response)
    ordered = (alpha, beta, delta, gamma)
    assert [row["data-location-id"] for row in rows] == [str(loc.pk) for loc in ordered]
    assert [row["data-status"] for row in rows] == ["on", "waiting", "waiting", "off"]
    assert [row["data-delivery"] for row in rows] == ["ok"] * 4
    assert [pill["data-status"] for row in rows for pill in _in(row, "status-pill")] == [
        "on",
        "waiting",
        "waiting",
        "off",
    ]
    # Each name opens the location's page, not its setup page; it is the row's only link.
    for row, location in zip(rows, ordered, strict=True):
        assert [link["href"] for link in row.find_all("a")] == [f"/locations/{location.pk}/"]
        assert [link["href"] for link in _in(row, "location-link")] == [
            f"/locations/{location.pk}/"
        ]
    # UI-D1: no Language column, and no language anywhere in the page content.
    content = text(main(parse(response)))
    for label in ("English", "Ukrainian", "Russian"):
        assert label not in content
    soup = parse(response)
    assert len(all_by_testid(soup, "add-location")) == 1
    assert all_by_testid(soup, "empty-state") == []
    assert _count(response) == "4 locations"


@pytest.mark.django_db
def test_list_rows_use_the_phase4_vocabulary_and_tags(
    admin: Client,
    kyiv: Any,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    # Two names that differ only in case: the lower id goes first (LOC-03 ordering).
    first = location_factory(
        name="office", maintenance=True, alerts_enabled=False, router_grace=True
    )
    second = location_factory(name="Office", alerts_enabled=False)
    basement = location_factory(name="Basement", router_grace=True)
    _set_state(first, status="on", last_heartbeat_at=fixed_now, on_since=fixed_now)
    _set_state(second, status="off", last_heartbeat_at=fixed_now, outage_started_at=fixed_now)

    response = admin.get("/")

    assert first.pk < second.pk
    assert _rows(response) == [
        ["Basement", "Waiting for first heartbeat Router grace", "Never", "OK"],
        [
            "office",
            "Maintenance Alerts off Router grace",
            "just now 2026-10-01 11:00:00 EEST",
            "OK",
        ],
        ["Office", "Off Alerts off", "just now 2026-10-01 11:00:00 EEST", "OK"],
    ]
    rows = _row_elements(response)
    assert [row["data-location-id"] for row in rows] == [
        str(basement.pk),
        str(first.pk),
        str(second.pk),
    ]
    # Maintenance whenever the flag is on (the power state underneath is On); the tags
    # follow the status pill, "Alerts off" before "Router grace".
    assert [row["data-status"] for row in rows] == ["waiting", "maintenance", "off"]
    assert [[pill["data-status"] for pill in _in(row, "status-pill")] for row in rows] == [
        ["waiting"],
        ["maintenance"],
        ["off"],
    ]
    assert [[tag["data-tag"] for tag in _in(row, "tag")] for row in rows] == [
        ["router-grace"],
        ["alerts-off", "router-grace"],
        ["alerts-off"],
    ]


@pytest.mark.django_db
def test_list_hides_deleted_locations(admin: Client, location_factory: Callable[..., Any]) -> None:
    location_factory(name="kept")
    location_factory(name="tombstoned", deleted_at=datetime(2026, 9, 1, tzinfo=UTC))

    response = admin.get("/")

    assert [row[0] for row in _rows(response)] == ["kept"]
    assert "tombstoned" not in response.content.decode()
    assert _count(response) == "1 location"


@pytest.mark.django_db
def test_list_last_heartbeat_uses_display_time(
    admin: Client, kyiv: Any, list_clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    # 01:30 UTC on the fall-back day is the second 03:30 in Kyiv (P-3).
    beat = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    _set_state(location, status="on", last_heartbeat_at=beat, on_since=beat)
    list_clock.set(beat + timedelta(minutes=5))

    response = admin.get("/")

    assert _rows(response) == [["Office", "On", "5 min ago 2026-10-25 03:30:00 EET", "OK"]]
    [row] = _row_elements(response)
    [cell] = _in(row, "last-heartbeat")
    [time] = cell.find_all("time")
    assert time["datetime"] == "2026-10-25T03:30:00+02:00"
    assert text(time) == "2026-10-25 03:30:00 EET"


@pytest.mark.django_db
def test_list_location_without_state_row_shows_waiting(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # Locations are always created with their state row; a missing one must not crash.
    location = location_factory(name="Orphan")
    LocationState.objects.filter(location=location).delete()

    response = admin.get("/")

    # No state row and no delivery incident: waiting, never, OK (E1 partial).
    assert _rows(response) == [["Orphan", "Waiting for first heartbeat", "Never", "OK"]]
    [row] = _row_elements(response)
    assert row["data-status"] == "waiting"


@pytest.mark.django_db
def test_list_shows_twenty_locations_on_one_page(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    for n in range(20):
        location_factory(name=f"Location {n:02d}")

    response = admin.get("/")

    assert [row[0] for row in _rows(response)] == [f"Location {n:02d}" for n in range(20)]
    # No pagination: every row on the one page, and the meta line counts them all.
    assert "page=" not in response.content.decode()
    assert _count(response) == "20 locations"


def test_UI05_no_meta_refresh(admin: Client) -> None:
    html = admin.get("/").content.decode()

    soup = parse(html)
    assert not [meta for meta in soup.find_all("meta") if str(meta.get("http-equiv", "")).lower()]
    # Only the layout's two script tags, both loaded by src with an empty body.
    assert_no_injected_script(html, "list")


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
    soup = parse(response)
    assert text(h1(soup)) == "Something went wrong"
    assert soup.find("table") is None
    assert all_by_testid(soup, "locations-table") == []
    html = response.content.decode()
    assert "Office" not in html
    assert "could not connect" not in html


# The ops-chat banner (Phase 2 D-09, INV-20)


@pytest.mark.django_db
def test_list_warns_when_the_ops_chat_is_not_configured(
    admin: Client, settings: Any, location_factory: Callable[..., Any]
) -> None:
    settings.CFG = replace(settings.CFG, ops_bot_token="", ops_chat_id=None)

    empty = admin.get("/")
    location_factory(name="Office")
    listed = admin.get("/")

    by_testid(parse(empty), "empty-state")
    by_testid(parse(listed), "locations-table")
    for response in (empty, listed):
        soup = parse(response)
        banner = by_testid(soup, "ops-chat-banner")
        assert banner.get("data-tone") == "warning"
        # Not a live message: no role.
        assert banner.get("role") is None
        assert text(banner) == OPS_BANNER_TEXT
        assert [text(code) for code in banner.find_all("code")] == ["OPS_BOT_TOKEN", "OPS_CHAT_ID"]
        # System-wide, so above the page header.
        assert h1(soup).find_all_previous(attrs={"data-testid": "ops-chat-banner"}) == [banner]


@pytest.mark.django_db
def test_list_has_no_ops_warning_when_configured(
    admin: Client, settings: Any, location_factory: Callable[..., Any]
) -> None:
    settings.CFG = replace(settings.CFG, ops_bot_token=OPS_TOKEN, ops_chat_id=-1005555555555)

    empty = admin.get("/")
    location_factory(name="Office")
    listed = admin.get("/")

    by_testid(parse(empty), "empty-state")
    by_testid(parse(listed), "locations-table")
    for response in (empty, listed):
        html = response.content.decode()
        assert all_by_testid(parse(html), "ops-chat-banner") == []
        assert "ops chat is not configured" not in html
        assert_no_secrets(html, [OPS_TOKEN], label="list")


def test_anonymous_list_redirects_to_sign_in(client: Client, db: None) -> None:
    response = client.get("/")

    assert response.status_code == 302
    assert response.url == "/login/?next=/"


# Page shell


def test_signed_in_header_renders_nav_and_sign_out(admin: Client) -> None:
    soup = parse(admin.get("/"))

    nav = soup.find("nav", attrs={"aria-label": "Main"})
    assert isinstance(nav, Tag)
    current = by_testid(nav, "nav-locations")
    assert current["href"] == "/"
    assert current.get("aria-current") == "page"
    assert text(current) == "Locations"
    sign_out = by_testid(soup, "sign-out-form")
    assert str(sign_out.get("method")).lower() == "post"
    assert sign_out.get("action") == "/logout/"
    assert sign_out.find("input", attrs={"name": "csrfmiddlewaretoken"}) is not None
    assert [text(button) for button in sign_out.find_all("button")] == ["Sign out"]


# Escaping and wrapping of the user-typed name. The list page's half of each test is here;
# the setup page's half is *_on_setup in tests/web/test_setup_page.py (06-09).


@pytest.mark.django_db
def test_xss_name_is_escaped_everywhere(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location_factory(name=XSS_NAME)

    listing = admin.get("/").content.decode()

    [row] = _row_elements(listing)
    [link] = _in(row, "location-link")
    # The parser decodes the entities, so the text and the title are the typed name.
    assert text(link) == XSS_NAME
    assert link["title"] == XSS_NAME
    assert ESCAPED_XSS_NAME in listing
    assert_no_injected_script(listing, "list")
    # The name never lands in the page's heading.
    assert text(h1(parse(listing))) == "Locations"


@pytest.mark.django_db
def test_long_name_has_the_wrapping_class(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "x" * 100
    location = location_factory(name=name)

    listing = admin.get("/")

    [row] = _row_elements(listing)
    [link] = _in(row, "location-link")
    # The 100-character name in full, in the link text and its title (UI-12).
    assert text(link) == name
    assert link["title"] == name
    assert link["href"] == f"/locations/{location.pk}/"


# Relative times (UI-11)


@pytest.mark.django_db
def test_UI11_list_times(
    admin: Client,
    kyiv: Any,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    on = location_factory(name="A on")
    off = location_factory(name="B off")
    location_factory(name="C waiting")
    on_beat = fixed_now - timedelta(seconds=90)
    off_beat = fixed_now - timedelta(hours=3, minutes=5)
    _set_state(on, status="on", last_heartbeat_at=on_beat, on_since=on_beat)
    _set_state(off, status="off", last_heartbeat_at=off_beat, outage_started_at=off_beat)

    response = admin.get("/")

    rows = _row_elements(response)
    for row, beat, relative in zip(
        rows[:2], (on_beat, off_beat), ("1 min ago", "3 h ago"), strict=True
    ):
        [cell] = _in(row, "last-heartbeat")
        [time] = cell.find_all("time")
        # The datetime parses to the stored aware instant; the text is unchanged.
        assert datetime.fromisoformat(str(time["datetime"])) == beat
        assert text(time) == display_time(beat)
        [compact] = time.find_all(attrs={"data-part": "compact"})
        assert text(compact) == display_time_compact(beat)
        # Exactly one relative time in the cell, with the same instant.
        [rel] = cell.find_all(attrs={"data-relative": True})
        assert rel["data-relative"] == time["datetime"]
        assert text(rel) == relative == relative_text(beat, fixed_now)
    # Never: no time element and no relative time.
    [never] = _in(rows[2], "last-heartbeat")
    assert never.find_all("time") == []
    assert never.find_all(attrs={"data-relative": True}) == []
    assert text(never) == "Never"


# Static assets


def test_app_css_is_in_the_manifest() -> None:
    stored = staticfiles_storage.stored_name("web/app.css")

    assert re.fullmatch(r"web/app\.[0-9a-f]{12}\.css", stored)


def test_unknown_static_file_is_not_in_the_manifest() -> None:
    with pytest.raises(ValueError, match="Missing staticfiles manifest entry"):
        staticfiles_storage.stored_name("web/missing.css")
