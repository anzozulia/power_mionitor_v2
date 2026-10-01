"""The device heartbeat endpoint /hb (HB-01, HB-02, MON-01; INV-24 #1-#2, INV-01 #3, K-1).

Time-dependent tests serve HeartbeatView through RequestFactory with an injected FakeClock.
Routing, status-code and middleware tests go through the Django test client, so the
whole middleware stack (CSRF, login-required, CSP, slash handling) is part of the check.

While the database fails, /hb answers 503 ``db unavailable``; the log gets one WARNING per
outage per process (class name only) and one line when the database answers again, not a
traceback or a django.request line per heartbeat (D-16, OPS-08). Only errors that mean the
database cannot be reached count as an outage: OperationalError (statement_timeout
included), InterfaceError, and the ProgrammingError psycopg raises on a connection that
already died. Any other database error (IntegrityError, DataError, a genuine
ProgrammingError) is a bug: Django answers 500 and logs the traceback, and the outage flag
is left alone (wave 1 audit A1).
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import FakeClock
from django.db import (
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    connection,
    transaction,
)
from django.http import HttpResponse
from django.test import Client, RequestFactory

from powermon.engine import transitions
from powermon.engine.models import LocationState
from powermon.locations.keys import generate_device_key
from powermon.locations.models import Location
from powermon.web import views

CHALLENGE = 'Bearer realm="heartbeat"'
DB_DOWN = "heartbeat: database unavailable ({}); answering 503 until it is back"
DB_BACK = "heartbeat: database reachable again"


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


def _assert_db_unavailable(response: Any) -> None:
    assert response.status_code == 503
    assert response.content == b"db unavailable"
    assert response["Content-Type"] == "text/plain"


def _db_down(*args: Any, **kwargs: Any) -> Any:
    # A psycopg message can carry the host, the user and more; none of it may be logged.
    raise OperationalError("connection to server at db failed: password=hunter2")


def _integrity_bug(*args: Any, **kwargs: Any) -> Any:
    # The database answered: the code asked for something the schema refuses.
    raise IntegrityError('duplicate key value violates unique constraint "power_interval_no"')


def _lenient_client() -> Client:
    """A test client that returns Django's 500 response instead of raising the exception."""
    return Client(raise_request_exception=False)


def _view_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == views.__name__ and r.levelno >= logging.WARNING
    ]


def _request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "django.request"]


@pytest.fixture
def outage_log(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh per-process outage flag, so no test sees another test's outage."""
    fresh = views._DbOutageLog()
    monkeypatch.setattr(views, "_HEARTBEAT_DB", fresh)
    return fresh


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
    location = location_factory()

    # The waiting -> on gate changes 1 row once; a repeat matches 0 rows there.
    assert transitions.record_heartbeat(location.pk, fixed_now) == "started"
    assert transitions.record_heartbeat(location.pk, fixed_now) == "plain"
    assert transitions.record_heartbeat(location.pk, fixed_now + timedelta(seconds=5)) == "plain"

    state = _state(location)
    assert state.status == "on"
    assert state.on_since == fixed_now
    assert state.last_heartbeat_at == fixed_now + timedelta(seconds=5)
    assert state.state_version == 3


@pytest.mark.django_db
def test_record_heartbeat_ignores_a_location_without_state(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory()
    before = _all_states()

    assert transitions.record_heartbeat(location.pk + 1000, fixed_now) == "ignored"

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


# Database outage: 503 and one log line per outage (D-16, OPS-08, T-02-07, T-02-08)


@pytest.mark.django_db
def test_heartbeat_answers_503_while_the_database_fails(
    client: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location = location_factory()
    monkeypatch.setattr(transitions, "record_heartbeat", _db_down)
    caplog.set_level(logging.DEBUG)

    first = client.get("/hb", headers=_bearer(location.device_key))
    second = client.post(f"/hb?key={location.device_key}")

    _assert_db_unavailable(first)
    _assert_db_unavailable(second)
    assert _view_warnings(caplog) == [DB_DOWN.format("OperationalError")]
    # No "Service Unavailable: /hb" line per heartbeat, and no traceback.
    assert _request_records(caplog) == []
    assert "Traceback" not in caplog.text
    assert "hunter2" not in caplog.text
    assert location.device_key not in caplog.text


@pytest.mark.django_db
def test_heartbeat_logs_once_when_the_database_is_back(
    client: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location = location_factory()
    real = transitions.record_heartbeat
    monkeypatch.setattr(transitions, "record_heartbeat", _db_down)
    caplog.set_level(logging.DEBUG)
    for _ in range(2):
        _assert_db_unavailable(client.get("/hb", headers=_bearer(location.device_key)))
    monkeypatch.setattr(transitions, "record_heartbeat", real)
    caplog.clear()

    back = client.get("/hb", headers=_bearer(location.device_key))
    back_warnings = _view_warnings(caplog)
    caplog.clear()
    again = client.get("/hb", headers=_bearer(location.device_key))

    assert (back.status_code, back.content) == (200, b"ok")
    assert (again.status_code, again.content) == (200, b"ok")
    assert back_warnings == [DB_BACK]
    assert _view_warnings(caplog) == []
    assert _state(location).status == "on"


@pytest.mark.django_db
def test_heartbeat_warns_again_on_a_second_outage(
    client: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location = location_factory()
    real = transitions.record_heartbeat
    caplog.set_level(logging.DEBUG)

    for record in (_db_down, real, _db_down, _db_down):
        monkeypatch.setattr(transitions, "record_heartbeat", record)
        client.get("/hb", headers=_bearer(location.device_key))

    assert _view_warnings(caplog) == [
        DB_DOWN.format("OperationalError"),
        DB_BACK,
        DB_DOWN.format("OperationalError"),
    ]


@pytest.mark.django_db
def test_heartbeat_503_when_the_key_lookup_fails(
    client: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location = location_factory()

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        # Django raises a refused connection as OperationalError.
        raise OperationalError("could not connect to server: password=hunter2")

    monkeypatch.setattr(Location.objects, "filter", unreachable)
    caplog.set_level(logging.DEBUG)

    by_header = client.get("/hb", headers=_bearer(location.device_key))
    by_query = client.get("/hb", {"key": location.device_key})

    _assert_db_unavailable(by_header)
    _assert_db_unavailable(by_query)
    assert _view_warnings(caplog) == [DB_DOWN.format("OperationalError")]
    assert _request_records(caplog) == []
    assert "hunter2" not in caplog.text


@pytest.mark.django_db
def test_rejected_keys_keep_their_401_while_the_database_fails(
    client: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location_factory()
    monkeypatch.setattr(transitions, "record_heartbeat", _db_down)
    caplog.set_level(logging.DEBUG)

    # A malformed key costs no query, so it never meets the outage.
    response = client.get("/hb", {"key": "too-short"})

    _assert_unauthorized(response)
    assert _view_warnings(caplog) == []


# Which errors count as an outage (wave 1 audit A1): only "the database cannot be reached"


@pytest.mark.django_db
def test_heartbeat_503_on_interface_error(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    # InterfaceError (a connection the driver can no longer use) is not a DatabaseError
    # subclass in Django, but it is an outage all the same.
    location = location_factory()

    def unusable(*args: Any, **kwargs: Any) -> Any:
        raise InterfaceError("connection already closed")

    monkeypatch.setattr(transitions, "record_heartbeat", unusable)
    caplog.set_level(logging.DEBUG)

    response = _lenient_client().get("/hb", headers=_bearer(location.device_key))

    _assert_db_unavailable(response)
    assert _view_warnings(caplog) == [DB_DOWN.format("InterfaceError")]
    assert _request_records(caplog) == []


@pytest.mark.django_db
def test_heartbeat_503_when_the_statement_times_out(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    # A real statement_timeout (QueryCanceled) is an OperationalError: a database too slow
    # to answer is an outage for the device, not a bug.
    location = location_factory()

    def too_slow(location_id: int, now: datetime) -> str:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 1")
            cur.execute("SELECT pg_sleep(1)")
        return "plain"

    monkeypatch.setattr(transitions, "record_heartbeat", too_slow)
    caplog.set_level(logging.DEBUG)

    response = _lenient_client().get("/hb", headers=_bearer(location.device_key))

    _assert_db_unavailable(response)
    assert _view_warnings(caplog) == [DB_DOWN.format("OperationalError")]


@pytest.mark.django_db(transaction=True)
def test_heartbeat_503_when_atomic_meets_a_dead_connection(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    # When the session has died under Django, psycopg refuses atomic()'s autocommit switch
    # with a ProgrammingError, not an OperationalError. With the connection gone, that is
    # an outage.
    location = location_factory()

    def dead_session(location_id: int, now: datetime) -> str:
        connection.connection.close()
        raise ProgrammingError(
            "can't change 'autocommit' now: connection in transaction status UNKNOWN"
        )

    monkeypatch.setattr(transitions, "record_heartbeat", dead_session)
    caplog.set_level(logging.DEBUG)

    try:
        response = _lenient_client().get("/hb", headers=_bearer(location.device_key))
    finally:
        # Drop the dead connection, so the test teardown opens a fresh one.
        connection.close()

    _assert_db_unavailable(response)
    assert _view_warnings(caplog) == [DB_DOWN.format("ProgrammingError")]
    assert _request_records(caplog) == []


@pytest.mark.django_db
def test_heartbeat_integrity_error_is_a_bug_not_an_outage(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location = location_factory()
    monkeypatch.setattr(transitions, "record_heartbeat", _integrity_bug)
    caplog.set_level(logging.DEBUG)
    client = _lenient_client()

    response = client.get("/hb", {"key": location.device_key})

    assert response.status_code == 500
    assert _view_warnings(caplog) == []
    assert "database unavailable" not in caplog.text
    # Django logs the bug once, at ERROR, with its traceback (redacted by the formatter).
    [record] = _request_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[0] is IntegrityError
    assert location.device_key not in caplog.text

    # The outage flag was not set: the next real outage still gets its one WARNING.
    monkeypatch.setattr(transitions, "record_heartbeat", _db_down)
    _assert_db_unavailable(client.get("/hb", headers=_bearer(location.device_key)))
    assert _view_warnings(caplog) == [DB_DOWN.format("OperationalError")]


@pytest.mark.django_db
def test_heartbeat_programming_error_on_a_live_connection_is_a_bug(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    # A real ProgrammingError from broken SQL, on a connection that still works.
    location = location_factory()

    def broken_sql(location_id: int, now: datetime) -> str:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("SELECT * FROM no_such_table")
        return "plain"

    monkeypatch.setattr(transitions, "record_heartbeat", broken_sql)
    caplog.set_level(logging.DEBUG)

    response = _lenient_client().get("/hb", headers=_bearer(location.device_key))

    assert response.status_code == 500
    assert _view_warnings(caplog) == []
    [record] = _request_records(caplog)
    assert record.exc_info is not None
    assert record.exc_info[0] is ProgrammingError


@pytest.mark.django_db
def test_heartbeat_bug_neither_ends_nor_restarts_an_outage(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    location = location_factory()
    client = _lenient_client()
    caplog.set_level(logging.DEBUG)

    codes = []
    for record in (_db_down, _integrity_bug, _db_down):
        monkeypatch.setattr(transitions, "record_heartbeat", record)
        codes.append(client.get("/hb", headers=_bearer(location.device_key)).status_code)

    assert codes == [503, 500, 503]
    # Still one outage: the bug in between logged no "reachable again" and no second start.
    assert _view_warnings(caplog) == [DB_DOWN.format("OperationalError")]


@pytest.mark.django_db
def test_heartbeat_failing_location_next_to_a_healthy_one_logs_no_outage(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage_log: Any,
) -> None:
    # Regression: one location whose heartbeat always hits a bug, beating in turn with a
    # healthy one, once wrote "unavailable" and "reachable again" for every failing beat.
    failing = location_factory(name="Failing location")
    healthy = location_factory(name="Healthy location")
    real = transitions.record_heartbeat

    def record(location_id: int, now: datetime) -> str:
        if location_id == failing.pk:
            _integrity_bug()
        return real(location_id, now)

    monkeypatch.setattr(transitions, "record_heartbeat", record)
    client = _lenient_client()
    caplog.set_level(logging.DEBUG)

    codes = [
        client.get("/hb", headers=_bearer(location.device_key)).status_code
        for _ in range(5)
        for location in (failing, healthy)
    ]

    assert codes == [500, 200] * 5
    assert _view_warnings(caplog) == []
    # Each failing beat is one ERROR with its traceback, never a WARNING pair.
    errors = _request_records(caplog)
    assert [r.levelno for r in errors] == [logging.ERROR] * 5
    assert all(r.exc_info is not None and r.exc_info[0] is IntegrityError for r in errors)
    assert _state(healthy).status == "on"
    assert _state(failing).status == "waiting"
