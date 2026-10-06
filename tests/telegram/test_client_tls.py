"""TelegramClient over real loopback sockets: "not sent" ends where the request starts.

The ``fake_telegram`` tests replace requests' transport, so they never see where urllib3
draws the line between opening a connection and writing the request. These tests run the
real requests/urllib3 stack against a server on 127.0.0.1 (pytest-socket allows loopback):

- A failure before any request byte is written (TCP connect, TLS handshake, including a
  handshake that times out) is ``not_sent``, which the relay retries (D-14, INV-16).
- A failure after the request was written (no answer, connection closed) is
  ``maybe_delivered``, which is never resent (INV-16).

urllib3 2.8 reports a handshake timeout as ``ReadTimeoutError`` and a handshake failure as
``SSLError``: the same types it uses once the request is on the wire (Wave 3 audit F1).
Every failing case also checks that the server saw exactly one connection, so the client
never retries on its own (retry timing belongs to the outbox, INV-15/16).
"""

import json
import socket
import ssl
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID

from powermon.telegram.client import SendResult, TelegramClient

TOKEN = DEFAULT_BOT_TOKEN
TEXT = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
# Short client timeouts keep the stall cases fast; the classification does not depend on them.
TIMEOUT = (1.0, 1.0)
# How long a stalling server holds a connection open: longer than any client timeout.
HOLD_S = 5.0
TLS_HANDSHAKE_RECORD = 0x16

Handler = Callable[[socket.socket, "LoopbackServer"], None]


@dataclass(frozen=True)
class Certificate:
    cert: Path
    key: Path


class LoopbackServer:
    """Accepts connections on 127.0.0.1 in a thread and hands each one to ``handler``."""

    def __init__(self, handler: Handler, tls: ssl.SSLContext | None) -> None:
        self.handler = handler
        self.tls = tls
        self.connections = 0
        self.received = bytearray()
        self.stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.05)
        self.port: int = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            self.connections += 1
            with conn:
                conn.settimeout(HOLD_S)
                try:
                    self.handler(conn, self)
                except OSError:  # ssl.SSLError is an OSError: the client gave up
                    pass

    def accept_tls(self, conn: socket.socket) -> ssl.SSLSocket:
        assert self.tls is not None, "this handler needs a server started with tls="
        return self.tls.wrap_socket(conn, server_side=True)

    def close(self) -> None:
        self.stop.set()
        self._thread.join(timeout=HOLD_S + 1)
        self._listener.close()


def _read_request(stream: socket.socket) -> bytes:
    """One HTTP request: the head up to the blank line, then Content-Length body bytes."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = stream.recv(65536)
        if not chunk:
            return bytes(data)
        data += chunk
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = stream.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


# Server behaviours


def stall_before_handshake(conn: socket.socket, server: LoopbackServer) -> None:
    """Accept TCP, read the ClientHello, never answer it."""
    server.received += conn.recv(65536)
    server.stop.wait(HOLD_S)


def answer_in_plaintext(conn: socket.socket, server: LoopbackServer) -> None:
    """Answer the ClientHello with plain HTTP, which breaks the handshake."""
    server.received += conn.recv(65536)
    conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")


def answer_ok(conn: socket.socket, server: LoopbackServer) -> None:
    """Complete TLS, read the request and answer like Telegram does on success."""
    with server.accept_tls(conn) as tls:
        server.received += _read_request(tls)
        body = json.dumps({"ok": True, "result": {"message_id": 1}}).encode()
        head = (
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        tls.sendall(head.encode() + body)


def stall_after_request(conn: socket.socket, server: LoopbackServer) -> None:
    """Complete TLS, read the whole request, never answer it."""
    with server.accept_tls(conn) as tls:
        server.received += _read_request(tls)
        server.stop.wait(HOLD_S)


def close_after_request(conn: socket.socket, server: LoopbackServer) -> None:
    """Complete TLS, read the whole request, close without an answer."""
    with server.accept_tls(conn) as tls:
        server.received += _read_request(tls)


# Fixtures


@pytest.fixture(scope="session")
def loopback_cert(tmp_path_factory: pytest.TempPathFactory) -> Certificate:
    """A throwaway self-signed certificate for 127.0.0.1, made with the image's openssl."""
    directory = tmp_path_factory.mktemp("tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-noenc",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return Certificate(cert, key)


@pytest.fixture(scope="session")
def server_tls(loopback_cert: Certificate) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(loopback_cert.cert, loopback_cert.key)
    return context


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch, loopback_cert: Certificate) -> None:
    """The client trusts the loopback certificate (requests reads REQUESTS_CA_BUNDLE)."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(loopback_cert.cert))


@pytest.fixture
def untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client uses its default CA bundle, which does not hold the loopback certificate."""
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)


@pytest.fixture
def serve(server_tls: ssl.SSLContext) -> Iterator[Callable[[Handler], LoopbackServer]]:
    servers: list[LoopbackServer] = []

    def start(handler: Handler) -> LoopbackServer:
        server = LoopbackServer(handler, server_tls)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _send(port: int) -> SendResult:
    client = TelegramClient(TOKEN, api_base=f"https://127.0.0.1:{port}", timeout=TIMEOUT)
    return client.send_message(DEFAULT_CHAT_ID, TEXT)


# Before the request: not sent, safe to retry (D-14, INV-16)


@pytest.mark.usefixtures("trusted")
def test_tls_handshake_timeout_is_not_sent(serve: Callable[[Handler], LoopbackServer]) -> None:
    # TCP connects, then the handshake stalls past the connect timeout. urllib3 reports
    # this as a read timeout, but no request byte was written.
    server = serve(stall_before_handshake)

    result = _send(server.port)

    assert result == SendResult("not_sent", code="tls_handshake_timeout")
    assert server.received[0] == TLS_HANDSHAKE_RECORD  # the client got as far as ClientHello
    assert b"sendMessage" not in server.received
    assert server.connections == 1


@pytest.mark.parametrize(
    ("handler", "trust"),
    [(answer_in_plaintext, "trusted"), (answer_ok, "untrusted")],
    ids=["plaintext_answer", "untrusted_certificate"],
)
def test_tls_handshake_failure_is_not_sent(
    request: pytest.FixtureRequest,
    serve: Callable[[Handler], LoopbackServer],
    handler: Handler,
    trust: str,
) -> None:
    # A broken handshake or a certificate the client rejects: requests raises SSLError,
    # the same type as a TLS error after sending, but the request never left the client.
    request.getfixturevalue(trust)
    server = serve(handler)

    result = _send(server.port)

    assert result == SendResult("not_sent", code="tls_handshake_error")
    assert b"sendMessage" not in server.received
    assert server.connections == 1


def test_refused_connection_is_not_sent() -> None:
    # A real refused TCP connect: a port that had a listener a moment ago and has none now.
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]

    assert _send(port) == SendResult("not_sent", code="connect_error")


# After the request: maybe delivered, never resent (INV-16)


@pytest.mark.usefixtures("trusted")
def test_ok_over_real_tls(serve: Callable[[Handler], LoopbackServer]) -> None:
    server = serve(answer_ok)

    result = _send(server.port)

    assert result == SendResult("ok", message_id=1)
    head, _, body = bytes(server.received).partition(b"\r\n\r\n")
    assert head.startswith(f"POST /bot{TOKEN}/sendMessage HTTP/1.1".encode())
    assert json.loads(body) == {"chat_id": DEFAULT_CHAT_ID, "text": TEXT, "parse_mode": "HTML"}
    assert server.connections == 1


@pytest.mark.usefixtures("trusted")
def test_no_answer_after_the_request_is_maybe_delivered(
    serve: Callable[[Handler], LoopbackServer],
) -> None:
    # Telegram may have accepted the message and the answer was lost: never resend.
    server = serve(stall_after_request)

    result = _send(server.port)

    assert result == SendResult("maybe_delivered", code="read_timeout")
    _, _, body = bytes(server.received).partition(b"\r\n\r\n")
    assert json.loads(body)["chat_id"] == DEFAULT_CHAT_ID  # the whole request arrived
    assert server.connections == 1


@pytest.mark.usefixtures("trusted")
def test_connection_closed_after_the_request_is_maybe_delivered(
    serve: Callable[[Handler], LoopbackServer],
) -> None:
    server = serve(close_after_request)

    result = _send(server.port)

    assert result == SendResult("maybe_delivered", code="connection_dropped")
    assert b"sendMessage" in server.received
    assert server.connections == 1
