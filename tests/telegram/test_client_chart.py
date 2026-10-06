"""TelegramClient chart calls: sendPhoto, editMessageMedia, pinChatMessage, unpinChatMessage.

Telegram is faked at the HTTP boundary (``fake_telegram``, built on ``responses``), and
multipart bodies are read back with ``parse_multipart``.

- D-01: a chart is posted and pinned silently (``disable_notification``), with a plain-text
  caption (no ``parse_mode``).
- D-04: unpin always names the stored message; the client has no unpin-all call (INV-19).
- D-05: an edit replaces the image and the caption in one call (the caption sits inside
  the ``media`` JSON); "message is not modified" counts as success.
- D-06: "message to edit / pin / unpin not found" is ``edit_target_missing`` (INV-17: the
  record is retired, today's chart is posted again once); an ok sendPhoto without a usable
  message id is ``maybe_delivered``, since the photo cannot be recorded.
- D-07: a missing pin right stays ``permanent`` (the lifecycle's pin-failure path).
- D-08: a fresh session per call, the short (5 s, 10 s) timeouts, no redirects, no client
  retries.
- INV-23: result codes are short fixed strings, never the token or Telegram's text.
"""

import email.policy
import json
import logging
import pathlib
import re
from collections.abc import Callable
from email.parser import BytesParser
from typing import Any

import pytest
import requests
import responses
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, TELEGRAM_API, parse_multipart
from requests import PreparedRequest
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from powermon.telegram import client as client_module
from powermon.telegram.client import SendResult, TelegramClient

TOKEN = DEFAULT_BOT_TOKEN
# Fixed bytes stand in for a chart: the client never looks inside the PNG.
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
CAPTION = "Today off: 4h 10m · 2 outages"
CAPTION_UK = "Сьогодні без світла: 4 год 10 хв · 2 відключення"
MESSAGE_ID = 1001
CLIENT_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "powermon/telegram/client.py"

# One call of each chart method, with the same arguments every time.
CALLS: dict[str, Callable[[TelegramClient], SendResult]] = {
    "sendPhoto": lambda c: c.send_photo(DEFAULT_CHAT_ID, PNG, CAPTION),
    "editMessageMedia": lambda c: c.edit_message_media(DEFAULT_CHAT_ID, MESSAGE_ID, PNG, CAPTION),
    "pinChatMessage": lambda c: c.pin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID),
    "unpinChatMessage": lambda c: c.unpin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID),
}
METHODS = list(CALLS)
# The calls that name a stored message (D-04): a bad id is a programming error.
BY_ID: dict[str, Callable[[TelegramClient, Any], SendResult]] = {
    "editMessageMedia": lambda c, m: c.edit_message_media(DEFAULT_CHAT_ID, m, PNG, CAPTION),
    "pinChatMessage": lambda c, m: c.pin_chat_message(DEFAULT_CHAT_ID, m),
    "unpinChatMessage": lambda c, m: c.unpin_chat_message(DEFAULT_CHAT_ID, m),
}


def _url(method: str) -> str:
    return f"{TELEGRAM_API}/bot{TOKEN}/{method}"


def _client() -> TelegramClient:
    return TelegramClient(TOKEN)


def _bad_request(description: Any) -> dict[str, Any]:
    return {
        "status": 400,
        "json_body": {"ok": False, "error_code": 400, "description": description},
    }


def _file_part(request: PreparedRequest, field: str) -> tuple[str | None, str]:
    """The file name and content type of the multipart file field ``field``."""
    assert isinstance(request.body, bytes)
    content_type = request.headers["Content-Type"].encode("ascii")
    message: Any = BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type + b"\r\n\r\n" + request.body
    )
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") == field:
            return part.get_filename(), part.get_content_type()
    raise AssertionError(f"no multipart field {field!r}")


# sendPhoto (D-01, INV-17)


def test_send_photo_posts_multipart_and_returns_the_message_id(fake_telegram: Any) -> None:
    fake_telegram.accept_chart(TOKEN)

    result = _client().send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)

    assert result == SendResult("ok", message_id=1001)
    assert len(fake_telegram.calls) == 1
    request = fake_telegram.calls[0].request
    assert (request.method, request.url) == ("POST", _url("sendPhoto"))
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    fields, files = parse_multipart(request)
    # Exactly these fields: silent, plain-text caption (no parse_mode).
    assert fields == {
        "chat_id": str(DEFAULT_CHAT_ID),
        "caption": CAPTION,
        "disable_notification": "true",
    }
    assert files == {"photo": PNG}
    assert _file_part(request, "photo") == ("chart.png", "image/png")
    assert [(c.method, c.token, c.files) for c in fake_telegram.chart_calls] == [
        ("sendPhoto", TOKEN, {"photo": PNG})
    ]


def test_send_photo_carries_a_utf8_caption_unchanged(fake_telegram: Any) -> None:
    fake_telegram.accept_chart(TOKEN)

    assert _client().send_photo(DEFAULT_CHAT_ID, PNG, CAPTION_UK).kind == "ok"

    assert fake_telegram.chart_calls[0].fields["caption"] == CAPTION_UK


def test_a_second_photo_gets_the_next_message_id(fake_telegram: Any) -> None:
    fake_telegram.accept_chart(TOKEN)
    client = _client()

    first = client.send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)
    second = client.send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)

    assert (first.message_id, second.message_id) == (1001, 1002)
    assert len(fake_telegram.calls) == 2


def test_send_message_still_returns_ok_without_a_message_id(fake_telegram: Any) -> None:
    # No regression: the alert path ignores the id Telegram sends back.
    fake_telegram.accept(TOKEN)

    result = _client().send_message(DEFAULT_CHAT_ID, "x")

    assert result == SendResult("ok")
    assert result.message_id is None
    assert fake_telegram.chart_calls == []


def test_send_photo_failure_is_logged_by_kind_and_code(fake_telegram: Any, caplog: Any) -> None:
    body = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}
    fake_telegram.rsps.add(responses.POST, _url("sendPhoto"), status=403, json=body)

    with caplog.at_level(logging.WARNING, logger="powermon.telegram.client"):
        result = _client().send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)

    assert result == SendResult("permanent", code="http_403")
    assert "telegram sendPhoto: permanent (http_403)" in caplog.text
    assert "kicked" not in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        None,
        [{"message_id": 5}],
        {},
        {"message_id": True},
        {"message_id": 0},
        {"message_id": -5},
        {"message_id": "12"},
        {"message_id": 12.0},
        {"message_id": 2**63},
    ],
    ids=["missing", "list", "no-id", "bool", "zero", "negative", "str", "float", "too-big"],
)
def test_send_photo_needs_a_usable_message_id(fake_telegram: Any, caplog: Any, result: Any) -> None:
    # D-06: the photo may exist, but without a usable id it cannot be recorded or pinned.
    body: dict[str, Any] = {"ok": True}
    if result is not None:
        body["result"] = result
    fake_telegram.fail_method(TOKEN, "sendPhoto", status=200, json_body=body)

    with caplog.at_level(logging.WARNING, logger="powermon.telegram.client"):
        answer = _client().send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)

    assert answer == SendResult("maybe_delivered", code="no_message_id")
    assert "telegram sendPhoto: maybe_delivered (no_message_id)" in caplog.text


def test_send_photo_accepts_the_largest_storable_message_id(fake_telegram: Any) -> None:
    body = {"ok": True, "result": {"message_id": 2**63 - 1}}
    fake_telegram.fail_method(TOKEN, "sendPhoto", status=200, json_body=body)

    assert _client().send_photo(DEFAULT_CHAT_ID, PNG, CAPTION) == SendResult(
        "ok", message_id=2**63 - 1
    )


# editMessageMedia, pinChatMessage, unpinChatMessage (D-01, D-04, D-05)


def test_edit_message_media_puts_the_caption_inside_the_media(fake_telegram: Any) -> None:
    fake_telegram.accept_chart(TOKEN)

    result = _client().edit_message_media(DEFAULT_CHAT_ID, MESSAGE_ID, PNG, CAPTION_UK)

    assert result == SendResult("ok")
    request = fake_telegram.calls[0].request
    assert request.url == _url("editMessageMedia")
    fields, files = parse_multipart(request)
    # No top-level caption and no parse_mode: the caption is replaced with the image.
    assert set(fields) == {"chat_id", "message_id", "media"}
    assert (fields["chat_id"], fields["message_id"]) == (str(DEFAULT_CHAT_ID), "1001")
    assert json.loads(fields["media"]) == {
        "type": "photo",
        "media": "attach://chart",
        "caption": CAPTION_UK,
    }
    assert files == {"chart": PNG}
    assert _file_part(request, "chart") == ("chart.png", "image/png")
    assert fake_telegram.count(TOKEN, "editMessageMedia") == 1


def test_pin_is_silent_and_by_id(fake_telegram: Any) -> None:
    fake_telegram.accept_chart(TOKEN)

    result = _client().pin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)

    assert result == SendResult("ok")
    request = fake_telegram.calls[0].request
    assert request.url == _url("pinChatMessage")
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.body) == {
        "chat_id": DEFAULT_CHAT_ID,
        "message_id": MESSAGE_ID,
        "disable_notification": True,
    }


def test_unpin_always_names_the_message(fake_telegram: Any) -> None:
    fake_telegram.accept_chart(TOKEN)

    result = _client().unpin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)

    assert result == SendResult("ok")
    request = fake_telegram.calls[0].request
    assert request.url == _url("unpinChatMessage")
    assert json.loads(request.body) == {"chat_id": DEFAULT_CHAT_ID, "message_id": MESSAGE_ID}
    assert [(c.method, c.fields) for c in fake_telegram.chart_calls] == [
        ("unpinChatMessage", {"chat_id": DEFAULT_CHAT_ID, "message_id": MESSAGE_ID})
    ]


# Classification (D-05, D-06, D-07, INV-17)

NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and reply markup "
    "are exactly the same as a current content and reply markup of the message"
)
NO_PIN_RIGHT = "Bad Request: not enough rights to manage pinned messages in the chat"
KICKED = "Forbidden: bot was kicked from the channel chat"
FORBIDDEN = {"status": 403, "json_body": {"ok": False, "error_code": 403, "description": KICKED}}
RATE_LIMITED = {
    "status": 429,
    "json_body": {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 7",
        "parameters": {"retry_after": 7},
    },
}
# Every chart method answers these the way sendMessage does.
COMMON: list[tuple[str, dict[str, Any], SendResult]] = [
    ("403", FORBIDDEN, SendResult("permanent", code="http_403")),
    ("429", RATE_LIMITED, SendResult("rate_limited", retry_after=7, code="429")),
    ("502", {"status": 502}, SendResult("transient", code="http_502")),
    (
        "read-timeout",
        {"exc": requests.ReadTimeout("read timed out")},
        SendResult("maybe_delivered", code="read_timeout"),
    ),
    (
        "connect-timeout",
        {"exc": requests.ConnectTimeout("connect timed out")},
        SendResult("not_sent", code="connect_timeout"),
    ),
]

# (id, method, FakeTelegram.fail_method kwargs, expected result)
CLASSIFICATION: list[tuple[str, str, dict[str, Any], SendResult]] = [
    (
        "edit-not-modified",
        "editMessageMedia",
        _bad_request(NOT_MODIFIED),
        SendResult("ok", code="not_modified"),
    ),
    (
        "edit-not-modified-any-case",
        "editMessageMedia",
        _bad_request(NOT_MODIFIED.upper()),
        SendResult("ok", code="not_modified"),
    ),
    (
        "edit-target-gone",
        "editMessageMedia",
        _bad_request("Bad Request: message to edit not found"),
        SendResult("edit_target_missing", code="target_missing"),
    ),
    (
        "pin-target-gone",
        "pinChatMessage",
        _bad_request("Bad Request: message to pin not found"),
        SendResult("edit_target_missing", code="target_missing"),
    ),
    (
        "unpin-target-gone",
        "unpinChatMessage",
        _bad_request("Bad Request: message to unpin not found"),
        SendResult("edit_target_missing", code="target_missing"),
    ),
    (
        "pin-already-pinned",
        "pinChatMessage",
        _bad_request("Bad Request: CHAT_NOT_MODIFIED"),
        SendResult("ok", code="not_modified"),
    ),
    (
        "unpin-not-pinned",
        "unpinChatMessage",
        _bad_request("Bad Request: CHAT_NOT_MODIFIED"),
        SendResult("ok", code="not_modified"),
    ),
    (
        "pin-no-right",
        "pinChatMessage",
        _bad_request(NO_PIN_RIGHT),
        SendResult("permanent", code="http_400"),
    ),
    (
        "edit-cannot-be-edited",
        "editMessageMedia",
        _bad_request("Bad Request: message can't be edited"),
        SendResult("permanent", code="http_400"),
    ),
    (
        "edit-description-not-text",
        "editMessageMedia",
        _bad_request(["Bad Request: message to edit not found"]),
        SendResult("permanent", code="http_400"),
    ),
    (
        "edit-no-description",
        "editMessageMedia",
        {"status": 400, "json_body": {"ok": False, "error_code": 400}},
        SendResult("permanent", code="http_400"),
    ),
    *(
        (f"{method}-{case}", method, kwargs, result)
        for method in METHODS
        for case, kwargs, result in COMMON
    ),
]


@pytest.mark.parametrize(
    ("method", "kwargs", "expected"),
    [case[1:] for case in CLASSIFICATION],
    ids=[case[0] for case in CLASSIFICATION],
)
def test_chart_call_classification(
    fake_telegram: Any, caplog: Any, method: str, kwargs: dict[str, Any], expected: SendResult
) -> None:
    fake_telegram.fail_method(TOKEN, method, **kwargs)

    with caplog.at_level(logging.DEBUG, logger="powermon.telegram.client"):
        result = CALLS[method](_client())

    assert result == expected
    assert fake_telegram.count(TOKEN, method) == 1
    if expected.kind == "ok":
        assert caplog.text == ""
    else:
        assert f"telegram {method}: {expected.kind} ({expected.code})" in caplog.text
    # Telegram's description is untrusted text: never logged, never in the result.
    for text in ("Bad Request", "BAD REQUEST", "Forbidden", "Too Many Requests"):
        assert text not in caplog.text + repr(result)


@pytest.mark.parametrize("method", ["sendPhoto", "sendMessage"])
@pytest.mark.parametrize(
    "description",
    ["Bad Request: message to edit not found", NOT_MODIFIED, "Bad Request: CHAT_NOT_MODIFIED"],
    ids=["not-found", "not-modified", "chat-not-modified"],
)
def test_send_photo_and_send_message_keep_the_old_400_rule(
    fake_telegram: Any, method: str, description: str
) -> None:
    # The description mapping is limited to edit, pin and unpin: a 400 on a send is
    # permanent whatever Telegram says.
    fake_telegram.fail_method(TOKEN, method, **_bad_request(description))
    client = _client()

    if method == "sendPhoto":
        result = client.send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)
    else:
        result = client.send_message(DEFAULT_CHAT_ID, "x")

    assert result == SendResult("permanent", code="http_400")


# Transport (D-08)


@pytest.mark.parametrize("method", METHODS)
def test_chart_calls_use_the_short_timeouts_and_no_redirects(
    fake_telegram: Any, method: str
) -> None:
    # responses does not record allow_redirects, so the redirect is checked by behaviour:
    # a followed 302 would POST again to an unregistered URL, which raises (a second call,
    # maybe_delivered). Not following it leaves the 302 itself, from exactly one call.
    elsewhere = f"{TELEGRAM_API}/elsewhere/{method}"
    fake_telegram.rsps.add(
        responses.POST, _url(method), status=302, headers={"Location": elsewhere}
    )

    result = CALLS[method](_client())

    assert result == SendResult("permanent", code="http_302")
    assert len(fake_telegram.calls) == 1
    assert fake_telegram.calls[0].request.req_kwargs["timeout"] == (5.0, 10.0)


@pytest.mark.parametrize("method", METHODS)
def test_every_chart_call_opens_a_fresh_session(
    fake_telegram: Any, monkeypatch: Any, method: str
) -> None:
    sessions: list[requests.Session] = []
    original = client_module._new_session

    def spy() -> requests.Session:
        sessions.append(original())
        return sessions[-1]

    monkeypatch.setattr(client_module, "_new_session", spy)
    fake_telegram.accept_chart(TOKEN)
    client = _client()

    assert CALLS[method](client).kind == "ok"
    assert CALLS[method](client).kind == "ok"

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


@pytest.mark.parametrize("method", METHODS)
def test_unexpected_error_in_a_chart_call_is_logged_by_class(
    fake_telegram: Any, caplog: Any, method: str
) -> None:
    fake_telegram.fail_method(TOKEN, method, exc=RuntimeError(f"boom at {_url(method)}"))

    with caplog.at_level(logging.WARNING, logger="powermon.telegram.client"):
        result = CALLS[method](_client())

    assert result == SendResult("maybe_delivered", code="unexpected_error")
    assert f"telegram {method}: unexpected RuntimeError" in caplog.text
    assert TOKEN.split(":", 1)[1] not in caplog.text


@pytest.mark.parametrize("method", list(BY_ID))
@pytest.mark.parametrize("message_id", [0, -1, True, "1001"], ids=["zero", "neg", "bool", "str"])
def test_bad_message_ids_raise_before_any_request(
    fake_telegram: Any, method: str, message_id: Any
) -> None:
    # A programming error, not a Telegram outcome: no request is made for it.
    with pytest.raises(ValueError, match="message_id"):
        BY_ID[method](_client(), message_id)

    assert len(fake_telegram.calls) == 0


def test_client_has_no_unpin_all_call() -> None:
    # D-04, INV-19: only stored chart messages are unpinned, by id; admin pins stay.
    names = [name.lower().replace("_", "") for name in dir(TelegramClient)]

    assert "unpinchatmessage" in names  # the scan saw the real client
    assert not [name for name in names if "unpinall" in name]
    assert "unpinAll" not in CLIENT_SOURCE.read_text(encoding="utf-8")


# Token-free result codes (INV-23, OPS-08)


def _token_failures(method: str) -> list[tuple[str, dict[str, Any]]]:
    """Every failure kind, each answer or exception text carrying the URL and so the token."""
    path = f"/bot{TOKEN}/{method}"
    url = _url(method)
    refused = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return [
        ("429", RATE_LIMITED),
        ("502", {"status": 502, "body": f"<html>Bad Gateway {url}</html>"}),
        ("400", _bad_request(f"Bad Request: chat not found {url}")),
        ("400-not-found", _bad_request(f"Bad Request: message to edit not found {url}")),
        (
            "401",
            {"status": 401, "json_body": {"ok": False, "error_code": 401, "description": TOKEN}},
        ),
        ("403", FORBIDDEN),
        ("error-code-is-url", {"status": 400, "json_body": {"ok": False, "error_code": url}}),
        ("connect-timeout", {"exc": requests.ConnectTimeout(f"timed out: {url}")}),
        ("refused", {"exc": requests.ConnectionError(MaxRetryError(None, path, refused))}),
        ("read-timeout", {"exc": requests.ReadTimeout(f"read timed out: {url}")}),
        (
            "dropped",
            {"exc": requests.ConnectionError(ProtocolError(f"Connection aborted. {url}"))},
        ),
        ("request-error", {"exc": requests.exceptions.ChunkedEncodingError(f"broken: {url}")}),
        ("unexpected", {"exc": RuntimeError(f"boom at {url}")}),
    ]


TOKEN_FAILURES: list[tuple[str, str, dict[str, Any]]] = [
    *(
        (f"{method}-{case}", method, kwargs)
        for method in METHODS
        for case, kwargs in _token_failures(method)
    ),
    # An ok answer whose message_id is the token itself (a broken or hostile answer).
    (
        "sendPhoto-message-id-is-token",
        "sendPhoto",
        {"status": 200, "json_body": {"ok": True, "result": {"message_id": TOKEN}}},
    ),
]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [case[1:] for case in TOKEN_FAILURES],
    ids=[case[0] for case in TOKEN_FAILURES],
)
def test_chart_result_codes_never_contain_the_token(
    fake_telegram: Any, caplog: Any, method: str, kwargs: dict[str, Any]
) -> None:
    if "body" in kwargs:
        fake_telegram.rsps.add(responses.POST, _url(method), **kwargs)
    else:
        fake_telegram.fail_method(TOKEN, method, **kwargs)
    secret = TOKEN.split(":", 1)[1]

    with caplog.at_level(logging.DEBUG):
        result = CALLS[method](_client())

    assert result.kind != "ok"
    assert re.fullmatch(r"[a-z0-9_]{1,24}", result.code)
    assert secret not in repr(result)
    assert secret not in caplog.text
    assert all(secret not in str(record.args) for record in caplog.records)
    # Every failure is logged, by method, kind and code only.
    assert f"telegram {method}: " in caplog.text


# The fake's per-method helpers, which the chart lifecycle tests build on


def test_fail_method_then_accept_chart_answer_in_order(fake_telegram: Any) -> None:
    fake_telegram.fail_method(TOKEN, "pinChatMessage", **_bad_request(NO_PIN_RIGHT))
    fake_telegram.accept_chart(TOKEN)
    client = _client()

    first = client.pin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)
    second = client.pin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)
    third = client.pin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)

    assert [first.kind, second.kind, third.kind] == ["permanent", "ok", "ok"]
    assert fake_telegram.count(TOKEN, "pinChatMessage") == 3
    # Only accepted calls are recorded.
    assert [c.method for c in fake_telegram.chart_calls] == ["pinChatMessage"] * 2


def test_answer_method_runs_during_then_accepts(fake_telegram: Any) -> None:
    seen: list[str] = []
    fake_telegram.answer_method(TOKEN, "sendPhoto", lambda: seen.append("during"))

    result = _client().send_photo(DEFAULT_CHAT_ID, PNG, CAPTION)

    assert result == SendResult("ok", message_id=1001)
    assert seen == ["during"]
    assert [c.method for c in fake_telegram.chart_calls] == ["sendPhoto"]


def test_answer_method_can_answer_an_error_or_raise(fake_telegram: Any) -> None:
    gone = _bad_request("Bad Request: message to edit not found")
    fake_telegram.answer_method(TOKEN, "editMessageMedia", lambda: None, **gone)
    timeout = requests.ReadTimeout("read timed out")
    fake_telegram.answer_method(TOKEN, "unpinChatMessage", lambda: None, exc=timeout)
    client = _client()

    edited = client.edit_message_media(DEFAULT_CHAT_ID, MESSAGE_ID, PNG, CAPTION)
    unpinned = client.unpin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)

    assert edited == SendResult("edit_target_missing", code="target_missing")
    assert unpinned == SendResult("maybe_delivered", code="read_timeout")
    assert fake_telegram.chart_calls == []


def test_answer_method_and_fail_method_refuse_misuse(fake_telegram: Any) -> None:
    with pytest.raises(ValueError, match="sendMessage"):
        fake_telegram.answer_method(TOKEN, "sendMessage", lambda: None)
    with pytest.raises(TypeError):
        fake_telegram.fail_method(TOKEN, "sendPhoto")


def test_an_unregistered_chart_method_still_raises(fake_telegram: Any) -> None:
    # Only sendPhoto is answered: any other URL raises in the transport (maybe_delivered).
    fake_telegram.fail_method(TOKEN, "sendPhoto", status=200, json_body={"ok": True})

    result = _client().pin_chat_message(DEFAULT_CHAT_ID, MESSAGE_ID)

    assert result == SendResult("maybe_delivered", code="connection_dropped")
    assert fake_telegram.count(TOKEN, "pinChatMessage") == 1
