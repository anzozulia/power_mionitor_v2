"""At DEBUG level no secret reaches any log, column, payload or notice (INV-23 #1, OPS-08).

The sinks a bot token or a device key could reach, each driven at the most verbose level:

- the log output, through a handler with ``RedactingFormatter(FORMAT)`` on the root logger
  while root, ``powermon``, ``django``, ``django.db.backends``, ``urllib3`` and
  ``requests`` are forced to DEBUG (``debug_capture``; the levels are restored after). The
  raw records of our own ``powermon`` loggers must not hold a secret either: they never
  put one into a message or a traceback in the first place;
- the outbox and ops_incident rows (payloads, ``last_error``) after every relay outcome;
- the text of every ops notice kind.

``responses`` patches requests above urllib3, so with ``fake_telegram`` urllib3 never logs
at all (RESEARCH Pitfall 10). The real-urllib3 case therefore runs without it: a plain HTTP
server on 127.0.0.1 (allowed by pytest-socket) answers like Telegram, and urllib3's own
DEBUG line, which carries the ``/bot<token>/`` path, must come out redacted.

The adjacency cases check that a token or key glued to other text is still redacted, and
that short digit:letter pairs below the token shape stay untouched.
"""

import dataclasses
import io
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    FakeClock,
)
from django.db import connection, transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon import logging_setup
from powermon.alerts import ops, ops_texts, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine import lapse
from powermon.locations.keys import generate_device_key
from powermon.logging_setup import RedactingFormatter
from powermon.telegram.client import TelegramClient
from powermon.worker import io_loop
from powermon.worker.lease import Lease

T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
FORBIDDEN_TOKEN = "222222222:" + "F" * 35
REFUSED_TOKEN = "333333333:" + "R" * 35
TIMEOUT_TOKEN = "444444444:" + "T" * 35
EXPIRED_TOKEN = "666666666:" + "E" * 35
TOKENS = (
    DEFAULT_BOT_TOKEN,
    OPS_BOT_TOKEN,
    FORBIDDEN_TOKEN,
    REFUSED_TOKEN,
    TIMEOUT_TOKEN,
    EXPIRED_TOKEN,
)
DEBUG_LOGGERS = ("", "powermon", "django", "django.db.backends", "urllib3", "requests")
SHORT_CODE = re.compile(r"[a-z0-9_]{0,64}")
TELEGRAM_OK = {"ok": True, "result": {"message_id": 1}}


def _secrets(*extra: str) -> list[str]:
    """Every token, every token's secret half, and ``extra`` (device keys)."""
    return [*TOKENS, *(token.split(":", 1)[1] for token in TOKENS), *extra]


def _assert_secret_free(text: str, secrets: list[str]) -> None:
    for secret in secrets:
        assert secret not in text


class _Records(logging.Handler):
    """Keeps every record it sees, unformatted."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclasses.dataclass
class Capture:
    """What the redacting handler wrote, and the raw records behind it."""

    stream: io.StringIO
    raw: _Records

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def raw_text(self, prefix: str) -> str:
        """Message and traceback text of every raw record from loggers under ``prefix``."""
        parts = []
        plain = logging.Formatter()
        for record in self.raw.records:
            if record.name == prefix or record.name.startswith(prefix + "."):
                parts.append(record.getMessage())
                if record.exc_info:
                    parts.append(plain.formatException(record.exc_info))
                if record.exc_text:
                    parts.append(record.exc_text)
        return "\n".join(parts)


@pytest.fixture
def debug_capture() -> Iterator[Capture]:
    """The six loggers at DEBUG and a redacting StringIO handler on root, all undone after."""
    loggers = [logging.getLogger(name) for name in DEBUG_LOGGERS]
    levels = [logger.level for logger in loggers]
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(logging_setup.FORMAT))
    raw = _Records()
    root = logging.getLogger()
    root.addHandler(handler)
    root.addHandler(raw)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    try:
        yield Capture(stream, raw)
    finally:
        root.removeHandler(handler)
        root.removeHandler(raw)
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def _queue_off(location: Any, at: datetime = T0) -> OutboxMessage:
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - timedelta(seconds=91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


def _refused(token: str) -> requests.ConnectionError:
    # A real connect-phase exception carries the URL, and so the token, in its text (P-12).
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


def _read_timeout(token: str) -> requests.ReadTimeout:
    return requests.ReadTimeout(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out. "
        f"(read timeout=10) /bot{token}/sendMessage"
    )


def _status(row: OutboxMessage) -> tuple[str, str]:
    fresh = OutboxMessage.objects.get(pk=row.pk)
    return fresh.status, fresh.last_error


def _no_ops_chat(settings: Any) -> None:
    settings.CFG = dataclasses.replace(settings.CFG, ops_bot_token="", ops_chat_id=None)


# Every log channel at DEBUG (INV-23 #1)


@pytest.mark.django_db(transaction=True)
def test_INV23_debug_logs_never_contain_token_or_key(
    debug_capture: Capture,
    fake_telegram: Any,
    ops_settings: Any,
    location_factory: Callable[..., Any],
    client: Any,
) -> None:
    sent = location_factory()
    forbidden = location_factory(bot_token=FORBIDDEN_TOKEN, chat_id=-1002222222222)
    refused = location_factory(bot_token=REFUSED_TOKEN, chat_id=-1003333333333)
    unknown_key = generate_device_key()

    # Heartbeats: the key in the query string, in a Bearer header, and an unknown key.
    assert client.get("/hb", {"key": sent.device_key}).status_code == 200
    bearer = {"authorization": f"Bearer {forbidden.device_key}"}
    assert client.get("/hb", headers=bearer).status_code == 200
    assert client.get("/hb", {"key": unknown_key}).status_code == 401

    # One relay pass: an alert sent, a 403, a refused connection, and an ops notice.
    ok_row = _queue_off(sent)
    forbidden_row = _queue_off(forbidden)
    refused_row = _queue_off(refused)
    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_GAP,
            payload={
                "start_us": ops.instant_us(T0 - timedelta(minutes=10)),
                "end_us": ops.instant_us(T0),
            },
            recorded_at=T0,
        )
    [ops_row] = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    forbidden_body = {"ok": False, "error_code": 403, "description": "Forbidden"}
    fake_telegram.fail(FORBIDDEN_TOKEN, status=403, json_body=forbidden_body)
    fake_telegram.fail(REFUSED_TOKEN, exc=_refused(REFUSED_TOKEN))
    fake_telegram.accept(OPS_BOT_TOKEN)
    assert io_loop.run_iteration(FakeClock(T0 + timedelta(minutes=1)), io_loop.RelayState())
    assert _status(ok_row) == ("sent", "")
    assert _status(forbidden_row) == ("pending", "http_403")
    assert _status(refused_row) == ("pending", "connect_error")
    assert _status(ops_row)[0] == "sent"

    # An ops notice with no ops chat goes to the log at WARNING, with its text.
    _no_ops_chat(ops_settings)
    with transaction.atomic():
        ops.notify(
            outbox.KIND_OPS_UNCERTAIN,
            payload={"message_id": ok_row.pk},
            recorded_at=T0,
            location_id=sent.pk,
        )

    # The lease cannot reach the database.
    down = Lease({**connection.settings_dict, "HOST": "127.0.0.1", "PORT": 1})
    try:
        assert down.ensure_held().state == "db_down"
    finally:
        down.close()

    text = debug_capture.text
    # Every sink above wrote through the handler.
    for line in (
        f"heartbeat: location {sent.pk} started",
        "heartbeat rejected",
        "telegram sendMessage: permanent (http_403)",
        "telegram sendMessage: not_sent (connect_error)",
        "ops notice (ops chat not configured): ❓ OFF alert for Test location",
        "worker lock: database unreachable (OperationalError); retrying",
    ):
        assert line in text
    keys = [sent.device_key, forbidden.device_key, refused.device_key, unknown_key]
    _assert_secret_free(text, _secrets(*keys))
    # Our own loggers never put a secret into a message or a traceback.
    _assert_secret_free(debug_capture.raw_text("powermon"), _secrets(*keys))


class _TelegramStub(BaseHTTPRequestHandler):
    """Answers every POST like Telegram's sendMessage: ok, message 1."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps(TELEGRAM_OK).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # The server's own access line would print the request path, token included.
        return


@pytest.fixture
def telegram_stub() -> Iterator[str]:
    """A plain HTTP Telegram stand-in on 127.0.0.1; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TelegramStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_INV23_real_urllib3_debug_line_is_redacted(
    debug_capture: Capture, telegram_stub: str
) -> None:
    client = TelegramClient(DEFAULT_BOT_TOKEN, api_base=telegram_stub)

    assert client.send_message(DEFAULT_CHAT_ID, "x").kind == "ok"

    # urllib3 really logged the request line, with the token in its path.
    raw = [r.getMessage() for r in debug_capture.raw.records if r.name.startswith("urllib3")]
    assert any(f"/bot{DEFAULT_BOT_TOKEN}/sendMessage" in line for line in raw)
    # The redacting handler wrote it without the token.
    text = debug_capture.text
    assert '"POST /[REDACTED-TOKEN]/sendMessage HTTP/1.1" 200' in text
    _assert_secret_free(text, _secrets())


# DB columns and payloads (INV-23 #1, D-16: last_error stores short codes only)


@pytest.mark.django_db(transaction=True)
def test_INV23_db_columns_and_payloads_secret_free(
    fake_telegram: Any, ops_settings: Any, location_factory: Callable[..., Any]
) -> None:
    now = T0 + timedelta(minutes=1)
    sent = location_factory()
    forbidden = location_factory(bot_token=FORBIDDEN_TOKEN, chat_id=-1002222222222)
    refused = location_factory(bot_token=REFUSED_TOKEN, chat_id=-1003333333333)
    timed_out = location_factory(bot_token=TIMEOUT_TOKEN, chat_id=-1004444444444)
    expired = location_factory(bot_token=EXPIRED_TOKEN, chat_id=-1006666666666)
    rows = {
        "ok": _queue_off(sent),
        "403": _queue_off(forbidden),
        "refused": _queue_off(refused),
        "read_timeout": _queue_off(timed_out),
        # Recorded 7 h ago: past the 6 h maximum age, so it expires before any send.
        "expired": _queue_off(expired, at=now - timedelta(hours=7)),
    }
    # A monitoring gap: one ops_incident row and one gap notice.
    assert lapse.read_cursor() is None
    assert lapse.start_fresh(T0 - timedelta(minutes=10))
    assert lapse.carve_if_needed(T0, force=False) is not None
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    forbidden_body = {"ok": False, "error_code": 403, "description": "Forbidden"}
    fake_telegram.fail(FORBIDDEN_TOKEN, status=403, json_body=forbidden_body)
    fake_telegram.fail(REFUSED_TOKEN, exc=_refused(REFUSED_TOKEN))
    fake_telegram.fail(TIMEOUT_TOKEN, exc=_read_timeout(TIMEOUT_TOKEN))
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()

    # One ops notice per pass: the gap, the expired alert, the uncertain one.
    for _ in range(4):
        io_loop.run_iteration(FakeClock(now), state)

    assert {name: _status(row)[0] for name, row in rows.items()} == {
        "ok": "sent",
        "403": "pending",
        "refused": "pending",
        "read_timeout": "uncertain",
        "expired": "expired",
    }
    ops_rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS)
    assert sorted(ops_rows.values_list("kind", "status")) == [
        (outbox.KIND_OPS_EXPIRED, "sent"),
        (outbox.KIND_OPS_GAP, "sent"),
        (outbox.KIND_OPS_UNCERTAIN, "sent"),
    ]
    assert OpsIncident.objects.count() == 1
    keys = [loc.device_key for loc in (sent, forbidden, refused, timed_out, expired)]
    stored = repr(list(OutboxMessage.objects.values())) + repr(list(OpsIncident.objects.values()))
    _assert_secret_free(stored, _secrets(*keys))
    errors = list(OutboxMessage.objects.values_list("last_error", flat=True))
    assert {"http_403", "connect_error", "read_timeout"} <= set(errors)
    assert [e for e in errors if not SHORT_CODE.fullmatch(e)] == []


# Ops notice texts (D-10, D-11): names and times only


@pytest.mark.django_db
def test_INV23_ops_notice_texts_secret_free(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office <main>")
    alert = _queue_off(location)
    since_us = ops.instant_us(T0 - timedelta(minutes=10))
    notices = {
        outbox.KIND_OPS_GAP: ({"start_us": since_us, "end_us": ops.instant_us(T0)}, None),
        outbox.KIND_OPS_ALL_SILENT_START: ({"since_us": since_us, "count": 3}, None),
        outbox.KIND_OPS_ALL_SILENT_END: (
            {"since_us": since_us, "first_us": ops.instant_us(T0)},
            location.pk,
        ),
        outbox.KIND_OPS_EXPIRED: ({"message_id": alert.pk}, location.pk),
        outbox.KIND_OPS_UNCERTAIN: ({"message_id": alert.pk}, location.pk),
    }
    assert set(notices) == set(outbox.OPS_KINDS)
    texts = [
        ops.render_text(kind, payload, location_id, now=T0, escape=escape)
        for kind, (payload, location_id) in notices.items()
        for escape in (True, False)
    ]
    texts.append(ops_texts.db_down(T0 - timedelta(minutes=6), T0, "Europe/Kyiv"))

    # Telegram HTML escapes the name; the plain text for a log line keeps it.
    assert any("Office &lt;main&gt;" in text for text in texts)
    assert any("Office <main>" in text for text in texts)
    for text in texts:
        _assert_secret_free(text, _secrets(location.device_key, str(DEFAULT_CHAT_ID)))
        assert str(abs(DEFAULT_CHAT_ID)) not in text


# Redaction adjacency (OPS-08 edge)


def _format(message: str) -> str:
    record = logging.LogRecord("powermon.test", logging.INFO, __file__, 1, message, None, None)
    return RedactingFormatter(logging_setup.FORMAT).format(record)


def test_redaction_catches_glued_tokens_and_keys() -> None:
    key = generate_device_key()
    token = DEFAULT_BOT_TOKEN

    assert _format(f"POST /bot{token}/sendMessage").endswith("POST /[REDACTED-TOKEN]/sendMessage")
    assert _format(f"failed ({token})").endswith("failed ([REDACTED-TOKEN])")
    assert _format(f"token {token}.").endswith("token [REDACTED-TOKEN].")
    assert _format(f"GET /hb?key={key}&x=1").endswith("GET /hb?key=[REDACTED]&x=1")
    assert _format(f"GET /hb?x=1&key={key}").endswith("GET /hb?x=1&key=[REDACTED]")
    for text in (_format(f"POST /bot{token}/sendMessage"), _format(f"?key={key}")):
        assert token not in text
        assert token.split(":", 1)[1] not in text
        assert key not in text
    # Below the token shape (5+ digits, colon, 30+ characters): left alone.
    assert _format("at 12:34 and 123:abc").endswith("at 12:34 and 123:abc")
    assert _format("short 12345:abcdefghij").endswith("short 12345:abcdefghij")
