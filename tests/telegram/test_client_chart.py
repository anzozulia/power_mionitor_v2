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
import logging
from email.parser import BytesParser
from typing import Any

import responses
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, TELEGRAM_API, parse_multipart
from requests import PreparedRequest

from powermon.telegram.client import SendResult, TelegramClient

TOKEN = DEFAULT_BOT_TOKEN
# Fixed bytes stand in for a chart: the client never looks inside the PNG.
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
CAPTION = "Today off: 4h 10m · 2 outages\nUpdated 14:37"
CAPTION_UK = "Сьогодні без світла: 4 год 10 хв · 2 відключення\nОновлено о 14:37"


def _url(method: str) -> str:
    return f"{TELEGRAM_API}/bot{TOKEN}/{method}"


def _client() -> TelegramClient:
    return TelegramClient(TOKEN)


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
