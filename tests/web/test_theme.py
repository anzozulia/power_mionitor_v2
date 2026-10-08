"""The theme cookie: its context processor and the no-JS POST fallback (UI-02, D6-03, R8;
TEST-STRATEGY §8.4).

- ``context_processors.theme``: the ``theme`` cookie's value when it is exactly ``light``,
  ``dark`` or ``system``, else ``system`` (no cookie, other case, trailing characters, an
  empty value, a 4 KB string, a script payload). It runs no query, and the raw cookie never
  reaches a template (R1). No GET response sets the cookie.
- ``POST /theme/`` (``ThemeView``): ``theme=light|dark|system`` answers 302 to the fixed
  ``/`` and sets ``theme=<value>`` with Path=/, SameSite=Lax, Max-Age one year, Secure
  exactly when ``SESSION_COOKIE_SECURE`` is true, and not HttpOnly (the JS toggle writes
  it too). Any other or missing value answers 400 with an empty body and sets no cookie.
  GET redirects to / and sets nothing (F-24); a POST without a CSRF token is 403 and an
  anonymous POST goes to sign-in, neither with a cookie. ``next`` and the Referer never
  change the target (no open redirect, R8). The POST writes nothing, adds no flash and is
  never cached.
- Rendered (S1, the first page on the auth layout): ``<html data-theme>`` is the
  processor's value for every cookie above, the page keeps the page invariants in each
  state, and a hostile value never reaches the HTML. No GET sets the cookie (D6-03).
"""

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from pages import assert_no_injected_script, assert_page

from powermon.web import context_processors
from powermon.web.theme import THEME_MAX_AGE

User = get_user_model()

URL = "/theme/"
VALUES = ("light", "dark", "system")
SCRIPT_PAYLOAD = '"><script>alert(1)</script>'
LONG_VALUE = "x" * 4096
# Every value the processor and the POST must refuse.
HOSTILE = ("DARK", "dark;", "light ", "", LONG_VALUE, SCRIPT_PAYLOAD)
HOSTILE_IDS = ("upper", "semicolon", "trailing-space", "empty", "4kb", "script")
WRITES = ("INSERT", "UPDATE", "DELETE")


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _with_cookie(value: str | None) -> Any:
    """A bare request carrying ``theme=<value>`` (or no theme cookie)."""
    request = RequestFactory().get("/")
    if value is not None:
        request.COOKIES[context_processors.THEME_COOKIE] = value
    return request


def _cache_control(response: Any) -> set[str]:
    return {part.strip() for part in response["Cache-Control"].split(",")}


# The context processor (UI-02 boundary, R1)


@pytest.mark.django_db
def test_UI02_theme_processor_allowlist(django_assert_num_queries: Any) -> None:
    assert context_processors.THEMES == frozenset(VALUES)
    with django_assert_num_queries(0):
        # Expected: each allowlisted value renders as itself.
        for value in VALUES:
            assert context_processors.theme(_with_cookie(value)) == {"theme": value}
        # Edge: no cookie at all is the default.
        assert context_processors.theme(_with_cookie(None)) == {"theme": "system"}
        # Failure: anything else, however close, is the default; never the raw value.
        for value in HOSTILE:
            assert context_processors.theme(_with_cookie(value)) == {"theme": "system"}


@pytest.mark.django_db
def test_UI02_theme_reaches_every_page_from_the_cookie(admin: Client) -> None:
    # Registered in settings: every page's context carries the allowlisted value.
    assert admin.get("/").context["theme"] == "system"

    admin.cookies["theme"] = "dark"
    page = admin.get("/")

    assert page.context["theme"] == "dark"
    # Reading the theme never sets it: only the theme POST does.
    assert "theme" not in page.cookies

    admin.cookies["theme"] = '"><script>alert(1)</script>'
    page = admin.get("/")

    assert page.context["theme"] == "system"
    assert "alert(1)" not in page.content.decode()


@pytest.mark.django_db
def test_UI02_theme_reaches_the_sign_in_page(client: Client) -> None:
    client.cookies["theme"] = "light"

    page = client.get("/login/")

    assert page.status_code == 200
    assert page.context["theme"] == "light"
    assert "theme" not in page.cookies


# Rendered from the cookie on the auth layout (UI-02, D6-03, R1)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        # Edge: no cookie at all is the default.
        (None, "system"),
        # Expected: each allowlisted value renders as itself.
        *((value, value) for value in VALUES),
        # Failure: anything else, however close, renders the default.
        *((value, "system") for value in HOSTILE),
    ],
    ids=["no-cookie", *VALUES, *HOSTILE_IDS],
)
def test_UI02_sign_in_renders_theme_from_cookie(
    client: Client, cookie: str | None, expected: str
) -> None:
    if cookie is not None:
        client.cookies["theme"] = cookie

    response = client.get("/login/")

    # The page keeps the invariants in every theme state, on the auth layout.
    page = assert_page(response, title="Sign in", app=False)
    root = page.find("html")
    assert root is not None and root.get("data-theme") == expected
    # Reading the theme never sets it: only the theme POST does.
    assert "theme" not in response.cookies
    html = response.content.decode()
    if cookie in (SCRIPT_PAYLOAD, LONG_VALUE):
        # The raw cookie never reaches the page, so the payload never runs (R1).
        assert cookie not in html
        assert_no_injected_script(html, "sign-in")


# The no-JS POST fallback (D6-03, R8)


@pytest.mark.django_db
@pytest.mark.parametrize("value", VALUES)
def test_UI02_theme_post_sets_cookie(admin: Client, value: str) -> None:
    response = admin.post(URL, {"theme": value})

    assert response.status_code == 302
    assert response.url == "/"
    cookie = response.cookies["theme"]
    assert cookie.value == value
    assert cookie["path"] == "/"
    assert cookie["samesite"] == "Lax"
    assert int(cookie["max-age"]) == THEME_MAX_AGE == 31_536_000
    # Not HttpOnly: the JS toggle updates the same cookie. Not Secure outside production.
    assert not cookie["httponly"]
    assert not cookie["secure"]


@pytest.mark.django_db
def test_UI02_theme_post_cookie_is_secure_in_production(
    production_settings: Any, admin: Client
) -> None:
    response = admin.post(URL, {"theme": "dark"}, secure=True)

    assert response.status_code == 302
    assert response.url == "/"
    cookie = response.cookies["theme"]
    assert cookie["secure"] is True
    assert cookie["samesite"] == "Lax"
    assert not cookie["httponly"]


@pytest.mark.django_db
def test_UI02_theme_post_rejects_bad_values(admin: Client) -> None:
    for data in ({}, *({"theme": value} for value in HOSTILE)):
        response = admin.post(URL, data)
        assert response.status_code == 400, data
        assert response.content == b""
        assert "theme" not in response.cookies


@pytest.mark.django_db
def test_UI02_theme_get_redirects_and_sets_nothing(admin: Client) -> None:
    # Expected (F-24): the sign-in redirect after an ended session GETs /theme/; edge: a
    # query value is ignored. No cookie, no flash, never cached, only the fixed target.
    for response in (admin.get(URL), admin.get(URL, {"theme": "dark"})):
        assert response.status_code == 302
        assert response["Location"] == "/"
        assert "theme" not in response.cookies
        assert {"no-store", "private"} <= _cache_control(response)
    assert list(admin.get("/").context["messages"]) == []

    # Failure: every other method still answers 405, with no cookie.
    for method in (admin.put, admin.delete):
        response = method(URL)
        assert response.status_code == 405
        assert "theme" not in response.cookies


@pytest.mark.django_db
def test_UI02_theme_post_without_a_csrf_token_is_refused() -> None:
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(User.objects.create_user("admin", password="not-used-here"))

    response = browser.post(URL, {"theme": "dark"})

    assert response.status_code == 403
    assert "theme" not in response.cookies


@pytest.mark.django_db
def test_UI02_theme_post_anonymous_redirects(client: Client) -> None:
    response = client.post(URL, {"theme": "dark"})

    assert response.status_code == 302
    assert response.url == f"/login/?next={URL}"
    assert "theme" not in response.cookies


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("data", "headers"),
    [
        ({"next": "https://evil.example/"}, {}),
        ({"next": "//evil.example/"}, {}),
        ({"next": "/\\evil.example"}, {}),
        ({}, {"referer": "https://evil.example/locations/1/"}),
        ({}, {"referer": "javascript:alert(1)"}),
        ({}, {"referer": "http://testserver/locations/1/"}),
        ({}, {}),
    ],
    ids=[
        "next-https",
        "next-scheme-relative",
        "next-backslash",
        "referer-evil",
        "referer-js",
        "referer-same-host",
        "nothing",
    ],
)
def test_UI02_theme_post_no_open_redirect(
    admin: Client, data: dict[str, str], headers: dict[str, str]
) -> None:
    response = admin.post(URL, {"theme": "dark", **data}, headers=headers)

    assert response.status_code == 302
    assert response["Location"] == "/"


@pytest.mark.django_db
def test_UI02_theme_post_ignores_next_in_the_query_string(admin: Client) -> None:
    response = admin.post(f"{URL}?next=https://evil.example/", {"theme": "light"})

    assert response.status_code == 302
    assert response["Location"] == "/"


@pytest.mark.django_db
def test_UI02_theme_post_writes_nothing(admin: Client) -> None:
    with CaptureQueriesContext(connection) as queries:
        response = admin.post(URL, {"theme": "dark"})

    assert response.status_code == 302
    statements = [query["sql"].lstrip().upper() for query in queries.captured_queries]
    assert [sql for sql in statements if sql.startswith(WRITES)] == []
    # No flash: the next page has no message.
    assert list(admin.get("/").context["messages"]) == []


@pytest.mark.django_db
def test_UI02_theme_post_never_cached(admin: Client) -> None:
    for data, status in (({"theme": "dark"}, 302), ({"theme": "DARK"}, 400)):
        response = admin.post(URL, data)
        assert response.status_code == status
        cache_control = _cache_control(response)
        assert {"no-store", "private"} <= cache_control
        assert "public" not in cache_control
        assert not any(part.startswith("s-maxage") for part in cache_control)
