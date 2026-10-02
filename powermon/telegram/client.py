"""Telegram Bot API client: the calls the app makes.

- ``send_message``: an alert or an ops notice (``sendMessage``, Telegram HTML), or the
  admin's test message, sent silently (``disable_notification``, D-11).
- ``send_photo``: post a chart silently (multipart ``sendPhoto`` with
  ``disable_notification``, D-01). The result carries the new message's id, which the chart
  lifecycle records before it pins (INV-17).
- ``edit_message_media``: replace a chart's image and caption in one call (multipart
  ``editMessageMedia``, the caption inside the ``media`` JSON, D-05).
- ``pin_chat_message`` / ``unpin_chat_message``: pin a chart silently, and unpin exactly
  that message (D-04). There is no call that unpins everything: pins the channel admin
  made stay (INV-19).

Edit, pin and unpin name a stored message, so a ``message_id`` that is not an int above 0
is a programming error: ``ValueError``, before any request. For these three calls only, a
400 whose description says the message is unchanged ("message is not modified", or
``CHAT_NOT_MODIFIED`` for a pin or unpin already in place) is ``ok`` with code
``not_modified``, and one that says the message is gone ("message to edit / pin / unpin
not found") is ``edit_target_missing``. Any other 400, such as a missing pin right, stays
``permanent`` (D-07). The description is untrusted: it is only matched, never logged,
stored or returned.

No method raises across its boundary. Every outcome comes back as a ``SendResult`` whose
``code`` is a short fixed string, never the token, a URL or Telegram's description text:

- ``ok``: Telegram accepted the call. For ``send_photo``, ``message_id`` is the posted
  message's id (an int above 0).
- ``not_sent``: no request byte left the client (connect timeout, refused connection, DNS
  failure, a TLS handshake that failed or timed out). Safe to retry.
- ``maybe_delivered``: the request may have reached Telegram (read timeout after the
  request was written, connection dropped after sending, a TLS error after the handshake,
  any other transport error). The caller decides what that means: an alert is never resent
  (INV-16), an ambiguous chart post is posted again (D-06). A ``sendPhoto`` that Telegram
  accepted without a usable ``message_id`` is ``maybe_delivered`` too (code
  ``no_message_id``): the photo may exist but cannot be recorded.
- ``rate_limited``: HTTP 429; ``retry_after`` is the wait in whole seconds.
- ``transient``: HTTP 5xx.
- ``permanent``: any other answer (400, 401, 403, 404, ...). When Telegram says the group
  became a supergroup (``parameters.migrate_to_chat_id``), the result carries that new
  chat ID, if it is a 64-bit integer, for the admin's notice (D-10); no caller ever sends
  to it on its own (PITFALLS 6e).
- ``edit_target_missing``: the chart message an edit, pin or unpin names no longer exists
  (deleted in the channel, INV-17).

Where "not sent" ends: urllib3 opens the connection (TCP and the TLS handshake) before it
writes the request, but it reports a handshake timeout as a read timeout and a handshake
failure as an SSLError, the same types it uses once the request is on the wire. So HTTPS
calls go through ``_ConnectPhaseConnection``, whose ``connect()`` re-raises any handshake
failure as a ``NewConnectionError`` subclass. Every call uses a fresh session, so every
request starts with ``connect()`` (D-08).

The token sits in the URL path, and requests puts that URL into the text of connect
errors and of ``raise_for_status()`` (STACK G3, P-12). So this module never calls
``raise_for_status()``, never logs the URL or an exception's text, and logs only the
method, kind and code. The adapter keeps requests' default of no retries, and nothing
sleeps: all retry timing belongs to the caller (the outbox relay, INV-15/16, and the chart
lifecycle). Every call passes explicit (connect, read) timeouts, because requests has no
default timeout.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError

log = logging.getLogger(__name__)

SendKind = Literal[
    "ok",
    "not_sent",
    "maybe_delivered",
    "rate_limited",
    "transient",
    "permanent",
    "edit_target_missing",
]

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 10.0)
# The wait used when a 429 carries no usable retry_after (STACK G13).
DEFAULT_RETRY_AFTER_S = 30
# The multipart file field of sendPhoto, the attach:// name of editMessageMedia's file
# field, and the file name both uploads carry.
PHOTO_FIELD = "photo"
MEDIA_ATTACH = "chart"
PHOTO_FILENAME = "chart.png"
_PNG_TYPE = "image/png"
# The largest message id the chart record can store (a PostgreSQL bigint).
_MAX_MESSAGE_ID = 2**63 - 1
# A chat ID Telegram reports must fit a signed 64-bit integer (a location's chat_id).
_MIN_CHAT_ID = -(2**63)
_MAX_CHAT_ID = 2**63 - 1
# Lower-case substrings of Telegram's 400 descriptions for an edit, a pin or an unpin.
_NOT_MODIFIED = ("message is not modified", "chat_not_modified")
_TARGET_MISSING = (
    "message to edit not found",
    "message to pin not found",
    "message to unpin not found",
)

_RETRY_AFTER_TEXT = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True)
class SendResult:
    """The outcome of one call. ``code`` is short and never holds the token or a URL.

    ``message_id`` is set only on an ``ok`` from a call that posts a message (sendPhoto).
    ``migrate_to_chat_id`` is set only on a ``permanent`` answer that reports the
    supergroup a group became (D-10); it is reported, never sent to (PITFALLS 6e).
    """

    kind: SendKind
    retry_after: int | None = None
    code: str = ""
    message_id: int | None = None
    migrate_to_chat_id: int | None = None


class _HandshakeError(NewConnectionError):
    """The TLS handshake failed (certificate, protocol, reset): no request byte was written."""

    code: ClassVar[str] = "tls_handshake_error"


class _HandshakeTimeout(_HandshakeError):
    """The TLS handshake timed out: no request byte was written."""

    code: ClassVar[str] = "tls_handshake_timeout"


class _ConnectPhaseConnection(HTTPSConnection):
    """An HTTPS connection whose ``connect()`` fails only with NewConnectionError types."""

    def connect(self) -> None:
        try:
            super().connect()
        except ConnectTimeoutError:  # the TCP connect: timeout, refused, DNS (already not sent)
            raise
        except TimeoutError as exc:  # the socket timed out during the TLS handshake
            raise _HandshakeTimeout(self, "TLS handshake timed out") from exc
        except Exception as exc:
            raise _HandshakeError(self, "TLS handshake failed") from exc


class _ConnectPhasePool(HTTPSConnectionPool):
    ConnectionCls = _ConnectPhaseConnection


class _ConnectPhaseAdapter(HTTPAdapter):
    """requests' default adapter (no retries) with ``_ConnectPhaseConnection`` for HTTPS."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        manager = self.poolmanager
        manager.pool_classes_by_scheme = {
            **manager.pool_classes_by_scheme,
            "https": _ConnectPhasePool,
        }


def _new_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", _ConnectPhaseAdapter())
    return session


class TelegramClient:
    """Makes Bot API calls for one bot.

    The token was checked against ``[0-9]{5,}:[A-Za-z0-9_-]{30,}`` when it was saved, which
    keeps ``/``, ``?`` and ``#`` out of the URL path (T-01-21).
    """

    def __init__(
        self,
        token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        self._base = f"{api_base}/bot{token}"
        self._timeout = timeout

    def __repr__(self) -> str:
        return "TelegramClient(<token hidden>)"

    def send_message(
        self, chat_id: int, text: str, *, disable_notification: bool = False
    ) -> SendResult:
        """Send ``text`` (Telegram HTML) to ``chat_id`` once and classify the outcome.

        ``disable_notification`` sends it silently: the admin's test message uses it
        (D-11). The flag is in the body only when set, so an alert's body stays exactly
        ``chat_id``, ``text`` and ``parse_mode``.
        """
        body: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if disable_notification:
            body["disable_notification"] = True
        result = self._call("sendMessage", json_body=body)
        if result.kind != "ok":
            log.warning("telegram sendMessage: %s (%s)", result.kind, result.code)
        return result

    def send_photo(self, chat_id: int, png: bytes, caption: str) -> SendResult:
        """Post the chart ``png`` with a plain-text ``caption`` to ``chat_id``, silently.

        An ``ok`` result carries the new message's id; an ``ok`` answer without a usable id
        comes back as ``maybe_delivered`` (``no_message_id``), as the photo cannot be recorded.
        """
        result = self._call(
            "sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption, "disable_notification": "true"},
            files={PHOTO_FIELD: (PHOTO_FILENAME, png, _PNG_TYPE)},
            want_message_id=True,
        )
        return _logged("sendPhoto", result)

    def edit_message_media(
        self, chat_id: int, message_id: int, png: bytes, caption: str
    ) -> SendResult:
        """Replace the image and the plain-text caption of chart message ``message_id``.

        The caption sits inside the ``media`` JSON, so both change in one call (D-05).
        """
        _check_message_id(message_id)
        media = {"type": "photo", "media": f"attach://{MEDIA_ATTACH}", "caption": caption}
        result = self._call(
            "editMessageMedia",
            data={
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "media": json.dumps(media, ensure_ascii=False),
            },
            files={MEDIA_ATTACH: (PHOTO_FILENAME, png, _PNG_TYPE)},
            chart=True,
        )
        return _logged("editMessageMedia", result)

    def pin_chat_message(self, chat_id: int, message_id: int) -> SendResult:
        """Pin chart message ``message_id`` in ``chat_id`` without a notification (D-01)."""
        _check_message_id(message_id)
        result = self._call(
            "pinChatMessage",
            json_body={"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
            chart=True,
        )
        return _logged("pinChatMessage", result)

    def unpin_chat_message(self, chat_id: int, message_id: int) -> SendResult:
        """Unpin chart message ``message_id`` in ``chat_id``, and nothing else (D-04).

        The id is always sent: without it Telegram would unpin the most recent pin, which
        may be one the channel admin made (INV-19).
        """
        _check_message_id(message_id)
        result = self._call(
            "unpinChatMessage",
            json_body={"chat_id": chat_id, "message_id": message_id},
            chart=True,
        )
        return _logged("unpinChatMessage", result)

    def _call(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        want_message_id: bool = False,
        chart: bool = False,
    ) -> SendResult:
        """POST one Bot API ``method`` (a JSON body, or ``data`` + ``files`` as multipart)."""
        try:
            # A fresh session per call, so no stale keep-alive connection fails mid-request
            # and every request starts with connect(). No redirects: a redirected POST
            # would be sent a second time.
            with _new_session() as session:
                resp = session.post(
                    f"{self._base}/{method}",
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
        except requests.ConnectTimeout:  # before ConnectionError, which it subclasses
            return SendResult("not_sent", code="connect_timeout")
        except requests.ReadTimeout:  # after connect(), so the request was written
            return SendResult("maybe_delivered", code="read_timeout")
        except requests.ConnectionError as exc:
            code = _not_sent_code(exc)
            if code is not None:
                return SendResult("not_sent", code=code)
            return SendResult("maybe_delivered", code="connection_dropped")
        except requests.RequestException:
            return SendResult("maybe_delivered", code="request_error")
        except Exception as exc:  # never raise across the boundary; the outcome is unknown
            log.warning("telegram %s: unexpected %s", method, type(exc).__name__)
            return SendResult("maybe_delivered", code="unexpected_error")
        return _classify(resp, want_message_id=want_message_id, chart=chart)


def _logged(method: str, result: SendResult) -> SendResult:
    """Log a result other than ``ok`` by method, kind and code only; return it."""
    if result.kind != "ok":
        log.warning("telegram %s: %s (%s)", method, result.kind, result.code)
    return result


def _check_message_id(message_id: object) -> None:
    """An edit, pin or unpin names a stored message: anything but an int above 0 is a bug."""
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("message_id must be an int above 0")


def _not_sent_code(exc: requests.ConnectionError) -> str | None:
    """The code when the connection never opened (refused, DNS, TLS handshake), else None."""
    reason = exc.args[0] if exc.args else None
    if not isinstance(reason, MaxRetryError):
        return None
    if isinstance(reason.reason, _HandshakeError):
        return reason.reason.code
    if isinstance(reason.reason, NewConnectionError):
        return "connect_error"
    return None


def _classify(
    resp: requests.Response, *, want_message_id: bool = False, chart: bool = False
) -> SendResult:
    """Classify an HTTP answer by its status and JSON body. The body is untrusted input.

    With ``want_message_id`` an ``ok`` needs ``result.message_id`` to be an int (not a
    bool) in 1..2**63-1, else the answer is ``maybe_delivered`` / ``no_message_id``. With
    ``chart`` (edit, pin, unpin) a 400 is read by its description first.
    """
    try:
        payload: Any = resp.json()
    except Exception:  # not JSON (an HTML error page, an empty body) or too deep to parse
        payload = None
    data: dict[Any, Any] = payload if isinstance(payload, dict) else {}
    if resp.status_code == 200 and data.get("ok") is True:
        if not want_message_id:
            return SendResult("ok")
        message_id = _message_id(data.get("result"))
        if message_id is None:
            return SendResult("maybe_delivered", code="no_message_id")
        return SendResult("ok", message_id=message_id)
    code = _http_code(data.get("error_code"), default=resp.status_code)
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    if code == 429 or resp.status_code == 429:
        retry_after = _parse_retry_after(
            parameters.get("retry_after"), default=DEFAULT_RETRY_AFTER_S
        )
        return SendResult("rate_limited", retry_after=retry_after, code="429")
    if resp.status_code >= 500:
        return SendResult("transient", code=f"http_{resp.status_code}")
    if chart and code == 400:
        known = _chart_bad_request(data.get("description"))
        if known is not None:
            return known
    return SendResult(
        "permanent",
        code=f"http_{code}",
        migrate_to_chat_id=_chat_id(parameters.get("migrate_to_chat_id")),
    )


def _chart_bad_request(description: object) -> SendResult | None:
    """The result for a 400 on an edit, pin or unpin whose description is known, else None.

    The description is only matched here: it is never logged, stored or returned.
    """
    if not isinstance(description, str):
        return None
    text = description.lower()
    if any(part in text for part in _NOT_MODIFIED):
        return SendResult("ok", code="not_modified")
    if any(part in text for part in _TARGET_MISSING):
        return SendResult("edit_target_missing", code="target_missing")
    return None


def _message_id(result: object) -> int | None:
    """``result.message_id`` of an ok answer if it is a storable id, else None."""
    value = result.get("message_id") if isinstance(result, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _MAX_MESSAGE_ID:
        return value
    return None


def _chat_id(value: object) -> int | None:
    """A chat ID from an answer's ``parameters`` if it is a 64-bit integer (not a bool)."""
    if isinstance(value, int) and not isinstance(value, bool):
        if _MIN_CHAT_ID <= value <= _MAX_CHAT_ID:
            return value
    return None


def _http_code(value: object, *, default: int) -> int:
    """Telegram's ``error_code`` if it is a plausible HTTP status, else ``default``."""
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return default


def _parse_retry_after(value: object, *, default: int) -> int:
    """Whole seconds to wait. Missing, malformed or non-positive values give ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and _RETRY_AFTER_TEXT.fullmatch(value.strip()):
        seconds = int(value.strip())
    else:
        return default
    return seconds if seconds > 0 else default
