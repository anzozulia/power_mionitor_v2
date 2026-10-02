"""TelegramClient: one sendMessage call, every outcome classified, no token leak.

Telegram is faked at the HTTP boundary (``fake_telegram``, built on ``responses``).
"Not sent" may be retried; "maybe delivered" is never resent (INV-16, STACK G2). A result
code is a short fixed string and never carries the token or a URL (INV-23, STACK G3).
"""

import ast
import logging
import pathlib
import re
import socket
from typing import Any

import pytest
import requests
import responses
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, TELEGRAM_API
from urllib3.exceptions import (
    MaxRetryError,
    NameResolutionError,
    NewConnectionError,
    ProtocolError,
    SSLError,
)

from powermon.telegram.client import (
    SendResult,
    TelegramClient,
    _HandshakeError,
    _HandshakeTimeout,
)

TOKEN = DEFAULT_BOT_TOKEN
SEND_PATH = f"/bot{TOKEN}/sendMessage"
SEND_URL = f"{TELEGRAM_API}{SEND_PATH}"
TEXT = "🔴 <b>СВІТЛО ЗНИКЛО</b>\n⚡ Світло було: <b>5 год 12 хв</b>"
CLIENT_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "powermon/telegram/client.py"


def _send(**kwargs: Any) -> SendResult:
    return TelegramClient(TOKEN, **kwargs).send_message(DEFAULT_CHAT_ID, TEXT)


# Real connect-phase exceptions carry the URL, and so the token, in their text (P-12).
def _refused() -> requests.ConnectionError:
    reason = NewConnectionError(None, f"Failed to establish a new connection for {SEND_PATH}")
    return requests.ConnectionError(MaxRetryError(None, SEND_PATH, reason))


def _dns_failure() -> requests.ConnectionError:
    gai = socket.gaierror(-2, "Name or service not known")
    reason = NameResolutionError("api.telegram.org", None, gai)
    return requests.ConnectionError(MaxRetryError(None, SEND_PATH, reason))


def _dropped() -> requests.ConnectionError:
    return requests.ConnectionError(
        ProtocolError("Connection aborted.", ConnectionResetError(104, "reset"))
    )


# What the client's own HTTPS connection raises when the TLS handshake fails (audit F1).
def _handshake(error: type[NewConnectionError]) -> requests.ConnectionError:
    reason = error(None, f"TLS handshake for {SEND_PATH}")
    return requests.ConnectionError(MaxRetryError(None, SEND_PATH, reason))


# A TLS error once the handshake is done: the request may have been written.
def _ssl_after_handshake() -> requests.ConnectionError:
    reason = SSLError(f"decryption failed for {SEND_PATH}")
    return requests.exceptions.SSLError(MaxRetryError(None, SEND_PATH, reason))


# (id, FakeTelegram.fail kwargs, expected kind, expected code)
FAILURES: list[tuple[str, dict[str, Any], str, str]] = [
    (
        "429",
        {
            "status": 429,
            "json_body": {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 30",
                "parameters": {"retry_after": 30},
            },
        },
        "rate_limited",
        "429",
    ),
    ("502", {"status": 502}, "transient", "http_502"),
    (
        "400",
        {"status": 400, "json_body": {"ok": False, "error_code": 400}},
        "permanent",
        "http_400",
    ),
    (
        "401",
        {"status": 401, "json_body": {"ok": False, "error_code": 401}},
        "permanent",
        "http_401",
    ),
    (
        "403",
        {"status": 403, "json_body": {"ok": False, "error_code": 403}},
        "permanent",
        "http_403",
    ),
    (
        "404",
        {"status": 404, "json_body": {"ok": False, "error_code": 404}},
        "permanent",
        "http_404",
    ),
    (
        "connect_timeout",
        {"exc": requests.ConnectTimeout(f"timed out: {SEND_URL}")},
        "not_sent",
        "connect_timeout",
    ),
    ("refused", {"exc": _refused()}, "not_sent", "connect_error"),
    ("dns", {"exc": _dns_failure()}, "not_sent", "connect_error"),
    (
        "tls_handshake_timeout",
        {"exc": _handshake(_HandshakeTimeout)},
        "not_sent",
        "tls_handshake_timeout",
    ),
    (
        "tls_handshake_error",
        {"exc": _handshake(_HandshakeError)},
        "not_sent",
        "tls_handshake_error",
    ),
    (
        "ssl_after_handshake",
        {"exc": _ssl_after_handshake()},
        "maybe_delivered",
        "connection_dropped",
    ),
    (
        "read_timeout",
        {"exc": requests.ReadTimeout(f"read timed out: {SEND_URL}")},
        "maybe_delivered",
        "read_timeout",
    ),
    ("dropped", {"exc": _dropped()}, "maybe_delivered", "connection_dropped"),
    (
        "other_request_error",
        {"exc": requests.exceptions.ChunkedEncodingError(f"broken: {SEND_URL}")},
        "maybe_delivered",
        "request_error",
    ),
]


def test_ok_response(fake_telegram: Any) -> None:
    fake_telegram.accept(TOKEN)

    result = _send()

    assert result == SendResult("ok")
    assert fake_telegram.sent == [{"chat_id": DEFAULT_CHAT_ID, "text": TEXT, "parse_mode": "HTML"}]
    assert fake_telegram.calls[0].request.url == SEND_URL


def test_every_call_passes_the_short_timeouts(fake_telegram: Any) -> None:
    fake_telegram.accept(TOKEN)

    _send()

    assert fake_telegram.calls[0].request.req_kwargs["timeout"] == (5.0, 10.0)


def test_api_base_is_configurable() -> None:
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, f"https://tg.example.test{SEND_PATH}", json={"ok": True})

        assert _send(api_base="https://tg.example.test") == SendResult("ok")


def test_rate_limited_uses_retry_after(fake_telegram: Any) -> None:
    _, kwargs, _, _ = FAILURES[0]
    fake_telegram.fail(TOKEN, **kwargs)

    assert _send() == SendResult("rate_limited", retry_after=30, code="429")


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        (None, 30),
        ({}, 30),
        ({"retry_after": "abc"}, 30),
        ({"retry_after": 0}, 30),
        ({"retry_after": -5}, 30),
        ({"retry_after": True}, 30),
        ({"retry_after": 1.5}, 30),
        ({"retry_after": "12"}, 12),
        ({"retry_after": 7}, 7),
        ("not-a-dict", 30),
    ],
)
def test_retry_after_missing_or_bad_defaults_to_30(
    fake_telegram: Any, parameters: Any, expected: int
) -> None:
    body: dict[str, Any] = {"ok": False, "error_code": 429}
    if parameters is not None:
        body["parameters"] = parameters
    fake_telegram.fail(TOKEN, status=429, json_body=body)

    assert _send() == SendResult("rate_limited", retry_after=expected, code="429")


def test_server_error_is_transient(fake_telegram: Any) -> None:
    fake_telegram.rsps.add(responses.POST, SEND_URL, status=502, body="<html>Bad Gateway</html>")

    assert _send() == SendResult("transient", code="http_502")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors(fake_telegram: Any, status: int) -> None:
    body = {"ok": False, "error_code": status, "description": "Bad Request: chat not found"}
    fake_telegram.fail(TOKEN, status=status, json_body=body)

    assert _send() == SendResult("permanent", code=f"http_{status}")


@pytest.mark.parametrize(
    "body",
    [["not", "an", "object"], {"ok": False, "error_code": f"see {SEND_URL}"}, None],
    ids=["list", "url-in-error-code", "empty"],
)
def test_unexpected_error_bodies_are_permanent_and_safe(fake_telegram: Any, body: Any) -> None:
    # The response is untrusted input: an odd body must neither raise nor reach the code.
    fake_telegram.fail(TOKEN, status=400, json_body=body)

    assert _send() == SendResult("permanent", code="http_400")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, SendResult("permanent", code="http_200")),
        (429, SendResult("rate_limited", retry_after=30, code="429")),
        (502, SendResult("transient", code="http_502")),
    ],
    ids=["200", "429", "502"],
)
def test_deeply_nested_body_never_raises(
    fake_telegram: Any, status: int, expected: SendResult
) -> None:
    # The body is untrusted: json.loads raises RecursionError (not ValueError) on deep
    # nesting. The answer arrived, so it is classified like any unparseable body (audit F2).
    depth = 500_000
    fake_telegram.rsps.add(responses.POST, SEND_URL, status=status, body="[" * depth + "]" * depth)

    assert _send() == expected


def test_ok_false_on_200_is_not_success(fake_telegram: Any) -> None:
    fake_telegram.fail(TOKEN, status=200, json_body={"ok": False})

    assert _send().kind == "permanent"


def test_connect_timeout_is_not_sent(fake_telegram: Any) -> None:
    fake_telegram.fail(TOKEN, exc=requests.ConnectTimeout("connect timed out"))

    assert _send() == SendResult("not_sent", code="connect_timeout")


def test_refused_connection_is_not_sent(fake_telegram: Any) -> None:
    fake_telegram.fail(
        TOKEN,
        exc=requests.ConnectionError(
            MaxRetryError(None, "/x", NewConnectionError(None, "refused"))
        ),
    )

    assert _send() == SendResult("not_sent", code="connect_error")


def test_dns_failure_is_not_sent(fake_telegram: Any) -> None:
    fake_telegram.fail(TOKEN, exc=_dns_failure())

    assert _send() == SendResult("not_sent", code="connect_error")


def test_read_timeout_is_maybe_delivered(fake_telegram: Any) -> None:
    fake_telegram.fail(TOKEN, exc=requests.ReadTimeout("read timed out"))

    assert _send() == SendResult("maybe_delivered", code="read_timeout")


def test_dropped_connection_is_maybe_delivered(fake_telegram: Any) -> None:
    fake_telegram.fail(TOKEN, exc=requests.ConnectionError(ProtocolError("Connection aborted.")))

    assert _send() == SendResult("maybe_delivered", code="connection_dropped")


def test_unexpected_transport_exception_is_maybe_delivered(fake_telegram: Any) -> None:
    # Never raise across the boundary; at-most-once wins when the outcome is unknown.
    fake_telegram.fail(TOKEN, exc=RuntimeError(f"boom at {SEND_URL}"))

    assert _send() == SendResult("maybe_delivered", code="unexpected_error")


@pytest.mark.parametrize("exc", [requests.ReadTimeout("t"), _dropped()], ids=["read", "drop"])
def test_maybe_delivered_is_not_retried_by_the_client(fake_telegram: Any, exc: Any) -> None:
    fake_telegram.fail(TOKEN, exc=exc)

    _send()

    assert len(fake_telegram.calls) == 1


def test_server_error_is_not_retried_by_the_client(fake_telegram: Any) -> None:
    fake_telegram.fail(TOKEN, status=502)

    _send()

    assert len(fake_telegram.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "kind", "code"),
    [case[1:] for case in FAILURES],
    ids=[case[0] for case in FAILURES],
)
def test_result_code_never_contains_token(
    fake_telegram: Any, caplog: Any, kwargs: dict[str, Any], kind: str, code: str
) -> None:
    # INV-23: the token reaches neither the result nor any log record the client writes.
    fake_telegram.fail(TOKEN, **kwargs)
    secret = TOKEN.split(":", 1)[1]

    with caplog.at_level(logging.DEBUG):
        result = _send()

    assert (result.kind, result.code) == (kind, code)
    assert re.fullmatch(r"[a-z0-9_]{1,24}", result.code)
    assert secret not in repr(result)
    assert secret not in caplog.text
    assert all(secret not in str(record.args) for record in caplog.records)


def test_client_repr_hides_token() -> None:
    # A client that ends up in a log line or a traceback must not print its URL.
    text = repr(TelegramClient(TOKEN))

    assert TOKEN.split(":", 1)[1] not in text
    assert "sendMessage" not in text


def test_send_result_defaults() -> None:
    result = SendResult("ok")

    assert (result.kind, result.retry_after, result.code) == ("ok", None, "")
    assert (result.message_id, result.migrate_to_chat_id) == (None, None)
    with pytest.raises(AttributeError):
        result.code = "changed"


# D-10, PITFALLS 6e: a group upgraded to a supergroup answers 400 with the new chat ID


MIGRATED = -1009999999999


def _upgraded(parameters: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": parameters,
    }


@pytest.mark.parametrize("value", [MIGRATED, -(2**63), 2**63 - 1, 1])
def test_migrate_to_chat_id_is_read_from_a_refusal(fake_telegram: Any, value: int) -> None:
    fake_telegram.fail(TOKEN, status=400, json_body=_upgraded({"migrate_to_chat_id": value}))

    result = _send()

    assert result == SendResult("permanent", code="http_400", migrate_to_chat_id=value)


@pytest.mark.parametrize(
    "parameters",
    [
        {"migrate_to_chat_id": True},
        {"migrate_to_chat_id": -1009999999999.0},
        {"migrate_to_chat_id": "-1009999999999"},
        {"migrate_to_chat_id": 2**63},
        {"migrate_to_chat_id": -(2**63) - 1},
        {"migrate_to_chat_id": None},
        {},
        "not-a-dict",
        None,
    ],
    ids=[
        "bool",
        "float",
        "str",
        "above-int64",
        "below-int64",
        "null",
        "missing",
        "str-params",
        "none",
    ],
)
def test_migrate_to_chat_id_needs_an_int64(fake_telegram: Any, parameters: Any) -> None:
    # The body is untrusted: anything but a 64-bit integer is ignored, never raised.
    fake_telegram.fail(TOKEN, status=400, json_body=_upgraded(parameters))

    result = _send()

    assert result == SendResult("permanent", code="http_400")
    assert result.migrate_to_chat_id is None


def test_migrate_to_chat_id_is_only_on_a_permanent_result(fake_telegram: Any) -> None:
    # A 429 or a 5xx is not a refusal of the chat: the field stays empty.
    body = {**_upgraded({"migrate_to_chat_id": MIGRATED, "retry_after": 7}), "error_code": 429}
    fake_telegram.fail(TOKEN, status=429, json_body=body)
    fake_telegram.fail(TOKEN, status=502, json_body=_upgraded({"migrate_to_chat_id": MIGRATED}))

    assert _send() == SendResult("rate_limited", retry_after=7, code="429")
    assert _send() == SendResult("transient", code="http_502")


def test_client_source_never_raises_for_status_or_mounts_retries() -> None:
    # STACK G2/G3: raise_for_status() puts the URL (token) in the error text, and a retry
    # policy would resend on its own; retry timing belongs to the outbox (INV-15/16). The
    # client mounts one HTTPAdapter subclass (the connect/send split, audit F1) that keeps
    # requests' default of no retries; test_client_tls.py checks every real failure uses
    # exactly one connection.
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(ast.parse(CLIENT_SOURCE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute | ast.Name)
    }

    assert "post" in names  # the scan saw the real client, so an empty pass proves nothing
    assert not names & {"raise_for_status", "Retry", "max_retries"}
