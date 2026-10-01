"""The env-defined single admin account (LOC-01, INV-21 #3), the ``release`` command (D-02),
and signing in and out (D-09 surface 1, UI-SPEC screen 1).

``release`` runs in the one-shot migrate service on every deploy: it applies migrations and
then syncs the one admin account from ADMIN_USERNAME / ADMIN_PASSWORD. The admin then signs
in at /login/ with exactly that account and signs out with a POST to /logout/.
"""

import re
from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import authenticate, get_user, get_user_model
from django.core.management import call_command
from django.test import Client

from powermon.web.admin_sync import sync_admin

User = get_user_model()

SIGN_IN_ERROR = "Wrong username or password. Check both and try again."


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


# sign in / sign out (UI-SPEC screen 1)


def _input_tag(html: str, name: str) -> str:
    """The rendered ``<input ...>`` tag whose name attribute is ``name``."""
    match = re.search(rf'<input\b[^>]*\bname="{re.escape(name)}"[^>]*>', html)
    assert match is not None, f"no input named {name!r} in the page"
    return match.group(0)


def _role_text(html: str, role: str) -> list[str]:
    """The text directly inside each element that carries ``role="<role>"``."""
    return [t.strip() for t in re.findall(rf'role="{role}"[^>]*>([^<]*)<', html)]


def _sign_in(client: Client, username: str, password: str, **extra: str) -> Any:
    return client.post("/login/", {"username": username, "password": password, **extra})


def _signed_in(client: Client) -> bool:
    return bool(get_user(client).is_authenticated)  # type: ignore[arg-type]


@pytest.mark.django_db
def test_LOC01_sign_in_page_renders_fields(client: Client) -> None:
    response = client.get("/login/")

    assert response.status_code == 200
    html = response.content.decode()
    assert "<title>Sign in · Power Monitor</title>" in html
    username = _input_tag(html, "username")
    assert 'autocomplete="username"' in username
    assert "autofocus" in username
    password = _input_tag(html, "password")
    assert 'type="password"' in password
    assert 'autocomplete="current-password"' in password
    # First load (UI-SPEC E1 empty state): both fields start empty.
    assert "value=" not in username
    assert "value=" not in password
    assert 'name="csrfmiddlewaretoken"' in html
    assert "placeholder" not in html
    assert "<script" not in html


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
    html = response.content.decode()
    assert _role_text(html, "alert") == [SIGN_IN_ERROR]
    assert 'value="admin"' in _input_tag(html, "username")
    assert "wrong-pw-xyz" not in html
    assert "value=" not in _input_tag(html, "password")
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
    assert _role_text(html, "alert") == [SIGN_IN_ERROR]
    assert "This field is required." not in html
    assert "pw-one" not in html
    if username == "admin":
        assert 'value="admin"' in _input_tag(html, "username")
    assert not _signed_in(client)


@pytest.mark.django_db
def test_INV21_old_password_rejected_after_env_change(client: Client) -> None:
    sync_admin("admin", "pw-one")
    sync_admin("admin", "pw-two")

    old = _sign_in(client, "admin", "pw-one")

    assert old.status_code == 200
    assert _role_text(old.content.decode(), "alert") == [SIGN_IN_ERROR]
    assert not _signed_in(client)

    new = _sign_in(client, "admin", "pw-two")

    assert new.status_code == 302
    assert new.url == "/"
    assert _signed_in(client)


@pytest.mark.django_db
def test_next_same_host_is_honoured(client: Client) -> None:
    sync_admin("admin", "pw-one")
    form = client.get("/login/", {"next": "/locations/new/"}).content.decode()
    # The form carries the validated target in its hidden next field.
    assert 'value="/locations/new/"' in _input_tag(form, "next")

    response = _sign_in(client, "admin", "pw-one", next="/locations/new/")

    assert response.status_code == 302
    assert response.url == "/locations/new/"


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_next_off_host_is_ignored(client: Client, target: str) -> None:
    sync_admin("admin", "pw-one")
    form = client.get("/login/", {"next": target}).content.decode()
    assert "evil.example" not in form

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
    page = client.get("/login/").content.decode()
    assert _role_text(page, "status") == ["You are signed out."]
    # A flash is shown once.
    assert _role_text(client.get("/login/").content.decode(), "status") == []


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
