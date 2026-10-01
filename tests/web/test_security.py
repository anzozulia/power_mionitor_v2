"""Production security of the admin surface (SEC-02, INV-22 #3, D-17) and its error pages.

Over HTTPS, directly or behind Caddy's X-Forwarded-Proto, the admin pages set Secure
session and CSRF cookies and HSTS for one year (no includeSubDomains, no preload). Every
admin page carries the strict CSP and Django's header defaults and loads one same-origin
stylesheet and no script. 404, 500 and CSRF-failure responses render fixed custom copy
and never echo the request (UI-SPEC error pages, security-bound UI rules 1 and 5).

The ``production_settings`` fixture mirrors what APP_ENV=production builds; the subprocess
tests below prove that against a real production process.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.views.defaults import server_error

from powermon.web.admin_sync import sync_admin

# UI-SPEC security-bound UI rule 5, written out so a changed constant fails here.
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
HSTS = "max-age=31536000"
TEMPLATES_DIR = Path(settings.BASE_DIR) / "powermon" / "web" / "templates"
WEB_PACKAGE = Path(settings.BASE_DIR) / "powermon" / "web"

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
    response = {
        "login": lambda: client.get("/login/"),
        "404": lambda: client.get("/no-such-page-xyz"),
        "csrf-403": lambda: _csrf_failure("/login/"),
    }[page]()

    assert response["Content-Security-Policy"] == CSP
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "same-origin"


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
    assert "<h1>Page not found</h1>" in html
    assert "This page does not exist. Check the address, or go back to the location list." in html
    assert '<a href="/">Back to locations</a>' in html
    assert "no-such-page-xyz" not in html
    assert "echo-me" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/login/", "/logout/"])
def test_csrf_failure_renders_form_expired(path: str) -> None:
    # A form open too long (or a stale tab's Sign out) arrives without a valid token.
    response = _csrf_failure(path)

    assert response.status_code == 403
    html = response.content.decode()
    assert "<title>Form expired · Power Monitor</title>" in html
    assert "<h1>Form expired</h1>" in html
    assert (
        "This form was open too long, or the browser blocked cookies. "
        "Go back, reload the page and try again."
    ) in html
    assert '<a href="/">Back to locations</a>' in html
    # Django's own CSRF page explains the failure reason; ours shows fixed copy only.
    assert "CSRF" not in html


@pytest.mark.django_db
def test_csrf_failure_with_a_stale_token_renders_form_expired() -> None:
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.CSRF_COOKIE_NAME] = "a" * 32

    response = client.post("/logout/", {"csrfmiddlewaretoken": "b" * 32})

    assert response.status_code == 403
    assert "<h1>Form expired</h1>" in response.content.decode()


def test_500_page_renders_without_request_context() -> None:
    response = _server_error()

    assert response.status_code == 500
    html = response.content.decode()
    assert "<title>Server error · Power Monitor</title>" in html
    assert "<h1>Something went wrong</h1>" in html
    assert (
        "The server could not finish this request. Details are in the web container logs. "
        "Try again in a moment."
    ) in html
    assert '<a href="/">Back to locations</a>' in html
    # Renders with no context at all, as Django's handler does.
    assert "Something went wrong" in render_to_string("500.html")


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["login", "404", "csrf-403", "500"])
def test_pages_load_only_same_origin_assets(client: Client, page: str) -> None:
    response = {
        "login": lambda: client.get("/login/"),
        "404": lambda: client.get("/no-such-page-xyz"),
        "csrf-403": lambda: _csrf_failure("/login/"),
        "500": _server_error,
    }[page]()
    html = response.content.decode()

    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html
    # Exactly one stylesheet: the hashed app.css from the WhiteNoise manifest (P-24).
    links = re.findall(r"<link\b[^>]*>", html)
    assert len(links) == 1, links
    assert re.fullmatch(
        r'<link rel="stylesheet" href="/static/web/app\.[0-9a-f]{12}\.css">', links[0]
    )
    assert '<meta name="robots" content="noindex, nofollow">' in html


def test_no_template_disables_escaping() -> None:
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
    # Rule 1 also covers Python: no web module marks strings safe.
    for path in WEB_PACKAGE.rglob("*.py"):
        assert "mark_safe" not in path.read_text(encoding="utf-8"), path.name
