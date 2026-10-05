"""The env-defined single admin account (LOC-01, INV-21 #3), the ``release`` command (D-02),
and signing in and out on S1 (D-09 surface 1; UI-01, UI-09, UI-12).

``release`` runs in the one-shot migrate service on every deploy: it applies migrations and
then syncs the one admin account from ADMIN_USERNAME / ADMIN_PASSWORD. The admin then signs
in at /login/ with exactly that account and signs out with a POST to /logout/.

S1 is read only through tests/web/pages.py and the 06-UI-SPEC hooks (``sign-in-form``,
``form-error``, ``#id_username``, ``#id_password``, the hidden ``next``, the toast
regions), never through classes or raw markup. Python-owned copy is imported; template copy
is pinned against the 06-UI-SPEC copy table (signin.*).
"""

from io import StringIO
from typing import Any

import pytest
from bs4 import Tag
from django.conf import settings
from django.contrib.auth import authenticate, get_user, get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import Client
from django.test.html import Element, parse_html
from pages import (
    Message,
    Page,
    all_by_testid,
    assert_no_injected_script,
    assert_page,
    by_testid,
    field,
    h1,
    hidden_value,
    messages,
    parse,
    text,
    title,
)

from powermon.web.admin_sync import sync_admin
from powermon.web.forms import SIGN_IN_ERROR
from powermon.web.templatetags.icons import ICONS
from powermon.web.views import SIGNED_OUT_MESSAGE

User = get_user_model()

# 06-UI-SPEC copy rows signin.title / h1 / submit, signin.username, signin.password.
SIGN_IN = "Sign in"
USERNAME_LABEL = "Username"
PASSWORD_LABEL = "Password"
# Components > Alert: an error alert starts with this visually hidden prefix.
ERROR_PREFIX = "Error: "
# The S1 hooks of the app layout that the auth layout never has (Test hooks > S1, Absent).
APP_ONLY_HOOKS = ("sidebar", "sidebar-locations", "topbar", "theme-switch")


def _snapshot() -> list[tuple[Any, ...]]:
    return list(User.objects.order_by("pk").values_list("pk", "username", "password", "is_active"))


# sync_admin


@pytest.mark.django_db
def test_INV21_sync_creates_the_single_admin() -> None:
    sync_admin("admin", "pw-one")

    user = User.objects.get()
    assert user.username == "admin"
    assert user.is_active
    assert user.check_password("pw-one")
    assert authenticate(username="admin", password="pw-one") == user


@pytest.mark.django_db
def test_INV21_password_change_only_new_works() -> None:
    sync_admin("admin", "pw-one")
    sync_admin("admin", "pw-two")

    assert User.objects.count() == 1
    assert authenticate(username="admin", password="pw-two") is not None
    assert authenticate(username="admin", password="pw-one") is None


@pytest.mark.django_db
def test_INV21_username_rename_keeps_one_account() -> None:
    sync_admin("admin", "pw")
    pk = User.objects.get().pk

    sync_admin("root", "pw")

    user = User.objects.get()
    assert (user.pk, user.username) == (pk, "root")
    assert authenticate(username="root", password="pw") == user
    assert authenticate(username="admin", password="pw") is None


@pytest.mark.django_db
def test_INV21_extra_accounts_removed() -> None:
    User.objects.create_user("intruder", password="x")
    User.objects.create_user("second-admin", password="y")

    sync_admin("admin", "pw")

    assert list(User.objects.values_list("username", flat=True)) == ["admin"]
    assert authenticate(username="intruder", password="x") is None


@pytest.mark.django_db
def test_INV21_account_with_the_env_username_is_the_one_kept() -> None:
    User.objects.create_user("older", password="x")
    admin = User.objects.create_user("admin", password="pw")

    sync_admin("admin", "pw")

    assert list(User.objects.values_list("pk", "username")) == [(admin.pk, "admin")]


@pytest.mark.django_db
def test_sync_reactivates_a_disabled_admin() -> None:
    User.objects.create_user("admin", password="pw", is_active=False)

    sync_admin("admin", "pw")

    assert User.objects.get().is_active
    assert authenticate(username="admin", password="pw") is not None


@pytest.mark.django_db
def test_sync_without_change_keeps_the_password_hash() -> None:
    # P-13: re-hashing on every deploy would change the session hash and sign the admin out.
    sync_admin("admin", "pw")
    before = User.objects.get().password

    sync_admin("admin", "pw")

    assert User.objects.get().password == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("", "x", "username"),
        ("   ", "x", "username"),
        ("admin", "", "password"),
        ("admin", "   ", "password"),
    ],
)
def test_sync_rejects_empty_credentials(username: str, password: str, message: str) -> None:
    User.objects.create_user("admin", password="pw")
    User.objects.create_user("other", password="pw")
    before = _snapshot()

    with pytest.raises(ValueError, match=message):
        sync_admin(username, password)

    assert _snapshot() == before


# release


@pytest.mark.django_db(transaction=True)
def test_release_command_migrates_and_syncs() -> None:
    User.objects.create_user("stray", password="x")
    out, err = StringIO(), StringIO()

    call_command("release", stdout=out, stderr=err)

    user = User.objects.get()
    assert user.username == settings.CFG.admin_username
    assert user.check_password(settings.CFG.admin_password)
    # migrate ran first, through the same output stream.
    assert "Operations to perform" in out.getvalue()
    assert f"admin account synced: {settings.CFG.admin_username}" in out.getvalue()
    assert settings.CFG.admin_password not in out.getvalue() + err.getvalue()


# Sign in / sign out (S1, 06-UI-SPEC Page Contracts > S1, Test hooks > S1)


def _sign_in(client: Client, username: str, password: str, **extra: str) -> Any:
    return client.post("/login/", {"username": username, "password": password, **extra})


def _signed_in(client: Client) -> bool:
    return bool(get_user(client).is_authenticated)  # type: ignore[arg-type]


def _alerts(page: Page) -> list[str]:
    """The text of every ``role="alert"`` element that holds text, as a screen reader reads it.

    The toast regions are always on the page and empty unless an error flash is queued, so
    an empty region is left out. A message keeps its visually hidden "Error: " / "Warning: "
    prefix, which is part of what is announced.
    """
    soup = page if isinstance(page, Tag) else parse(page)
    return [found for element in soup.find_all(attrs={"role": "alert"}) if (found := text(element))]


def _is_icon(svg: Tag, name: str) -> bool:
    """A parsed inline ``<svg>`` draws the shapes of the vendored icon ``name``."""
    drawn = parse_html(f"<g>{svg.decode_contents()}</g>")
    vendored = parse_html(f"<g>{ICONS[name]}</g>")
    assert isinstance(drawn, Element) and isinstance(vendored, Element)
    return drawn.children == vendored.children


@pytest.mark.django_db
def test_UI01_sign_in_page_renders(client: Client) -> None:
    page = assert_page(client.get("/login/"), title=SIGN_IN, app=False)

    assert text(h1(page)) == SIGN_IN
    form = by_testid(page, "sign-in-form")
    assert (form.get("method"), form.get("action")) == ("post", "/login/")
    # Two field groups, each input with its label.
    labels = [(label.get("for"), text(label)) for label in form.find_all("label")]
    assert labels == [("id_username", USERNAME_LABEL), ("id_password", PASSWORD_LABEL)]
    username, password = field(form, "username"), field(form, "password")
    assert (username.get("autocomplete"), username.has_attr("autofocus")) == ("username", True)
    assert (password.get("type"), password.get("autocomplete")) == ("password", "current-password")
    assert not username.has_attr("value") and not password.has_attr("value")
    assert hidden_value(form, "next") == ""
    # One full-width primary submit, rendered enabled, with the hidden pending spinner.
    (submit,) = form.find_all("button")
    assert (submit.get("type"), submit.get("data-variant"), text(submit)) == (
        "submit",
        "primary",
        SIGN_IN,
    )
    assert not submit.has_attr("disabled") and not submit.has_attr("aria-disabled")
    (spinner,) = submit.find_all("svg")
    assert _is_icon(spinner, "loader-circle")
    assert (spinner.get("aria-hidden"), spinner.get("focusable")) == ("true", "false")
    # E1 empty: no alert and nothing of the app layout; both toast regions, empty.
    assert all_by_testid(page, "form-error") == all_by_testid(page, "throttle-message") == []
    assert _alerts(page) == []
    for hook in APP_ONLY_HOOKS:
        assert all_by_testid(page, hook) == [], hook
    for region in ("toasts-status", "toasts-alert"):
        assert len(all_by_testid(page, region)) == 1, region
    assert messages(page) == []


@pytest.mark.django_db
def test_UI01_sign_in_head(client: Client) -> None:
    page = parse(client.get("/login/"))
    head = page.find("head")
    assert isinstance(head, Tag)

    # The two scripts the CSP allows, both in the head: admin.js render-blocking first,
    # then the Alpine CSP build deferred (06-UI-SPEC Interaction Contract > JavaScript rules).
    scripts = page.find_all("script")
    assert [(script.get("src"), script.has_attr("defer")) for script in scripts] == [
        (staticfiles_storage.url("web/admin.js"), False),
        (staticfiles_storage.url("web/vendor/alpine-csp-3.17.4.min.js"), True),
    ]
    assert all(script.find_parent("head") is head for script in scripts)
    # Font preloads (latin, cyrillic) with crossorigin, the stylesheet, both favicons.
    links = [
        (
            " ".join(link.get("rel", [])),
            link.get("href"),
            link.get("as"),
            link.get("type"),
            link.has_attr("crossorigin"),
            link.get("sizes"),
        )
        for link in head.find_all("link")
    ]
    assert links == [
        (
            "preload",
            staticfiles_storage.url("web/fonts/inter-latin-wght-normal.woff2"),
            "font",
            "font/woff2",
            True,
            None,
        ),
        (
            "preload",
            staticfiles_storage.url("web/fonts/inter-cyrillic-wght-normal.woff2"),
            "font",
            "font/woff2",
            True,
            None,
        ),
        ("stylesheet", staticfiles_storage.url("web/build/app.css"), None, None, False, None),
        ("icon", staticfiles_storage.url("web/favicon.svg"), None, "image/svg+xml", False, None),
        ("icon", staticfiles_storage.url("web/favicon.ico"), None, None, False, "32x32"),
    ]
    # The skip link is the first focusable element and leads to main.
    skip = by_testid(page, "skip-link")
    assert (skip.get("href"), text(skip)) == ("#main", "Skip to content")
    assert page.find(["a", "button", "input"]) is skip


@pytest.mark.django_db
def test_LOC01_sign_in_page_renders_fields(client: Client) -> None:
    response = client.get("/login/")

    assert response.status_code == 200
    page = parse(response)
    assert title(page) == "Sign in · Power Monitor"
    username = field(page, "username")
    assert username.get("autocomplete") == "username"
    assert username.has_attr("autofocus")
    password = field(page, "password")
    assert password.get("type") == "password"
    assert password.get("autocomplete") == "current-password"
    # First load (UI-SPEC E1 empty state): both fields start empty.
    assert not username.has_attr("value")
    assert not password.has_attr("value")
    csrf = by_testid(page, "sign-in-form").find_all("input", attrs={"name": "csrfmiddlewaretoken"})
    assert len(csrf) == 1
    # No control shows a placeholder.
    controls = page.find_all(["input", "select", "textarea"])
    assert [control for control in controls if control.has_attr("placeholder")] == []
    assert_no_injected_script(response.content.decode())


@pytest.mark.django_db
def test_LOC01_env_admin_signs_in(client: Client) -> None:
    sync_admin("admin", "pw-one")

    response = _sign_in(client, "admin", "pw-one")

    assert response.status_code == 302
    assert response.url == "/"
    user = get_user(client)  # type: ignore[arg-type]
    assert user.is_authenticated
    assert user.username == "admin"


@pytest.mark.django_db
def test_wrong_password_shows_generic_error_and_clears_password(client: Client) -> None:
    sync_admin("admin", "pw-one")

    response = _sign_in(client, "admin", "wrong-pw-xyz")

    assert response.status_code == 200
    page = parse(response)
    assert _alerts(page) == [ERROR_PREFIX + SIGN_IN_ERROR]
    assert field(page, "username").get("value") == "admin"
    assert "wrong-pw-xyz" not in response.content.decode()
    assert not field(page, "password").has_attr("value")
    assert not _signed_in(client)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("username", "password"),
    [("", "pw-one"), ("admin", ""), ("", ""), ("   ", "pw-one")],
    ids=["blank-username", "blank-password", "both-blank", "whitespace-username"],
)
def test_blank_fields_show_the_same_error(client: Client, username: str, password: str) -> None:
    sync_admin("admin", "pw-one")

    response = _sign_in(client, username, password)

    assert response.status_code == 200
    html = response.content.decode()
    assert _alerts(html) == [ERROR_PREFIX + SIGN_IN_ERROR]
    assert "This field is required." not in html
    assert "pw-one" not in html
    if username == "admin":
        assert field(html, "username").get("value") == "admin"
    assert not _signed_in(client)


@pytest.mark.django_db
def test_INV21_old_password_rejected_after_env_change(client: Client) -> None:
    sync_admin("admin", "pw-one")
    sync_admin("admin", "pw-two")

    old = _sign_in(client, "admin", "pw-one")

    assert old.status_code == 200
    assert _alerts(old) == [ERROR_PREFIX + SIGN_IN_ERROR]
    assert not _signed_in(client)

    new = _sign_in(client, "admin", "pw-two")

    assert new.status_code == 302
    assert new.url == "/"
    assert _signed_in(client)


@pytest.mark.django_db
def test_next_same_host_is_honoured(client: Client) -> None:
    sync_admin("admin", "pw-one")
    form = by_testid(client.get("/login/", {"next": "/locations/new/"}), "sign-in-form")
    # The form carries the validated target in its hidden next field.
    assert hidden_value(form, "next") == "/locations/new/"

    response = _sign_in(client, "admin", "pw-one", next="/locations/new/")

    assert response.status_code == 302
    assert response.url == "/locations/new/"


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_next_off_host_is_ignored(client: Client, target: str) -> None:
    sync_admin("admin", "pw-one")
    page = client.get("/login/", {"next": target})
    assert "evil.example" not in page.content.decode()
    assert hidden_value(by_testid(page, "sign-in-form"), "next") == ""

    response = _sign_in(client, "admin", "pw-one", next=target)

    assert response.status_code == 302
    assert response.url == "/"


@pytest.mark.django_db
def test_sign_out_is_post_only_and_flashes(client: Client) -> None:
    sync_admin("admin", "pw-one")
    client.force_login(User.objects.get())

    assert client.get("/logout/").status_code == 405
    assert _signed_in(client)

    response = client.post("/logout/")

    assert response.status_code == 302
    assert response.url == "/login/"
    assert not _signed_in(client)
    page = client.get("/login/")
    assert messages(page) == [Message("info", "status", SIGNED_OUT_MESSAGE)]
    # A flash is shown once.
    assert messages(client.get("/login/")) == []


@pytest.mark.django_db
def test_sign_out_when_already_signed_out_lands_on_login(client: Client) -> None:
    # P-23: the logout view is login-exempt, so a stale tab's Sign out does not bounce
    # through /login/?next=/logout/ (a GET of /logout/ after sign-in would be a 405).
    response = client.post("/logout/")

    assert response.status_code == 302
    assert response.url == "/login/"


@pytest.mark.django_db
def test_signed_in_user_visiting_login_is_redirected(client: Client) -> None:
    sync_admin("admin", "pw-one")
    client.force_login(User.objects.get())

    response = client.get("/login/")

    assert response.status_code == 302
    assert response.url == "/"
