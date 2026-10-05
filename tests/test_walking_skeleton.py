"""Walking-skeleton stack tests: /healthz, CSP, the clock, redacting logs, the test harness,
and the whole alert path in process (DoD 1: heartbeat -> OFF -> ON -> Telegram).
"""

import logging
import socket
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, TELEGRAM_API, FakeClock
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import RequestFactory
from pytest_socket import SocketConnectBlockedError

from powermon.alerts.models import OutboxMessage
from powermon.clock import SystemClock
from powermon.engine.models import SystemState
from powermon.logging_setup import RedactingFormatter
from powermon.web import views
from powermon.web.views import HeartbeatView
from powermon.worker.detection import run_cycle
from powermon.worker.io_loop import RelayState, run_iteration

# 06-UI-SPEC security-bound rule R5 (brief §8), written out so a changed constant fails here.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
# A device key has the D-07 shape: 32 characters from [A-Za-z0-9].
DEVICE_KEY = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"
SEND_URL = f"{TELEGRAM_API}/bot{DEFAULT_BOT_TOKEN}/sendMessage"


# /healthz


@pytest.mark.django_db
def test_healthz_answers_ok_from_postgres(client: Any, django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(1):
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.content == b"ok"
    assert response["Content-Type"] == "text/plain"


def test_healthz_rejects_post(client: Any) -> None:
    response = client.post("/healthz")

    assert response.status_code == 405


class _FailingConnection:
    """Stands in for django.db.connection: opening a cursor fails like a lost database."""

    def cursor(self) -> Any:
        raise DatabaseError("connection to server at db failed: password=hunter2")


def test_healthz_returns_503_when_the_db_query_fails(
    client: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(views, "connection", _FailingConnection())

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.content == b"db unavailable"
    view_messages = [r.getMessage() for r in caplog.records if r.name == views.__name__]
    assert view_messages == ["healthz: database query failed"]
    # The driver's error text (it can carry connection details) is never logged.
    assert "hunter2" not in caplog.text


# Content-Security-Policy


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/x", CSP), ("/hb", None), ("/hb/", CSP), ("/hbx", CSP)],
    ids=["page", "heartbeat", "heartbeat-slash-lookalike", "heartbeat-prefix-lookalike"],
)
def test_csp_header_on_pages_but_not_on_heartbeat_path(
    client: Any, path: str, expected: str | None
) -> None:
    # Only the exact /hb path is exempt; lookalike paths are ordinary pages.
    response = client.get(path)

    assert response.get("Content-Security-Policy") == expected


def test_csp_covers_responses_that_other_middleware_build(client: Any) -> None:
    # CommonMiddleware rejects the Host header before any view runs.
    response = client.get("/x", headers={"host": "attacker.example"})

    assert response.status_code == 400
    assert response.get("Content-Security-Policy") == CSP


# Clock


def test_system_clock_is_aware_utc() -> None:
    clock = SystemClock()

    assert clock.now().tzinfo is UTC
    first = clock.monotonic()
    assert clock.monotonic() >= first


def test_fake_clock_refuses_naive_datetimes(fixed_now: datetime) -> None:
    with pytest.raises(ValueError, match="aware"):
        FakeClock(datetime(2026, 10, 1, 8, 0))  # noqa: DTZ001
    clock = FakeClock(fixed_now)
    with pytest.raises(ValueError, match="aware"):
        clock.set(datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001

    clock.advance(seconds=90)

    assert clock.now() == fixed_now + timedelta(seconds=90)
    assert clock.now().tzinfo is UTC
    assert clock.monotonic() == 90.0


# Logging (T-01-09)


def test_logging_routes_through_redacting_formatter(settings: Any) -> None:
    config = settings.LOGGING
    handler = config.get("handlers", {}).get("stdout", {})
    assert handler.get("stream") == "ext://sys.stdout"
    formatter = config["formatters"][handler["formatter"]]
    assert formatter["()"] == "powermon.logging_setup.RedactingFormatter"
    assert config["loggers"]["urllib3"]["level"] == "WARNING"
    assert config["loggers"]["django.db.backends"]["level"] == "WARNING"
    # Django's logging setup has run: the live stdout handler redacts, and Django's own
    # console and mail handlers are gone, so every record reaches stdout redacted.
    live = [h for h in logging.getLogger().handlers if h.get_name() == "stdout"]
    assert len(live) == 1
    assert isinstance(live[0].formatter, RedactingFormatter)
    assert logging.getLogger("django").handlers == []
    # DEBUG lines carrying token URLs (urllib3) or SQL parameters such as device keys
    # (django.db.backends) are never emitted.
    assert not logging.getLogger("urllib3").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("django.db.backends").isEnabledFor(logging.DEBUG)


def _format(msg: str, *args: object, exc_info: Any = None) -> str:
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)
    return RedactingFormatter("%(levelname)s %(message)s").format(record)


def test_redacting_formatter_scrubs_token_key_and_bearer_value() -> None:
    text = _format(
        "POST %s; GET /hb?key=%s&x=1; Authorization: Bearer %s; authorization: bearer %s",
        SEND_URL,
        DEVICE_KEY,
        DEVICE_KEY,
        DEVICE_KEY,
    )

    assert DEFAULT_BOT_TOKEN not in text
    assert DEVICE_KEY not in text
    assert f"{TELEGRAM_API}/[REDACTED-TOKEN]/sendMessage" in text
    assert "/hb?key=[REDACTED]&x=1" in text
    assert "Authorization: Bearer [REDACTED]" in text
    # The auth scheme name is case-insensitive.
    assert "authorization: bearer [REDACTED]" in text


def test_redacting_formatter_scrubs_secrets_inside_tracebacks() -> None:
    try:
        raise ConnectionError(f"Max retries exceeded with url: /bot{DEFAULT_BOT_TOKEN}/getMe")
    except ConnectionError:
        text = _format("send failed", exc_info=sys.exc_info())

    assert "Traceback" in text
    assert DEFAULT_BOT_TOKEN not in text
    assert "/[REDACTED-TOKEN]/getMe" in text


def test_redacting_formatter_leaves_ordinary_text_alone() -> None:
    # Near misses: a short "digits:text" pair and a parameter whose name only starts with key.
    text = "location 12345 period 60 s; ratio 12345:678; key-rotation=done; status=on"

    assert _format(text) == f"ERROR {text}"


# Test harness: fake Telegram and the network guard


def test_fake_telegram_records_accepted_messages(fake_telegram: Any) -> None:
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    response = requests.post(SEND_URL, json={"chat_id": -1, "text": "hi"}, timeout=(3, 5))

    assert response.json() == {"ok": True, "result": {"message_id": 1}}
    assert fake_telegram.sent == [{"chat_id": -1, "text": "hi"}]
    assert len(fake_telegram.calls) == 1


def test_fake_telegram_replays_registered_failures(fake_telegram: Any) -> None:
    rate_limited = {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
    fake_telegram.fail(DEFAULT_BOT_TOKEN, status=429, json_body=rate_limited)
    other_token = "987654321:" + "B" * 35
    fake_telegram.fail(other_token, exc=requests.ReadTimeout("read timed out"))

    response = requests.post(SEND_URL, json={"chat_id": -1, "text": "hi"}, timeout=(3, 5))

    assert response.status_code == 429
    assert response.json() == rate_limited
    with pytest.raises(requests.ReadTimeout):
        requests.post(
            f"{TELEGRAM_API}/bot{other_token}/sendMessage", json={"chat_id": -1}, timeout=(3, 5)
        )
    assert fake_telegram.sent == []


def test_fake_telegram_refuses_unregistered_bots(fake_telegram: Any) -> None:
    with pytest.raises(requests.ConnectionError):
        requests.post(SEND_URL, json={"chat_id": -1, "text": "hi"}, timeout=(3, 5))


# pytest-socket also warns when it blocks; the raise is what this test checks.
@pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")
def test_network_guard_blocks_real_outbound_connections() -> None:
    # 192.0.2.1 is TEST-NET-1: even without the guard it would never answer.
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


# Display time zone


def test_display_tz_is_canonical(settings: Any) -> None:
    assert settings.TIME_ZONE == "Europe/Kyiv"
    assert ZoneInfo(settings.TIME_ZONE).key == "Europe/Kyiv"


# The whole alert path in process (DoD 1 in-process; ALRT-01, ALRT-02, MON-02, MON-03)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


@pytest.mark.django_db(transaction=True)
def test_walking_skeleton_off_and_on_alerts_reach_telegram(
    location_factory: Any, fake_telegram: Any, rf: RequestFactory
) -> None:
    # The worker started at 09:00, long before the device's first heartbeat.
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": None}
    )
    location = location_factory(language="uk")
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    def beat(at: datetime) -> HttpResponse:
        request = rf.get("/hb", headers={"authorization": f"Bearer {location.device_key}"})
        response: HttpResponse = HeartbeatView.as_view(clock=FakeClock(at))(request)
        return response

    for minute in range(6):  # 10:00:00 ... 10:05:00, then the power goes
        response = beat(_at(10, minute))
        assert (response.status_code, response.content) == (200, b"ok")
    state = RelayState()

    # 10:06:31 is past period + grace (90 s) after the last heartbeat: OFF at 10:05:00.
    assert run_cycle(_at(10, 6, 31)) == 1
    assert run_iteration(FakeClock(_at(10, 6, 32)), state) is True

    off = {
        "chat_id": -1001234567890,
        "text": "🔴 <b>СВІТЛО ЗНИКЛО</b>\n⚡ Світло було: <b>5 хв</b>",
        "parse_mode": "HTML",
    }
    assert fake_telegram.sent == [off]

    # Power is back: the first heartbeat restores the location at 11:00:00.
    response = beat(_at(11, 0))
    assert (response.status_code, response.content) == (200, b"ok")
    assert run_iteration(FakeClock(_at(11, 0, 1)), state) is True

    on = {
        "chat_id": DEFAULT_CHAT_ID,
        "text": "🟢 <b>СВІТЛО ПОВЕРНУЛОСЯ</b>\n⚡ Світла не було: <b>55 хв</b>",
        "parse_mode": "HTML",
    }
    assert fake_telegram.sent == [off, on]
    assert len(fake_telegram.calls) == 2
    rows = OutboxMessage.objects.order_by("id").values_list("kind", "status")
    assert list(rows) == [("power_off", "sent"), ("power_on", "sent")]
    # Nothing else is due: no third message.
    assert run_cycle(_at(11, 0, 2)) == 0
    assert run_iteration(FakeClock(_at(11, 0, 3)), state) is False
