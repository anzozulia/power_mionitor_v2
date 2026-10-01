"""The env-defined single admin account (LOC-01, INV-21 #3) and the ``release`` command (D-02).

``release`` runs in the one-shot migrate service on every deploy: it applies migrations and
then syncs the one admin account from ADMIN_USERNAME / ADMIN_PASSWORD.

01-07 adds the sign-in and sign-out tests to this file.
"""

from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command

from powermon.web.admin_sync import sync_admin

User = get_user_model()


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
