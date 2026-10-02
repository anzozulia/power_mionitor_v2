"""The login throttle on /login/ (SEC-03, INV-21 #2, D-16, UI-D13).

INV-21 #2: five failed sign-ins within 60 s from one client IP make every sign-in POST from
that IP answer HTTP 429 for 5 minutes, counted from the fifth failure, even with the right
password. The 429 page holds exactly one callout, "Too many failed sign-ins. Try again in 5
minutes.", sends ``Retry-After: 300`` and checks no credentials (UI rule 6). The fifth
failure itself still gets the normal wrong-credentials page, and a GET of /login/ stays a
normal page during the cool-down.

The Django test client sends every request from REMOTE_ADDR 127.0.0.1 unless a test sets
another one. Cases that need exact times inject a FakeClock into SignInView.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import FakeClock
from django.contrib.auth import get_user
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory

from powermon.throttle import store
from powermon.throttle.models import LoginFailure
from powermon.web.admin_sync import sync_admin
from powermon.web.views import SignInView

SIGN_IN_ERROR = "Wrong username or password. Check both and try again."
THROTTLE_MESSAGE = "Too many failed sign-ins. Try again in 5 minutes."
US = timedelta(microseconds=1)


def _input_tag(html: str, name: str) -> str:
    """The rendered ``<input ...>`` tag whose name attribute is ``name``."""
    match = re.search(rf'<input\b[^>]*\bname="{re.escape(name)}"[^>]*>', html)
    assert match is not None, f"no input named {name!r} in the page"
    return match.group(0)


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


# The store (DB): what is read, what is pruned, exact instants


def _failure(ip: str, at: datetime) -> None:
    LoginFailure.objects.create(client_ip=ip, failed_at=at)


@pytest.mark.django_db
def test_store_reads_only_the_lookback_and_prunes_old_rows(fixed_now: datetime) -> None:
    now = fixed_now
    _failure("10.0.0.1", now - timedelta(seconds=360))
    _failure("10.0.0.1", now - timedelta(seconds=360) + US)
    _failure("10.0.0.1", now - timedelta(seconds=359))
    _failure("10.0.0.2", now - timedelta(seconds=10))

    # Strictly newer than now - 360 s, this IP only, oldest first.
    assert store.failures("10.0.0.1", now) == [
        now - timedelta(seconds=360) + US,
        now - timedelta(seconds=359),
    ]

    # Every IP's rows older than 1 h go with the next insert; a row exactly 1 h old stays.
    _failure("10.0.0.1", now - timedelta(hours=1) - US)
    _failure("10.0.0.3", now - timedelta(hours=2))
    _failure("10.0.0.3", now - timedelta(hours=1))

    store.record_failure("10.0.0.4", now)

    assert not LoginFailure.objects.filter(failed_at__lt=now - timedelta(hours=1)).exists()
    assert LoginFailure.objects.filter(client_ip="10.0.0.3").count() == 1
    assert LoginFailure.objects.filter(client_ip="10.0.0.4", failed_at=now).count() == 1
    assert LoginFailure.objects.count() == 6


@pytest.mark.django_db
def test_store_window_is_exact_to_the_microsecond(fixed_now: datetime) -> None:
    start = fixed_now - timedelta(seconds=100)
    for seconds in (0, 15, 30, 45):
        _failure("10.0.0.1", start + timedelta(seconds=seconds))
        _failure("10.0.0.2", start + timedelta(seconds=seconds))
    # Fifth failure exactly 60 s after the first for one IP, 60 s + 1 µs for the other.
    _failure("10.0.0.1", start + timedelta(seconds=60))
    _failure("10.0.0.2", start + timedelta(seconds=60) + US)

    assert store.is_blocked("10.0.0.1", fixed_now)
    assert not store.is_blocked("10.0.0.2", fixed_now)


@pytest.mark.django_db
def test_clear_forgets_only_that_ips_failures(fixed_now: datetime) -> None:
    _failure("10.0.0.1", fixed_now)
    _failure("10.0.0.1", fixed_now)
    _failure("10.0.0.2", fixed_now)

    assert store.clear("10.0.0.1") == 2
    assert store.clear("10.0.0.1") == 0
    assert list(LoginFailure.objects.values_list("client_ip", flat=True)) == ["10.0.0.2"]


# The sign-in view


@pytest.mark.django_db
def test_success_clears_the_ips_failures(
    client: Client, monkeypatch: pytest.MonkeyPatch, fixed_now: datetime
) -> None:
    sync_admin("admin", "pw-one")
    # One time base for the view and the other IP's row, so the 1 h prune keeps that row.
    monkeypatch.setattr(SignInView, "clock", FakeClock(fixed_now))
    _failure("10.0.0.9", fixed_now)
    _fail(client, 4)

    response = _sign_in(client, "admin", "pw-one")

    assert response.status_code == 302
    assert _signed_in(client)
    assert not LoginFailure.objects.filter(client_ip="127.0.0.1").exists()
    # Another IP's failures are untouched.
    assert LoginFailure.objects.filter(client_ip="10.0.0.9").count() == 1


@pytest.mark.django_db
def test_other_ip_is_not_throttled(client: Client) -> None:
    sync_admin("admin", "pw-one")
    _fail(client, 5, REMOTE_ADDR="10.0.0.1")

    assert _sign_in(client, "admin", "pw-one", REMOTE_ADDR="10.0.0.1").status_code == 429
    response = _sign_in(client, "admin", "pw-one", REMOTE_ADDR="10.0.0.2")

    assert response.status_code == 302
    assert _signed_in(client)


@pytest.mark.django_db
def test_forwarded_ip_is_the_key(client: Client) -> None:
    sync_admin("admin", "pw-one")
    _fail(client, 5, HTTP_X_FORWARDED_FOR="198.51.100.7, 203.0.113.9")
    assert LoginFailure.objects.filter(client_ip="203.0.113.9").count() == 5

    # A header that ends in the throttled IP is throttled, whatever comes before it.
    blocked = _sign_in(client, "admin", "pw-one", HTTP_X_FORWARDED_FOR="192.0.2.1, 203.0.113.9")
    assert blocked.status_code == 429

    # The same REMOTE_ADDR with a header that ends in another IP is not.
    response = _sign_in(client, "admin", "pw-one", HTTP_X_FORWARDED_FOR="203.0.113.9, 198.51.100.7")
    assert response.status_code == 302
    assert _signed_in(client)


@pytest.mark.django_db
def test_throttled_page_checks_no_credentials(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_admin("admin", "pw-one")
    _fail(client, 5)
    calls: list[dict[str, Any]] = []

    def recording_authenticate(request: Any = None, **credentials: Any) -> None:
        calls.append(credentials)
        return None

    # AuthenticationForm.clean() calls the name bound in its own module.
    monkeypatch.setattr("django.contrib.auth.forms.authenticate", recording_authenticate)

    for password in ("pw-one", "wrong-pw-xyz", ""):
        response = _sign_in(client, "admin", password)
        assert response.status_code == 429
        html = response.content.decode()
        assert _role_text(html, "alert") == [THROTTLE_MESSAGE]
        assert "Wrong username or password" not in html
    assert calls == []

    # The patch does sit on the path: an unthrottled IP's sign-in goes through it.
    _sign_in(client, "admin", "pw-one", REMOTE_ADDR="10.0.0.2")
    assert len(calls) == 1


@pytest.mark.django_db
def test_cool_down_ends_after_five_minutes(
    client: Client, monkeypatch: pytest.MonkeyPatch, fixed_now: datetime
) -> None:
    sync_admin("admin", "pw-one")
    clock = FakeClock(fixed_now)
    monkeypatch.setattr(SignInView, "clock", clock)
    for _ in range(5):
        _fail(client, 1)
        clock.advance(seconds=10)
    fifth = fixed_now + timedelta(seconds=40)
    assert list(LoginFailure.objects.values_list("failed_at", flat=True).order_by("id"))[-1] == (
        fifth
    )

    clock.set(fifth + timedelta(seconds=299))
    assert _sign_in(client, "admin", "pw-one").status_code == 429
    clock.set(fifth + timedelta(seconds=300) - US)
    assert _sign_in(client, "admin", "pw-one").status_code == 429

    # At exactly 5 minutes after the fifth failure the POST is checked normally.
    clock.set(fifth + timedelta(seconds=300))
    response = _sign_in(client, "admin", "wrong-pw-xyz")

    assert response.status_code == 200
    assert _role_text(response.content.decode(), "alert") == [SIGN_IN_ERROR]
    # The throttled POSTs were never recorded, so the cool-down was not extended.
    assert LoginFailure.objects.count() == 6


@pytest.mark.django_db
def test_throttled_page_keeps_the_username_and_no_password(client: Client) -> None:
    sync_admin("admin", "pw-one")
    for _ in range(5):
        fifth = _sign_in(client, "admin", "wrong-pw-xyz")
    throttled = _sign_in(client, "admin", "pw-one")

    # E9 partial: the 5th failure's page and the 429 page alike.
    for response in (fifth, throttled):
        html = response.content.decode()
        assert 'value="admin"' in _input_tag(html, "username")
        assert "value=" not in _input_tag(html, "password")
        assert "wrong-pw-xyz" not in html
        assert "pw-one" not in html
        # E9 loading: no script; E9 long-text: the P1 single sign-in column.
        assert "<script" not in html
        assert 'class="main flow main--single"' in html
    assert throttled.status_code == 429
    assert _role_text(throttled.content.decode(), "alert") == [THROTTLE_MESSAGE]


@pytest.mark.django_db
def test_throttled_page_keeps_a_same_host_next(client: Client) -> None:
    sync_admin("admin", "pw-one")
    _fail(client, 5)

    form = {"username": "admin", "password": "pw-one"}
    response = client.post("/login/", {**form, "next": "/locations/new/"})
    unsafe = client.post("/login/", {**form, "next": "https://evil.example/"})

    assert response.status_code == 429
    assert 'value="/locations/new/"' in _input_tag(response.content.decode(), "next")
    assert "evil.example" not in unsafe.content.decode()


@pytest.mark.django_db
def test_blank_credentials_count_as_a_failure(client: Client) -> None:
    sync_admin("admin", "pw-one")

    response = _sign_in(client, "", "pw-one")

    assert response.status_code == 200
    assert _role_text(response.content.decode(), "alert") == [SIGN_IN_ERROR]
    assert LoginFailure.objects.filter(client_ip="127.0.0.1").count() == 1


@pytest.mark.django_db
def test_cool_down_start_logs_one_warning_with_the_ip_only(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    sync_admin("admin", "pw-one")

    with caplog.at_level(logging.WARNING, logger="powermon.web.views"):
        _fail(client, 4)
        assert not [r for r in caplog.records if r.name == "powermon.web.views"]
        _fail(client, 1)
        _sign_in(client, "admin", "pw-one")

    warnings = [r.getMessage() for r in caplog.records if r.name == "powermon.web.views"]
    assert len(warnings) == 1
    assert "127.0.0.1" in warnings[0]
    assert "admin" not in warnings[0]
    assert "wrong-pw-xyz" not in warnings[0]


@pytest.mark.django_db
def test_failure_is_stamped_with_the_views_clock(rf: RequestFactory, fixed_now: datetime) -> None:
    request = rf.post(
        "/login/", {"username": "admin", "password": "wrong-pw-xyz"}, REMOTE_ADDR="10.0.0.3"
    )
    request.user = AnonymousUser()
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    # SignInView is csrf_protect-decorated; RequestFactory sends no token.
    request._dont_enforce_csrf_checks = True  # type: ignore[attr-defined]

    response = SignInView.as_view(clock=FakeClock(fixed_now))(request)

    assert response.status_code == 200
    failure = LoginFailure.objects.get()
    assert (failure.client_ip, failure.failed_at) == ("10.0.0.3", fixed_now)
