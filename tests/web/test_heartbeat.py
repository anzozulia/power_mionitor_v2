"""The device heartbeat endpoint /hb (HB-01, HB-02, MON-01; INV-24 #1-#2, INV-01 #3, K-1).

Time-dependent tests serve HeartbeatView through RequestFactory with an injected FakeClock.
Routing, status-code and middleware tests go through the Django test client, so the
whole middleware stack (CSRF, login-required, CSP, slash handling) is part of the check.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import FakeClock
from django.http import HttpResponse
from django.test import Client, RequestFactory

from powermon.engine.models import LocationState
from powermon.locations.keys import generate_device_key
from powermon.web import views

CHALLENGE = 'Bearer realm="heartbeat"'


def _bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


def _serve(clock: FakeClock, request: Any) -> HttpResponse:
    response: HttpResponse = views.HeartbeatView.as_view(clock=clock)(request)
    return response


def _state(location: Any) -> LocationState:
    return LocationState.objects.get(pk=location.pk)


def _all_states() -> list[dict[str, Any]]:
    return list(LocationState.objects.order_by("pk").values())


def _spy_on_record_heartbeat(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record what each record_heartbeat call returned, while it still runs for real."""
    from powermon.engine import transitions

    real = transitions.record_heartbeat
    results: list[str] = []

    def spy(location_id: int, now: datetime) -> str:
        result = real(location_id, now)
        results.append(result)
        return result

    monkeypatch.setattr(transitions, "record_heartbeat", spy)
    return results


def _assert_unauthorized(response: Any) -> None:
    assert response.status_code == 401
    assert response.content == b"unauthorized"
    assert response["Content-Type"] == "text/plain"
    assert response["WWW-Authenticate"] == CHALLENGE


# Accepted heartbeats (HB-01, MON-01)


@pytest.mark.django_db
def test_K1_first_heartbeat_sets_status_on(
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = location_factory()
    results = _spy_on_record_heartbeat(monkeypatch)

    response = _serve(FakeClock(fixed_now), rf.get("/hb", headers=_bearer(location.device_key)))

    assert response.status_code == 200
    assert response.content == b"ok"
    assert response["Content-Type"] == "text/plain"
    state = _state(location)
    assert state.status == "on"
    # The server receive time is the only timestamp.
    assert state.on_since == fixed_now
    assert state.last_heartbeat_at == fixed_now
    assert state.outage_started_at is None
    assert state.state_version == 1
    # Monitoring starts silently: the transition is "started", not a restore.
    assert results == ["started"]


@pytest.mark.django_db
def test_HB01_post_with_bearer_and_get_with_query_key_accepted(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory()

    posted = client.post("/hb", headers=_bearer(location.device_key))
    got = client.get("/hb", {"key": location.device_key})

    assert (posted.status_code, posted.content) == (200, b"ok")
    assert (got.status_code, got.content) == (200, b"ok")
    state = _state(location)
    assert state.status == "on"
    assert state.state_version == 2


@pytest.mark.django_db
@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
def test_HB01_bearer_scheme_is_case_insensitive(
    client: Client, location_factory: Callable[..., Any], scheme: str
) -> None:
    location = location_factory()

    response = client.get("/hb", headers={"authorization": f"{scheme} {location.device_key}"})

    assert (response.status_code, response.content) == (200, b"ok")
    assert _state(location).status == "on"


@pytest.mark.django_db
def test_plain_heartbeat_advances_last_heartbeat(
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = location_factory()
    clock = FakeClock(fixed_now)
    results = _spy_on_record_heartbeat(monkeypatch)
    _serve(clock, rf.get("/hb", headers=_bearer(location.device_key)))
    version_after_first = _state(location).state_version

    clock.advance(minutes=1)
    response = _serve(clock, rf.post("/hb", headers=_bearer(location.device_key)))

    assert response.status_code == 200
    state = _state(location)
    assert state.last_heartbeat_at == fixed_now + timedelta(minutes=1)
    assert state.on_since == fixed_now
    assert state.state_version == version_after_first + 1
    assert results == ["started", "plain"]


@pytest.mark.django_db
def test_older_heartbeat_never_moves_last_heartbeat_back(
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = location_factory()
    later = fixed_now + timedelta(minutes=1)
    clock = FakeClock(later)
    results = _spy_on_record_heartbeat(monkeypatch)
    _serve(clock, rf.get("/hb", headers=_bearer(location.device_key)))

    # A request that was stamped earlier but committed later (a slow request).
    clock.set(fixed_now - timedelta(minutes=1))
    response = _serve(clock, rf.get("/hb", headers=_bearer(location.device_key)))

    assert response.status_code == 200
    state = _state(location)
    assert state.last_heartbeat_at == later
    assert state.on_since == later
    assert results == ["started", "plain"]


@pytest.mark.django_db
def test_HB01_repeated_first_heartbeat_does_not_restart_monitoring(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    from powermon.engine.transitions import record_heartbeat

    location = location_factory()

    # The waiting -> on gate changes 1 row once; a repeat matches 0 rows there.
    assert record_heartbeat(location.pk, fixed_now) == "started"
    assert record_heartbeat(location.pk, fixed_now) == "plain"
    assert record_heartbeat(location.pk, fixed_now + timedelta(seconds=5)) == "plain"

    state = _state(location)
    assert state.status == "on"
    assert state.on_since == fixed_now
    assert state.last_heartbeat_at == fixed_now + timedelta(seconds=5)
    assert state.state_version == 3


@pytest.mark.django_db
def test_record_heartbeat_ignores_a_location_without_state(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    from powermon.engine.transitions import record_heartbeat

    location = location_factory()
    before = _all_states()

    assert record_heartbeat(location.pk + 1000, fixed_now) == "ignored"

    assert _all_states() == before


# Rejected requests (HB-02, INV-24 #2): 401 and no row changes


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("get", "/hb", {}),
        ("post", "/hb", {}),
        ("get", "/hb?key=", {}),
        ("get", "/hb", {"authorization": "Bearer"}),
        ("get", "/hb", {"authorization": "Bearer "}),
    ],
    ids=["get", "post", "empty-query-key", "bearer-without-key", "bearer-blank-key"],
)
def test_INV24_missing_key_401_no_writes(
    client: Client,
    location_factory: Callable[..., Any],
    django_assert_num_queries: Any,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    location_factory()
    before = _all_states()

    with django_assert_num_queries(0):
        response = getattr(client, method)(path, headers=headers)

    _assert_unauthorized(response)
    assert _all_states() == before


@pytest.mark.django_db
@pytest.mark.parametrize("transport", ["bearer-get", "bearer-post", "query-get", "query-post"])
def test_INV24_unknown_key_401_no_writes(
    client: Client,
    location_factory: Callable[..., Any],
    django_assert_num_queries: Any,
    transport: str,
) -> None:
    location_factory()
    unknown = generate_device_key()
    before = _all_states()
    method = transport.rsplit("-", 1)[1]
    if transport.startswith("bearer"):
        path, headers = "/hb", _bearer(unknown)
    else:
        path, headers = f"/hb?key={unknown}", {}

    # One indexed lookup, nothing else.
    with django_assert_num_queries(1):
        response = getattr(client, method)(path, headers=headers)

    _assert_unauthorized(response)
    assert _all_states() == before


MALFORMED: dict[str, Callable[[str], str]] = {
    "31-characters": lambda key: key[:31],
    "33-characters": lambda key: key + "A",
    "percent": lambda key: key[:31] + "%",
    "non-ascii-letter": lambda key: key[:31] + "А",
    "non-ascii-digit": lambda key: key[:31] + "١",
    "trailing-newline": lambda key: key + "\n",
}


@pytest.mark.django_db
@pytest.mark.parametrize("mangle", list(MALFORMED.values()), ids=list(MALFORMED))
def test_INV24_malformed_key_rejected_before_any_query(
    client: Client,
    location_factory: Callable[..., Any],
    django_assert_num_queries: Any,
    mangle: Callable[[str], str],
) -> None:
    # Derived from a real key, so a lax lookup (prefix, normalisation) would find it.
    location = location_factory()
    before = _all_states()

    with django_assert_num_queries(0):
        response = client.get("/hb", {"key": mangle(location.device_key)})

    _assert_unauthorized(response)
    assert _all_states() == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mangle",
    [MALFORMED["31-characters"], MALFORMED["33-characters"]],
    ids=["31-characters", "33-characters"],
)
def test_INV24_malformed_bearer_key_rejected_before_any_query(
    client: Client,
    location_factory: Callable[..., Any],
    django_assert_num_queries: Any,
    mangle: Callable[[str], str],
) -> None:
    location = location_factory()
    before = _all_states()

    with django_assert_num_queries(0):
        response = client.post("/hb", headers=_bearer(mangle(location.device_key)))

    _assert_unauthorized(response)
    assert _all_states() == before


@pytest.mark.django_db
def test_deleted_location_key_401(
    client: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(deleted_at=fixed_now)

    response = client.get("/hb", headers=_bearer(location.device_key))

    _assert_unauthorized(response)
    assert _state(location).status == "waiting"


# Routing: the exact URL, never a redirect (INV-24, D-06)


@pytest.mark.django_db
def test_INV24_trailing_slash_is_404_not_redirect(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory()

    by_header = client.get("/hb/", headers=_bearer(location.device_key))
    by_query = client.get("/hb/", {"key": location.device_key})

    assert by_header.status_code == 404
    assert by_query.status_code == 404
    assert "Location" not in by_query
    assert _state(location).status == "waiting"


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "patch", "delete", "head", "options"])
def test_other_methods_405(
    client: Client, location_factory: Callable[..., Any], method: str
) -> None:
    location = location_factory()

    response = getattr(client, method)("/hb", headers=_bearer(location.device_key))

    assert response.status_code == 405
    assert response["Allow"] == "GET, POST"
    # A valid key on another method is not a heartbeat.
    assert _state(location).status == "waiting"


@pytest.mark.django_db
def test_INV24_heartbeat_never_redirects(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    key = location_factory().device_key
    other_key = location_factory().device_key
    requests = [
        ("get", "/hb", _bearer(key)),
        ("post", "/hb", _bearer(key)),
        ("get", f"/hb?key={other_key}", {}),
        ("post", f"/hb?key={other_key}", {}),
        ("get", "/hb", {}),
        ("get", f"/hb?key={generate_device_key()}", {}),
        ("get", f"/hb?key={key[:31]}", {}),
        ("get", "/hb/", _bearer(key)),
        ("get", f"/hb/?key={key}", {}),
        ("get", f"/HB?key={key}", {}),
        ("put", "/hb", _bearer(key)),
        ("head", "/hb", _bearer(key)),
        ("options", "/hb", {}),
    ]

    for method, path, headers in requests:
        response = getattr(client, method)(path, headers=headers)
        assert not 300 <= response.status_code < 400, (method, path, response.status_code)
        assert "Location" not in response, (method, path)


# No outbound I/O, no CSRF, no CSP, no key in logs


@pytest.mark.django_db
def test_INV01_heartbeat_makes_no_outbound_http(
    client: Client, location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    # Nothing is registered: any Telegram call would fail and be recorded.
    location = location_factory()

    first = client.get("/hb", headers=_bearer(location.device_key))
    second = client.post("/hb", headers=_bearer(location.device_key))

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_heartbeat_post_needs_no_csrf_and_has_no_csp(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    csrf_client = Client(enforce_csrf_checks=True)

    accepted = csrf_client.post("/hb", headers=_bearer(location.device_key))
    rejected = csrf_client.post("/hb")

    assert (accepted.status_code, accepted.content) == (200, b"ok")
    assert rejected.status_code == 401
    assert "Content-Security-Policy" not in accepted
    assert "Content-Security-Policy" not in rejected


@pytest.mark.django_db
def test_heartbeat_logs_never_contain_the_key(
    client: Client, location_factory: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    location = location_factory()
    unknown = generate_device_key()
    caplog.set_level(logging.DEBUG)

    accepted = client.get("/hb", {"key": location.device_key})
    rejected = client.get("/hb", {"key": unknown})

    assert (accepted.status_code, rejected.status_code) == (200, 401)
    assert location.device_key not in caplog.text
    assert unknown not in caplog.text
