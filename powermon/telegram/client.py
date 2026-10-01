"""Telegram Bot API client: ``sendMessage`` only in Phase 1.

``send_message`` never raises across its boundary. Every outcome comes back as a
``SendResult`` whose ``code`` is a short fixed string, never the token or a URL:

- ``ok``: Telegram accepted the message.
- ``not_sent``: no request byte left the client (connect timeout, refused connection, DNS
  failure, a TLS handshake that failed or timed out). Safe to retry.
- ``maybe_delivered``: the request may have reached Telegram (read timeout after the
  request was written, connection dropped after sending, a TLS error after the handshake,
  any other transport error). Never resent: alerts are at-most-once after an ambiguous
  send (INV-16).
- ``rate_limited``: HTTP 429; ``retry_after`` is the wait in whole seconds.
- ``transient``: HTTP 5xx.
- ``permanent``: any other answer (400, 401, 403, 404, ...).

Where "not sent" ends: urllib3 opens the connection (TCP and the TLS handshake) before it
writes the request, but it reports a handshake timeout as a read timeout and a handshake
failure as an SSLError, the same types it uses once the request is on the wire. So HTTPS
sends go through ``_ConnectPhaseConnection``, whose ``connect()`` re-raises any handshake
failure as a ``NewConnectionError`` subclass. Every send uses a fresh session, so every
request starts with ``connect()``.

The token sits in the URL path, and requests puts that URL into the text of connect
errors and of ``raise_for_status()`` (STACK G3, P-12). So this module never calls
``raise_for_status()``, never logs the URL or an exception's text, and logs only the kind
and code. The adapter keeps requests' default of no retries, and nothing sleeps: all retry
timing belongs to the outbox relay (INV-15/16). Every call passes explicit (connect, read)
timeouts, because requests has no default timeout.
"""

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

SendKind = Literal["ok", "not_sent", "maybe_delivered", "rate_limited", "transient", "permanent"]

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 10.0)
# The wait used when a 429 carries no usable retry_after (STACK G13).
DEFAULT_RETRY_AFTER_S = 30

_RETRY_AFTER_TEXT = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True)
class SendResult:
    """The outcome of one send. ``code`` is short and never holds the token or a URL."""

    kind: SendKind
    retry_after: int | None = None
    code: str = ""


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
    """Sends messages for one bot.

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
        self._send_url = f"{api_base}/bot{token}/sendMessage"
        self._timeout = timeout

    def __repr__(self) -> str:
        return "TelegramClient(<token hidden>)"

    def send_message(self, chat_id: int, text: str) -> SendResult:
        """Send ``text`` (Telegram HTML) to ``chat_id`` once and classify the outcome."""
        result = self._post({"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        if result.kind != "ok":
            log.warning("telegram sendMessage: %s (%s)", result.kind, result.code)
        return result

    def _post(self, body: dict[str, Any]) -> SendResult:
        try:
            # A fresh session per send, so no stale keep-alive connection fails mid-request
            # and every request starts with connect(). No redirects: a redirected POST
            # would be sent a second time.
            with _new_session() as session:
                resp = session.post(
                    self._send_url, json=body, timeout=self._timeout, allow_redirects=False
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
            log.warning("telegram sendMessage: unexpected %s", type(exc).__name__)
            return SendResult("maybe_delivered", code="unexpected_error")
        return _classify(resp)


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


def _classify(resp: requests.Response) -> SendResult:
    """Classify an HTTP answer by its status and JSON body. The body is untrusted input."""
    try:
        payload: Any = resp.json()
    except ValueError:  # not JSON: an HTML error page, an empty body
        payload = None
    data: dict[Any, Any] = payload if isinstance(payload, dict) else {}
    if resp.status_code == 200 and data.get("ok") is True:
        return SendResult("ok")
    code = _http_code(data.get("error_code"), default=resp.status_code)
    if code == 429 or resp.status_code == 429:
        parameters = data.get("parameters")
        raw = parameters.get("retry_after") if isinstance(parameters, dict) else None
        retry_after = _parse_retry_after(raw, default=DEFAULT_RETRY_AFTER_S)
        return SendResult("rate_limited", retry_after=retry_after, code="429")
    if resp.status_code >= 500:
        return SendResult("transient", code=f"http_{resp.status_code}")
    return SendResult("permanent", code=f"http_{code}")


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
