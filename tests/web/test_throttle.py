"""The login throttle on /login/ (SEC-03, INV-21 #2, D-16, UI-D13).

INV-21 #2: five failed sign-ins within 60 s from one client IP make every sign-in POST from
that IP answer HTTP 429 for 5 minutes, counted from the fifth failure, even with the right
password. The 429 page holds exactly one callout, "Too many failed sign-ins. Try again in 5
minutes.", sends ``Retry-After: 300`` and checks no credentials (UI rule 6). The fifth
failure itself still gets the normal wrong-credentials page, and a GET of /login/ stays a
normal page during the cool-down.

The Django test client sends every request from REMOTE_ADDR 127.0.0.1.
"""

import re
from typing import Any

import pytest
from django.contrib.auth import get_user
from django.test import Client

from powermon.throttle.models import LoginFailure
from powermon.web.admin_sync import sync_admin

SIGN_IN_ERROR = "Wrong username or password. Check both and try again."
THROTTLE_MESSAGE = "Too many failed sign-ins. Try again in 5 minutes."


def _role_text(html: str, role: str) -> list[str]:
    """The text directly inside each element that carries ``role="<role>"``."""
    return [t.strip() for t in re.findall(rf'role="{role}"[^>]*>([^<]*)<', html)]


def _sign_in(client: Client, username: str, password: str, **extra: Any) -> Any:
    return client.post("/login/", {"username": username, "password": password}, **extra)


def _signed_in(client: Client) -> bool:
    return bool(get_user(client).is_authenticated)  # type: ignore[arg-type]


def _fail(client: Client, times: int, **extra: Any) -> None:
    """``times`` wrong-password sign-ins, each answered with the wrong-credentials page."""
    for _ in range(times):
        response = _sign_in(client, "admin", "wrong-pw-xyz", **extra)
        assert response.status_code == 200
        assert _role_text(response.content.decode(), "alert") == [SIGN_IN_ERROR]


@pytest.mark.django_db
def test_INV21_2_five_failures_then_429_even_with_the_right_password(client: Client) -> None:
    sync_admin("admin", "pw-one")

    _fail(client, 5)
    response = _sign_in(client, "admin", "pw-one")

    assert response.status_code == 429
    assert _role_text(response.content.decode(), "alert") == [THROTTLE_MESSAGE]
    assert response["Retry-After"] == "300"
    assert not _signed_in(client)
    assert LoginFailure.objects.filter(client_ip="127.0.0.1").count() == 5


@pytest.mark.django_db
def test_INV21_2_throttled_post_adds_no_failure(client: Client) -> None:
    sync_admin("admin", "pw-one")
    _fail(client, 5)

    for password in ("wrong-pw-xyz", "pw-one"):
        assert _sign_in(client, "admin", password).status_code == 429

    assert LoginFailure.objects.count() == 5


@pytest.mark.django_db
def test_INV21_2_get_of_the_sign_in_page_stays_200_during_the_cool_down(client: Client) -> None:
    sync_admin("admin", "pw-one")
    _fail(client, 5)

    response = client.get("/login/")

    assert response.status_code == 200
    assert _role_text(response.content.decode(), "alert") == []
