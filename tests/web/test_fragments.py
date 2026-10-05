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

Pages are read through ``pages.py`` and the 06-UI-SPEC hooks only. The location clock is
the views' default ``SystemClock`` unless a test pins it.
"""

from collections.abc import Callable
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
    h1,
    main,
    parse,
    text,
)

from powermon.locations.models import Location

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
