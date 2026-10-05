"""The app shell every signed-in page extends (UI-01, UI-02, UI-03, UI-12; TEST-STRATEGY
§7.5 UI-01 row; 06-UI-SPEC Layout Shell and Test hooks › App shell).

The shell is proven on the probe of ``urls_shell`` (``/_shell/``), a template string that
extends ``layouts/app.html`` behind the real middleware and context processors, before any
page template uses the layout (the brief §13 tracer).

- The probe passes every page invariant with the app hooks; its head has the stylesheet,
  the two font preloads, both favicons, admin.js render-blocking before the deferred
  Alpine build, robots noindex and a zoomable viewport.
- The theme control is a CSRF POST form to /theme/ with three buttons whose aria-pressed
  follows the allowlisted cookie; the account popover holds "Signed in as" and the POST
  sign-out form; body data-now is the sidebar processor's clock.
- ``breadcrumb_trail`` gives each app route its trail and the one item "Locations" for
  anything else, also without a request or a resolver match; the layout renders for a bare
  RequestFactory request (the shape view tests use).
- The drawer and rail controls are JS-only buttons rendered hidden; the sign-in page and
  the error pages have no shell; every location is one sidebar link to its page.
"""

from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.db import connection
from django.template import Context
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from pages import (
    CSP,
    STATIC_ASSET,
    all_by_testid,
    assert_page,
    breadcrumbs,
    by_testid,
    hidden_value,
    main,
    parse,
    post_form,
    text,
)
from urls_shell import PROBE_PATH, PROBE_TEMPLATE, PROBE_TITLE, render_probe

from powermon.web import context_processors
from powermon.web.templatetags import timefmt
from powermon.web.templatetags.crumbs import Crumb, breadcrumb_trail, trail_for

pytestmark = pytest.mark.urls("urls_shell")

User = get_user_model()

USERNAME = "admin"
DISTINCT_NAME = "Shell Probe Zhytomyr"
LONG_NAME = ("Very long location name " * 5)[:100]
THEMES = ("light", "dark", "system")
ONE_ITEM = [("Locations", None)]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> FakeClock:
    """One fixed clock for the sidebar processor and the timefmt tags' fallback."""
    fake = FakeClock(fixed_now)
    monkeypatch.setattr(context_processors, "CLOCK", fake)
    monkeypatch.setattr(timefmt, "CLOCK", fake)
    return fake


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user(USERNAME, password="not-used-here"))
    return client


def _signed_in(path: str) -> Any:
    """A request for ``path`` from the admin, resolved as the URL would be."""
    request = RequestFactory().get(path)
    request.user = User.objects.get_or_create(username=USERNAME)[0]
    request.resolver_match = resolve(path)
    return request


def _location_queries(queries: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in queries.captured_queries if '"location"' in q["sql"]]


# The shell on the probe (UI-01, UI-02)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("cookie", "pressed"),
    [
        (None, "system"),
        ("light", "light"),
        ("dark", "dark"),
        ("system", "system"),
        ("DARK", "system"),
    ],
    ids=["no-cookie", "light", "dark", "system", "hostile"],
)
def test_UI01_shell_on_the_probe(
    admin: Client, clock: FakeClock, fixed_now: datetime, cookie: str | None, pressed: str
) -> None:
    if cookie is not None:
        admin.cookies["theme"] = cookie

    response = admin.get(PROBE_PATH)

    soup = assert_page(response, title=PROBE_TITLE, app=True)
    assert response["Content-Security-Policy"] == CSP
    assert soup.find("html")["data-theme"] == pressed
    assert text(soup.find("h1")) == PROBE_TITLE
    # Skip link first, then the shell: sidebar, top bar and main inside the body column.
    skip = by_testid(soup, "skip-link")
    assert (skip.name, skip["href"], text(skip)) == ("a", "#main", "Skip to content")
    shell = by_testid(soup, "app-shell")
    assert shell["x-data"] == "sidebar"
    sidebar = by_testid(shell, "sidebar")
    assert (sidebar.name, sidebar["id"]) == ("aside", "sidebar")
    assert len(sidebar.find_all("nav", attrs={"aria-label": "Main"})) == 1
    nav = [
        (by_testid(sidebar, hook)["href"], text(by_testid(sidebar, hook)))
        for hook in ("nav-locations", "nav-add-location")
    ]
    assert nav == [("/", "Locations"), ("/locations/new/", "Add location")]
    # The probe is neither the list nor the add page: no main-nav item is current.
    assert sidebar.find_all(attrs={"aria-current": True}) == []
    body_column = shell.select_one("[data-shell-body]")
    topbar = by_testid(body_column, "topbar")
    assert topbar.name == "header"
    page_main = main(body_column)
    assert (page_main["id"], page_main["tabindex"]) == ("main", "-1")
    # The theme switch: a CSRF POST form, aria-pressed from the allowlisted cookie (UI-02).
    form = by_testid(by_testid(topbar, "theme-switch"), "theme-form")
    assert (form["method"], form["action"], form["role"], form["aria-label"]) == (
        "post",
        "/theme/",
        "group",
        "Theme",
    )
    assert (form["x-data"], form["@submit"]) == ("theme", "choose")
    assert hidden_value(form, "csrfmiddlewaretoken")
    buttons = form.find_all("button")
    assert [(b["type"], b["name"], b["value"], b["title"], text(b)) for b in buttons] == [
        ("submit", "theme", "light", "Light", "Light"),
        ("submit", "theme", "dark", "Dark", "Dark"),
        ("submit", "theme", "system", "System", "System"),
    ]
    assert [b["aria-pressed"] for b in buttons] == [
        "true" if value == pressed else "false" for value in THEMES
    ]
    # The account menu: popover trigger, "Signed in as", the POST sign-out form (R2).
    trigger = topbar.find("button", attrs={"popovertarget": "account-menu"})
    assert (trigger["type"], text(trigger)) == ("button", f"Account: {USERNAME}")
    menu = by_testid(topbar, "account-menu")
    assert menu.has_attr("popover")
    assert menu["id"] == "account-menu"
    assert f"Signed in as {USERNAME}" in text(menu)
    sign_out = by_testid(menu, "sign-out-form")
    assert sign_out is post_form(soup, "/logout/")
    assert hidden_value(sign_out, "csrfmiddlewaretoken")
    assert text(sign_out) == "Sign out"
    # Both toast regions, outside the shell; body data-now is the processor's clock.
    for region in ("toasts-status", "toasts-alert"):
        assert by_testid(soup, region).find_parent(attrs={"data-testid": "app-shell"}) is None
    body = soup.find("body")
    assert body["x-data"] == "relative"
    assert datetime.fromisoformat(body["data-now"]) == fixed_now


@pytest.mark.django_db
def test_UI01_shell_head(admin: Client) -> None:
    soup = parse(admin.get(PROBE_PATH))
    head = soup.find("head")

    def rel(link: Any) -> str:
        return " ".join(link.get("rel", []))

    links = head.find_all("link")
    preloads = [link for link in links if rel(link) == "preload"]
    assert [(p["as"], p["type"], p.has_attr("crossorigin")) for p in preloads] == [
        ("font", "font/woff2", True),
        ("font", "font/woff2", True),
    ]
    assert [
        "inter-latin-wght-normal" in preloads[0]["href"],
        "inter-cyrillic" in preloads[1]["href"],
    ] == [True, True]
    stylesheets = [link["href"] for link in links if rel(link) == "stylesheet"]
    assert len(stylesheets) == 1
    assert stylesheets[0].startswith("/static/web/build/app.")
    icons = [
        (link.get("type"), link.get("sizes"), link["href"]) for link in links if rel(link) == "icon"
    ]
    assert [(kind, sizes) for kind, sizes, _ in icons] == [("image/svg+xml", None), (None, "32x32")]
    assert icons[0][2].startswith("/static/web/favicon.") and icons[0][2].endswith(".svg")
    assert icons[1][2].startswith("/static/web/favicon.") and icons[1][2].endswith(".ico")
    for href in [p["href"] for p in preloads] + stylesheets + [href for _, _, href in icons]:
        assert STATIC_ASSET.fullmatch(href), href
    # Exactly two scripts, both in the head: admin.js render-blocking, then Alpine deferred.
    scripts = soup.find_all("script")
    assert [script.find_parent("head") is head for script in scripts] == [True, True]
    assert scripts[0]["src"].startswith("/static/web/admin.")
    assert not scripts[0].has_attr("defer")
    assert scripts[1]["src"].startswith("/static/web/vendor/alpine-csp-3.17.4.min.")
    assert scripts[1].has_attr("defer")
    metas = {meta.get("name"): meta.get("content") for meta in head.find_all("meta")}
    assert metas["robots"] == "noindex, nofollow"
    assert metas["viewport"] == "width=device-width, initial-scale=1"


# Breadcrumbs (UI-01)


@pytest.mark.django_db
def test_UI01_breadcrumb_trails(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Kyiv office")
    detail = f"/locations/{location.pk}/"
    setup = f"{detail}setup/"
    home = ("Locations", "/")
    named = ("Kyiv office", detail)
    cases = {
        "/": ONE_ITEM,
        "/locations/new/": [home, ("Add location", None)],
        detail: [home, ("Kyiv office", None)],
        f"{detail}edit/": [home, named, ("Edit", None)],
        setup: [home, named, ("Device setup", None)],
        f"{detail}delete/": [home, named, ("Delete", None)],
        f"{setup}regenerate/": [home, named, ("Device setup", setup), ("Regenerate key", None)],
        f"{detail}outages/1790000000000000/remove/": [home, named, ("Remove outage", None)],
        f"{detail}reset/": [home, named, ("Reset history", None)],
    }

    for path, expected in cases.items():
        request = RequestFactory().get(path)
        request.resolver_match = resolve(path)
        trail = breadcrumb_trail(Context({"request": request, "location": location}))
        assert trail == expected, path
        assert all(type(crumb) is Crumb for crumb in trail), path
        # The current page is the last item, never a link.
        assert trail[-1].href is None, path

    # Failure path: an unmapped route (the probe) is the one item Locations.
    probe = RequestFactory().get(PROBE_PATH)
    probe.resolver_match = resolve(PROBE_PATH)
    assert breadcrumb_trail(Context({"request": probe, "location": location})) == ONE_ITEM
    for url_name, about in (
        ("no-such-route", location),
        (None, location),
        (42, location),
        ("location-detail", None),
        ("location-edit", object()),
        ("location-setup", SimpleNamespace(pk="1", name="x")),
        ("location-reset", SimpleNamespace(pk=1, name=None)),
    ):
        assert trail_for(url_name, about) == ONE_ITEM, (url_name, about)


@pytest.mark.django_db
def test_UI01_one_item_trail_on_the_probe(admin: Client) -> None:
    soup = parse(admin.get(PROBE_PATH))

    nav = by_testid(soup, "breadcrumbs")
    assert (nav.name, nav["aria-label"]) == ("nav", "Breadcrumb")
    assert nav.find_parent(attrs={"data-testid": "topbar"}) is not None
    assert breadcrumbs(soup) == ONE_ITEM
    # A one-item trail has no compact copy under the h1.
    assert all_by_testid(soup, "breadcrumbs-compact") == []


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["Kyiv office", LONG_NAME], ids=["short", "100-chars"])
def test_UI01_compact_crumbs_repeat_the_top_bar_trail(
    clock: FakeClock, location_factory: Callable[..., Any], name: str
) -> None:
    location = location_factory(name=name)
    detail = f"/locations/{location.pk}/"
    request = _signed_in(f"{detail}edit/")

    soup = parse(render_probe(request, PROBE_TEMPLATE, {"location": location}))

    expected = [("Locations", "/"), (name, detail), ("Edit", None)]
    assert breadcrumbs(soup) == expected
    assert breadcrumbs(soup, "breadcrumbs-compact") == expected
    compact = by_testid(soup, "breadcrumbs-compact")
    assert (compact.name, compact["aria-label"]) == ("nav", "Breadcrumb")
    # Under the h1, inside main; the full name in both trails.
    assert compact.find_parent("main") is not None
    assert compact.find_parent(attrs={"data-testid": "topbar"}) is None


@pytest.mark.django_db
def test_UI01_app_layout_renders_for_a_bare_request(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name=DISTINCT_NAME)
    # No request at all, and a request with no user and no resolver match.
    assert breadcrumb_trail(Context({})) == ONE_ITEM
    assert breadcrumb_trail(Context({"location": location})) == ONE_ITEM
    bare = RequestFactory().get(PROBE_PATH)
    assert not hasattr(bare, "user")
    assert getattr(bare, "resolver_match", None) is None
    assert breadcrumb_trail(Context({"request": bare, "location": location})) == ONE_ITEM

    with CaptureQueriesContext(connection) as queries:
        html = render_probe(bare, PROBE_TEMPLATE, {"location": location})

    soup = parse(html)
    by_testid(soup, "topbar")
    by_testid(soup, "sidebar")
    assert breadcrumbs(soup) == ONE_ITEM
    assert all_by_testid(soup, "breadcrumbs-compact") == []
    assert all_by_testid(soup, "sidebar-location") == []
    # The sidebar processor returns nothing without a user: no location query, no name.
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in html
    assert soup.find("body")["data-now"] == ""


# Drawer and rail (UI-01, UI-12)


@pytest.mark.django_db
def test_UI01_drawer_and_rail_hooks(admin: Client) -> None:
    soup = parse(admin.get(PROBE_PATH))
    sidebar = by_testid(soup, "sidebar")
    topbar = by_testid(soup, "topbar")

    for hook, region, label in (
        ("sidebar-toggle", topbar, "Open navigation"),
        ("drawer-close", sidebar, "Close navigation"),
        ("rail-toggle", topbar, "Collapse sidebar"),
    ):
        button = by_testid(region, hook)
        assert (button.name, button["type"], button["aria-controls"]) == (
            "button",
            "button",
            "sidebar",
        ), hook
        assert button["aria-expanded"] in ("true", "false"), hook
        # JS only: rendered hidden, revealed by the sidebar component.
        assert button.has_attr("data-js-only") and button.has_attr("hidden"), hook
        assert text(button) == label, hook
    assert by_testid(topbar, "rail-toggle")["title"] == "Collapse sidebar"
    # The overlay and the body column the sidebar component makes inert.
    shell = by_testid(soup, "app-shell")
    assert len(shell.select("[data-drawer-overlay]")) == 1
    assert len(shell.select("[data-shell-body]")) == 1


# Bare pages (UI-01, R11)


@pytest.mark.django_db
def test_UI01_bare_pages_have_no_shell(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location_factory(name=DISTINCT_NAME)

    with CaptureQueriesContext(connection) as queries:
        missing = client.get("/no-such-page-xyz/")
    sign_in = client.get("/login/")
    signed = Client()
    signed.force_login(User.objects.create_user(USERNAME, password="not-used-here"))
    signed_missing = signed.get("/no-such-page-xyz/")

    assert_page(missing, status=404, app=False)
    assert_page(signed_missing, status=404, app=False)
    assert sign_in.status_code == 200
    for page in (missing, sign_in, signed_missing):
        soup = parse(page)
        for hook in (
            "app-shell",
            "sidebar",
            "sidebar-locations",
            "topbar",
            "theme-switch",
            "account-menu",
        ):
            assert all_by_testid(soup, hook) == [], hook
        assert DISTINCT_NAME not in page.content.decode()
    # An anonymous 404 runs no location query (R11).
    assert _location_queries(queries) == []


# One click to every location (UI-03)


@pytest.mark.django_db
def test_UI03_sidebar_links_open_in_one_click(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    kyiv = location_factory(name="Kyiv office")
    lviv = location_factory(name="Lviv home")
    location_factory(name=DISTINCT_NAME, deleted_at=fixed_now)

    soup = parse(admin.get(PROBE_PATH))

    listing = by_testid(soup, "sidebar-locations")
    assert (listing.name, listing["aria-label"]) == ("nav", "Locations")
    links = all_by_testid(soup, "sidebar-location")
    assert [
        (link.name, link["href"], link["data-location-id"], link["title"]) for link in links
    ] == [
        ("a", f"/locations/{kyiv.pk}/", str(kyiv.pk), "Kyiv office"),
        ("a", f"/locations/{lviv.pk}/", str(lviv.pk), "Lviv home"),
    ]
    assert [
        link.find_parent(attrs={"data-testid": "sidebar-locations"}) is listing for link in links
    ] == [True, True]
    assert [text(link).startswith(link["title"]) for link in links] == [True, True]
    # The deleted location is not listed; the probe is no location's page.
    assert DISTINCT_NAME not in admin.get(PROBE_PATH).content.decode()
    assert [link for link in links if link.has_attr("aria-current")] == []
