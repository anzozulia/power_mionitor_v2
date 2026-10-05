"""Production security of the admin surface (SEC-02, INV-22 #3, D-17, R5) and its error pages.

Over HTTPS, directly or behind Caddy's X-Forwarded-Proto, the admin pages set Secure
session and CSRF cookies and HSTS for one year (no includeSubDomains, no preload). Every
response except the exact path /hb carries the brief §8 CSP (same-origin scripts, styles,
fonts, images and fetches only; never unsafe-inline, unsafe-eval, a wildcard, an http(s)
source, strict-dynamic or a nonce) and Django's header defaults. Pages load only
manifest-hashed same-origin /static/ assets, and no attribute value names another origin
except the SVG namespace on an inline svg (R5).

E1 404, E2 403-CSRF and E3 500 extend one error layout that reads no context variable: each
body equals the template rendered with no context and no request, with the hard-coded
data-theme="system", fixed copy, no script and nothing from the request (R11, UI-01, UI-12).

The ``production_settings`` fixture mirrors what APP_ENV=production builds; the subprocess
tests below prove that against a real production process.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.views.defaults import server_error
from urls_raise import EXCEPTION_MESSAGE, RAISE_PATH

from powermon.web.admin_sync import sync_admin
from powermon.web.location_views import ALERTS_COPY

User = get_user_model()

# 06-UI-SPEC security-bound rule R5 (brief §8), written out so a changed constant fails here.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
# Sources that would widen the policy; none may ever appear in it (TEST-STRATEGY §7.3).
LOOSE_SOURCES = ("'unsafe-inline'", "'unsafe-eval'", "*", "http:", "https:", "'strict-dynamic'")
# The one absolute URL an attribute may hold: the namespace of an inline svg element.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
# A manifest-hashed static path, e.g. /static/web/build/app.0123456789ab.css.
HASHED_STATIC = re.compile(r"/static/(?:[\w-]+/)*[\w.-]+\.[0-9a-f]{12}\.\w+")
# Fixed copy rows e404, e403, e500 and error.back (06-UI-SPEC copy table).
E404_BODY = "This page does not exist. Check the address, or go back to the location list."
E403_BODY = (
    "This form was open too long, or the browser blocked cookies. "
    "Go back, reload the page and try again."
)
E500_BODY = (
    "The server could not finish this request. Details are in the web container logs. "
    "Try again in a moment."
)
BACK = "Back to locations"
# A location name that must never reach an error page (R11).
DISTINCT_NAME = "Error Page Probe Uzhhorod"
HSTS = "max-age=31536000"
TEMPLATES_DIR = Path(settings.BASE_DIR) / "powermon" / "web" / "templates"
WEB_PACKAGE = Path(settings.BASE_DIR) / "powermon" / "web"
# R1: the one web module that may call mark_safe, relative to WEB_PACKAGE. 06-06 adds the
# {% icon %} tag there with the one audited mark_safe; 06-07 replaces the test below with
# tests/web/test_frontend_lint.py, which confines mark_safe to the same file.
MARK_SAFE_MODULE = "templatetags/icons.py"

# A complete production env with no example values (see tests/test_config.py).
PRODUCTION_ENV: dict[str, str] = {
    "APP_ENV": "production",
    "DEBUG": "0",
    "SECRET_KEY": ("prod-test-secret-key-not-the-example-" * 2)[:64],
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "a-real-admin-password",
    "POSTGRES_DB": "powermon",
    "POSTGRES_USER": "powermon",
    "POSTGRES_PASSWORD": "a-real-db-password",
    "POSTGRES_HOST": "db",
    "POSTGRES_PORT": "5432",
    "DOMAIN": "power.example.org",
    "ACME_EMAIL": "ops@example.org",
    "DISPLAY_TZ": "Europe/Kyiv",
}
# The settings that make INV-22 #3 hold, with their production values.
PRODUCTION_SECURITY = {
    "DEBUG": False,
    "SESSION_COOKIE_SECURE": True,
    "CSRF_COOKIE_SECURE": True,
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SECURE_HSTS_SECONDS": 31_536_000,
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": False,
    "SECURE_HSTS_PRELOAD": False,
    "SECURE_PROXY_SSL_HEADER": ["HTTP_X_FORWARDED_PROTO", "https"],
    "SECURE_SSL_REDIRECT": False,
    "SECURE_CONTENT_TYPE_NOSNIFF": True,
    "SECURE_REFERRER_POLICY": "same-origin",
    "X_FRAME_OPTIONS": "DENY",
}


def _run_in_production(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python *args`` in the project dir with the production env and no other."""
    env = {
        **PRODUCTION_ENV,
        "DJANGO_SETTINGS_MODULE": "powermon.settings",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return subprocess.run(
        [sys.executable, *args],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _csrf_failure(path: str) -> Any:
    """POST ``path`` with no CSRF token from a client that enforces CSRF."""
    return Client(enforce_csrf_checks=True).post(path, {"username": "admin", "password": "x"})


def _server_error() -> Any:
    """The response Django's 500 handler builds (it renders 500.html with no context)."""
    return server_error(RequestFactory().get("/"))


def _page(client: Client, page: str) -> Any:
    """One response of each page kind the admin surface can show a signed-out browser."""
    if page == "login":
        return client.get("/login/")
    if page == "404":
        return client.get("/no-such-page-xyz")
    if page == "csrf-403":
        return _csrf_failure("/login/")
    assert page == "500", page
    return _server_error()


def _sign_in(client: Client) -> Client:
    """``client``, signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _location_queries(queries: CaptureQueriesContext) -> list[str]:
    """The captured statements that read the location or the location state table."""
    tables = ('"location"', '"location_state"')
    return [q["sql"] for q in queries.captured_queries if any(t in q["sql"] for t in tables)]


# Parsing: the stdlib parser (tests/web/pages.py arrives in the same wave and is not used here)

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)


@dataclass
class _Element:
    """One element of a parsed page: its tag, its attributes in source order, its text."""

    tag: str
    attrs: list[tuple[str, str]]
    chunks: list[str] = field(default_factory=list)

    def attr(self, name: str) -> str | None:
        return next((value for key, value in self.attrs if key == name), None)

    @property
    def text(self) -> str:
        """The element's text, tags dropped, entities decoded, whitespace collapsed."""
        return " ".join("".join(self.chunks).split())


class _Page(HTMLParser):
    """Every element of an HTML page with its attributes and its text, in document order."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._open: list[_Element] = []
        self._chunks: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, [(name, value or "") for name, value in attrs])
        self.elements.append(element)
        if tag not in _VOID:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(_Element(tag, [(name, value or "") for name, value in attrs]))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)
        for element in self._open:
            element.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self._chunks).split())

    def find(self, tag: str, attrs: dict[str, str] | None = None) -> list[_Element]:
        """The ``tag`` elements whose attributes equal every item of ``attrs``."""
        wanted = attrs or {}
        return [
            element
            for element in self.elements
            if element.tag == tag and all(element.attr(k) == v for k, v in wanted.items())
        ]

    def one(self, tag: str, attrs: dict[str, str] | None = None) -> _Element:
        found = self.find(tag, attrs)
        assert len(found) == 1, (tag, attrs, len(found))
        return found[0]

    def by_testid(self, testid: str) -> list[_Element]:
        return [element for element in self.elements if element.attr("data-testid") == testid]


def _absolute_url_attributes(html: str) -> list[tuple[str, str, str]]:
    """(tag, attribute, value) for every attribute value that points at another origin (R5).

    A value counts when, stripped and lowercased, it starts with ``http:``, ``https:`` or
    ``//``. The one exception is bound to a tag, an attribute and a value at once: ``xmlns``
    on an ``svg`` element, exactly ``http://www.w3.org/2000/svg`` (TEST-STRATEGY §3.5, §5.2
    #9), which every inline icon carries.
    """
    found = []
    for element in _Page(html).elements:
        for name, value in element.attrs:
            if not value.strip().lower().startswith(("http:", "https:", "//")):
                continue
            if (element.tag, name, value) == ("svg", "xmlns", SVG_NAMESPACE):
                continue
            found.append((element.tag, name, value))
    return found


def _loose_sources(policy: str) -> list[str]:
    """The sources in ``policy`` that would widen it; a nonce counts as ``'nonce-``."""
    found = [source for source in LOOSE_SOURCES if source in policy]
    if "'nonce-" in policy:
        found.append("'nonce-")
    return found


def _assert_csp(response: Any) -> None:
    """The response carries exactly the R5 policy, and the policy has no loose source."""
    policy = response["Content-Security-Policy"]
    assert policy == CSP
    assert _loose_sources(policy) == []


def _assert_error_page(html: str, *, code: str, title: str, heading: str, body: str) -> None:
    """The E1-E3 contract: hooks, fixed copy, no script, nothing of the app shell (R11)."""
    page = _Page(html)
    root = page.one("html")
    assert (root.attr("lang"), root.attr("data-theme")) == ("en", "system")
    assert [element.text for element in page.find("title")] == [f"{title} · Power Monitor"]
    assert [element.text for element in page.find("h1")] == [heading]
    cards = page.by_testid("error-page")
    assert [card.attr("data-code") for card in cards] == [code]
    assert code in cards[0].text.split()
    assert heading in cards[0].text
    assert body in cards[0].text
    back = page.by_testid("back-to-locations")
    assert [(a.tag, a.attr("href"), a.text) for a in back] == [("a", "/", BACK)]
    skip = page.by_testid("skip-link")
    assert [(a.tag, a.attr("href"), a.text) for a in skip] == [("a", "#main", "Skip to content")]
    main = page.one("main")
    assert (main.attr("id"), main.attr("tabindex")) == ("main", "-1")
    robots = page.find("meta", {"name": "robots"})
    assert [meta.attr("content") for meta in robots] == ["noindex, nofollow"]
    viewport = page.find("meta", {"name": "viewport"})
    assert [meta.attr("content") for meta in viewport] == ["width=device-width, initial-scale=1"]
    assert page.find("script") == []
    for absent in ("sidebar", "topbar", "toasts-status", "toasts-alert", "toast", "theme-switch"):
        assert page.by_testid(absent) == [], absent
    # Django's own CSRF page explains the failure reason; ours never names it.
    assert "csrf" not in html.lower()


# Secure cookies and HSTS (INV-22 #3)


@pytest.mark.django_db
def test_INV22_https_sign_in_sets_secure_cookies_and_hsts(
    production_settings: Any, client: Client
) -> None:
    sync_admin("admin", "pw-one")

    page = client.get("/login/", secure=True)

    assert page.status_code == 200
    assert page["Strict-Transport-Security"] == HSTS
    assert page.cookies[production_settings.CSRF_COOKIE_NAME]["secure"] is True

    response = client.post("/login/", {"username": "admin", "password": "pw-one"}, secure=True)

    assert response.status_code == 302
    assert response["Strict-Transport-Security"] == HSTS
    session = response.cookies[production_settings.SESSION_COOKIE_NAME]
    assert session["secure"] is True
    assert session["httponly"] is True
    assert session["samesite"] == "Lax"
    # Sign-in rotates the CSRF token; the new cookie is Secure too.
    assert response.cookies[production_settings.CSRF_COOKIE_NAME]["secure"] is True


@pytest.mark.django_db
def test_forwarded_proto_https_counts_as_secure(production_settings: Any, client: Client) -> None:
    # Caddy terminates TLS and sets X-Forwarded-Proto; the app sees plain HTTP.
    response = client.get("/login/", headers={"x-forwarded-proto": "https"})

    assert response.status_code == 200
    assert response["Strict-Transport-Security"] == HSTS
    assert response.cookies[production_settings.CSRF_COOKIE_NAME]["secure"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("headers", [{}, {"x-forwarded-proto": "http"}], ids=["plain", "fwd-http"])
def test_no_hsts_over_plain_http(
    production_settings: Any, client: Client, headers: dict[str, str]
) -> None:
    response = client.get("/login/", headers=headers)

    assert response.status_code == 200
    assert "Strict-Transport-Security" not in response


def test_INV22_production_env_builds_these_security_settings(production_settings: Any) -> None:
    script = (
        "import django, json; django.setup(); from django.conf import settings as s; "
        f"print(json.dumps({{k: getattr(s, k) for k in {sorted(PRODUCTION_SECURITY)!r}}}))"
    )

    result = _run_in_production("-c", script)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == PRODUCTION_SECURITY
    # The fixture the in-process tests use sets the same values.
    for name in ("DEBUG", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_HSTS_SECONDS"):
        assert getattr(production_settings, name) == PRODUCTION_SECURITY[name], name


# Header set and check --deploy


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["login", "404", "csrf-403"])
def test_admin_pages_security_headers(client: Client, page: str) -> None:
    response = _page(client, page)

    _assert_csp(response)
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "same-origin"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind",
    [
        "signed-in-page",
        "anonymous-redirect",
        "status-json",
        "chart-png",
        "theme-redirect",
        "static-css",
        "static-js",
    ],
)
def test_R5_csp_on_every_response_kind(
    client: Client, location_factory: Callable[..., Any], kind: str
) -> None:
    # The middleware is outermost, so JSON, images, redirects and the static files WhiteNoise
    # answers get the same header as HTML (TEST-STRATEGY §7.3).
    if kind == "anonymous-redirect":
        response = client.get("/")
        assert response.status_code == 302
    elif kind.startswith("static-"):
        name = {"static-css": "web/build/app.css", "static-js": "web/admin.js"}[kind]
        response = client.get("/static/" + staticfiles_storage.stored_name(name))
        assert response.status_code == 200
    else:
        admin = _sign_in(client)
        location = location_factory(name="Office")
        if kind == "theme-redirect":
            response = admin.post("/theme/", {"theme": "dark"})
            assert response.status_code == 302
        else:
            path = {
                "signed-in-page": "/",
                "status-json": "/locations/status.json",
                "chart-png": f"/locations/{location.pk}/chart.png",
            }[kind]
            response = admin.get(path)
            assert response.status_code == 200

    _assert_csp(response)


@pytest.mark.parametrize(
    ("source", "expected"),
    [(source, [source]) for source in LOOSE_SOURCES] + [("'nonce-r4nd0m'", ["'nonce-"])],
)
def test_R5_loose_source_check(source: str, expected: list[str]) -> None:
    # Expected: the policy itself has none. Failure: a script-src widened by any one loose
    # source is reported as exactly that source, so the negative check is not vacuous.
    widened = CSP.replace("script-src 'self'", f"script-src 'self' {source}")

    assert _loose_sources(CSP) == []
    assert _loose_sources(widened) == expected


def test_check_deploy_clean_with_production_settings(production_settings: Any) -> None:
    call_command("check", deploy=True, fail_level="WARNING")


def test_check_deploy_fails_with_debug_on(production_settings: Any) -> None:
    # The same check is not vacuous: DEBUG on in production is reported (security.W018).
    production_settings.DEBUG = True

    with pytest.raises(SystemCheckError, match="W018"):
        call_command("check", deploy=True, fail_level="WARNING")


def test_check_deploy_clean_in_a_real_production_process() -> None:
    result = _run_in_production("manage.py", "check", "--deploy", "--fail-level", "WARNING")

    assert result.returncode == 0, result.stdout + result.stderr


# Error pages (UI-SPEC error pages, E8)


@pytest.mark.django_db
def test_404_page_does_not_echo_the_path(client: Client) -> None:
    response = client.get("/no-such-page-xyz", {"q": "<b>echo-me</b>"})

    assert response.status_code == 404
    html = response.content.decode()
    assert "<title>Page not found · Power Monitor</title>" in html
    page = _Page(html)
    assert [h1.text for h1 in page.find("h1")] == ["Page not found"]
    assert E404_BODY in page.text
    back = page.one("a", {"data-testid": "back-to-locations"})
    assert (back.attr("href"), back.text) == ("/", BACK)
    assert "no-such-page-xyz" not in html
    assert "echo-me" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("signed_in", [False, True], ids=["anonymous", "signed-in"])
def test_E1_404_page_is_context_free(
    client: Client, location_factory: Callable[..., Any], signed_in: bool
) -> None:
    # Django renders 404.html with the request, so every context processor runs; the page
    # reads none of them (R11): no location query, no name, and the theme cookie is ignored.
    location_factory(name=DISTINCT_NAME)
    if signed_in:
        _sign_in(client)
    client.cookies["theme"] = "dark"

    with CaptureQueriesContext(connection) as queries:
        response = client.get("/no-such-page-xyz")

    assert response.status_code == 404
    html = response.content.decode()
    # Byte-equal to the template rendered with no context and no request: it reads nothing.
    assert html == render_to_string("404.html")
    _assert_error_page(
        html, code="404", title="Page not found", heading="Page not found", body=E404_BODY
    )
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in html


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["404", "csrf-403"])
def test_R11_error_pages_keep_a_pending_flash(
    client: Client, location_factory: Callable[..., Any], page: str
) -> None:
    location = location_factory(name="Office")
    admin = _sign_in(client)
    # Queue a flash: the alerts switch redirects with it, and nothing reads it yet.
    assert admin.post(f"/locations/{location.pk}/alerts/", {"value": "off"}).status_code == 302

    if page == "404":
        assert admin.get("/no-such-page-xyz").status_code == 404
    else:
        # The same browser (session and pending flash), sending a form without its token.
        browser = Client(enforce_csrf_checks=True)
        browser.cookies = admin.cookies
        response = browser.post(f"/locations/{location.pk}/alerts/", {"value": "on"})
        assert response.status_code == 403

    # The error page showed no toast, so the next page still has the flash.
    after = admin.get(f"/locations/{location.pk}/")
    assert [str(message) for message in after.context["messages"]] == [ALERTS_COPY["off"]]


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/login/", "/logout/"])
def test_csrf_failure_renders_form_expired(path: str) -> None:
    # A form open too long (or a stale tab's Sign out) arrives without a valid token.
    response = _csrf_failure(path)

    assert response.status_code == 403
    html = response.content.decode()
    assert "<title>Form expired · Power Monitor</title>" in html
    page = _Page(html)
    assert [h1.text for h1 in page.find("h1")] == ["Form expired"]
    assert E403_BODY in page.text
    back = page.one("a", {"data-testid": "back-to-locations"})
    assert (back.attr("href"), back.text) == ("/", BACK)
    # Django's own CSRF page explains the failure reason; ours shows fixed copy only.
    assert "CSRF" not in html


@pytest.mark.django_db
def test_csrf_failure_with_a_stale_token_renders_form_expired() -> None:
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.CSRF_COOKIE_NAME] = "a" * 32

    response = client.post("/logout/", {"csrfmiddlewaretoken": "b" * 32})

    assert response.status_code == 403
    assert [h1.text for h1 in _Page(response.content.decode()).find("h1")] == ["Form expired"]


@pytest.mark.django_db
@pytest.mark.parametrize("signed_in", [False, True], ids=["anonymous", "signed-in"])
def test_E2_csrf_page(
    client: Client, location_factory: Callable[..., Any], signed_in: bool
) -> None:
    # CsrfViewMiddleware runs before LoginRequiredMiddleware, so even an anonymous POST to an
    # admin URL renders 403_csrf.html with the request and every context processor (R11).
    location_factory(name=DISTINCT_NAME)
    browser = Client(enforce_csrf_checks=True)
    if signed_in:
        _sign_in(browser)
    browser.cookies["theme"] = "dark"

    with CaptureQueriesContext(connection) as queries:
        response = browser.post("/locations/new/", {"name": DISTINCT_NAME})

    assert response.status_code == 403
    html = response.content.decode()
    # Byte-equal to the template rendered with no context and no request: it reads nothing.
    assert html == render_to_string("403_csrf.html")
    _assert_error_page(
        html, code="403", title="Form expired", heading="Form expired", body=E403_BODY
    )
    # Neither Django's failure page nor its reason (no cookie, no token, bad Referer or
    # Origin) is shown, and nothing the request carried comes back.
    for echo in ("Forbidden", "verification failed", "cookie not set", "Referer", "Origin"):
        assert echo not in html, echo
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in html


@pytest.mark.django_db
@pytest.mark.urls("urls_raise")
def test_E3_500_through_the_middleware() -> None:
    # A view that raises, behind the whole middleware stack: Django's handler renders
    # 500.html with no context, and the CSP still reaches the response (R5, R11).
    browser = _sign_in(Client(raise_request_exception=False))
    browser.cookies["theme"] = "dark"

    response = browser.get(RAISE_PATH)

    assert response.status_code == 500
    _assert_csp(response)
    html = response.content.decode()
    _assert_error_page(
        html, code="500", title="Server error", heading="Something went wrong", body=E500_BODY
    )
    for part in (EXCEPTION_MESSAGE, "echo-me", "/secret/path", "RuntimeError", "Traceback"):
        assert part not in html, part


@pytest.mark.django_db
@pytest.mark.urls("urls_raise")
def test_E3_500_renders_without_context() -> None:
    # Signed in, so default-deny lets the request reach the raising view.
    browser = _sign_in(Client(raise_request_exception=False))

    served = browser.get(RAISE_PATH).content.decode()

    # The served page equals the template rendered with no context and no request, and so
    # does the page the handler builds for a bare request outside the middleware.
    assert served == render_to_string("500.html")
    assert _server_error().content.decode() == served


def test_500_page_renders_without_request_context() -> None:
    response = _server_error()

    assert response.status_code == 500
    html = response.content.decode()
    assert "<title>Server error · Power Monitor</title>" in html
    page = _Page(html)
    assert [h1.text for h1 in page.find("h1")] == ["Something went wrong"]
    assert E500_BODY in page.text
    _assert_error_page(
        html, code="500", title="Server error", heading="Something went wrong", body=E500_BODY
    )


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["login", "404", "csrf-403", "500"])
def test_pages_load_only_same_origin_assets(client: Client, page: str) -> None:
    html = _page(client, page).content.decode()
    parsed = _Page(html)

    # Every script and every link (stylesheet, preload, icon) is a manifest-hashed
    # same-origin /static/ file; a script without src (an inline body) fails here (R5).
    assets = [(s.tag, s.attr("src")) for s in parsed.find("script")]
    assets += [(link.tag, link.attr("href")) for link in parsed.find("link")]
    assert len(parsed.find("link", {"rel": "stylesheet"})) == 1
    for tag, url in assets:
        assert url is not None and HASHED_STATIC.fullmatch(url), (tag, url)
        assert staticfiles_storage.exists(url.removeprefix("/static/")), url
    assert _absolute_url_attributes(html) == []
    robots = parsed.find("meta", {"name": "robots"})
    assert [meta.attr("content") for meta in robots] == ["noindex, nofollow"]


ICON_PAGE = (
    '<link rel="stylesheet" href="/static/web/build/app.0123456789ab.css">'
    '<p><svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false" '
    'viewBox="0 0 24 24"><path d="M4 14h6"/></svg>Page not found</p>'
)


@pytest.mark.parametrize(
    ("sample", "findings"),
    [
        # Expected: an icon page, as 06-08 and 06-10 build them, passes.
        ("", []),
        # Failure: the exception is that tag, that attribute and that value, nothing wider.
        ('<a href="https://example.com/">x</a>', [("a", "href", "https://example.com/")]),
        ('<img src="//cdn.example/x.png" alt="">', [("img", "src", "//cdn.example/x.png")]),
        (
            '<link rel="stylesheet" href="http://cdn.example/x.css">',
            [("link", "href", "http://cdn.example/x.css")],
        ),
        (
            '<svg xmlns="https://www.w3.org/2000/svg"></svg>',
            [("svg", "xmlns", "https://www.w3.org/2000/svg")],
        ),
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink"></svg>',
            [("svg", "xmlns:xlink", "http://www.w3.org/1999/xlink")],
        ),
        (
            '<div xmlns="http://www.w3.org/2000/svg"></div>',
            [("div", "xmlns", "http://www.w3.org/2000/svg")],
        ),
        (
            '<svg xmlns="http://www.w3.org/2000/svg" data-src=" HTTP://evil.example/"></svg>',
            [("svg", "data-src", " HTTP://evil.example/")],
        ),
    ],
    ids=[
        "icon-page",
        "https-href",
        "protocol-relative-src",
        "http-stylesheet",
        "https-namespace",
        "xlink-namespace",
        "namespace-on-div",
        "data-src-on-svg",
    ],
)
def test_same_origin_rule_allows_only_the_svg_namespace(
    sample: str, findings: list[tuple[str, str, str]]
) -> None:
    # Inline icons (06-08 on E1-E3, 06-10 on sign-in) keep this rule green; any other
    # absolute URL in an attribute, on any tag, still fails it (R5, TEST-STRATEGY §5.2 #9).
    assert _absolute_url_attributes(ICON_PAGE + sample) == findings


def test_no_template_disables_escaping(tmp_path: Path) -> None:
    templates = sorted(TEMPLATES_DIR.rglob("*.html"))
    names = {p.relative_to(TEMPLATES_DIR).as_posix() for p in templates}
    assert {"base.html", "web/login.html", "404.html", "500.html", "403_csrf.html"} <= names

    forbidden = {
        "safe filter": r"\|\s*safe(seq)?\b",
        "autoescape off": r"{%\s*autoescape\s+off\b",
        "script element": r"<script\b",
        "style element": r"<style\b",
        "style attribute": r"\sstyle\s*=",
        "on* handler": r"\son[a-z]+\s*=",
        "URL to another host": r"(https?:)?//[a-z0-9-]+\.[a-z]",
    }
    for path in templates:
        text = path.read_text(encoding="utf-8")
        for what, pattern in forbidden.items():
            assert not re.search(pattern, text, re.IGNORECASE), f"{path.name}: {what}"

    # Rule 1 also covers Python: no web module marks strings safe, except the one audited
    # icon tag at exactly MARK_SAFE_MODULE.
    def marking_safe(package: Path) -> list[str]:
        modules = (path.relative_to(package).as_posix() for path in package.rglob("*.py"))
        return sorted(
            name
            for name in modules
            if name != MARK_SAFE_MODULE
            and "mark_safe" in (package / name).read_text(encoding="utf-8")
        )

    assert marking_safe(WEB_PACKAGE) == []
    # The exemption is that one path: mark_safe in any other module, or in an icons.py
    # anywhere else, still fails.
    others = ["templatetags/display_time.py", "views.py", "x/templatetags/icons.py"]
    for name in [MARK_SAFE_MODULE, *others]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("from django.utils.safestring import mark_safe\n")
    assert marking_safe(tmp_path) == others
