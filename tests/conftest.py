"""Shared pytest harness: clock, fake Telegram, location factory, production settings.

Defaults follow docs/v1-lessons.md section 1: heartbeat period 60 s, grace 30 s,
router-reconnect grace off, language en, display TZ Europe/Kyiv.

- Time comes only from an injected clock. Engine code gets ``now`` as an argument, and
  views get a ``FakeClock`` by constructor injection (``SomeView.as_view(clock=...)``).
  There is no freezegun and no monkeypatching of time.
- Telegram is faked at the HTTP boundary (``responses``), and any unregistered URL raises.
  ``FakeTelegram`` answers sendMessage (``accept``, ``fail``, ``answer``) and the calls that
  name a stored message, the four chart calls and deleteMessage (``accept_chart``,
  ``fail_method``, ``answer_method``), records what each accepted
  call carried (``sent``; ``chart_calls``, multipart bodies read back by
  ``parse_multipart``) and counts every request per method (``count``).
- pytest-socket (``--allow-hosts`` in pyproject.toml) fails every real outbound connection
  except the database and localhost.
- Races run on real PostgreSQL with the actor harness below (``Actor``, ``blocked_on_lock``,
  ``wait_for``, ``terminate_backends``): one connection per actor thread, a hook inside the
  transaction instead of ``time.sleep``, and a ``pg_stat_activity`` check that the waiter
  really waits on the row lock (RESEARCH Pitfall 8).
- The admin ops chat is not configured in the test env, so ops notices go to the log;
  ``ops_settings`` configures it with ``OPS_BOT_TOKEN`` and ``OPS_CHAT_ID`` (D-09).
- pytest-django's own sessions run without the web role's 5 s ``statement_timeout``
  (02-03, D-16), every test session keeps it (``django_db_modify_db_settings`` and
  ``django_db_setup`` below).

Test modules import the helper classes directly: ``from conftest import FakeClock``.
"""

import dataclasses
import email.policy
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from typing import Any, NamedTuple

import pytest
import responses
from django.db import connection, connections, transaction
from requests import PreparedRequest

TELEGRAM_API = "https://api.telegram.org"
# A token with the shape the location form accepts (digits, colon, 30+ characters).
DEFAULT_BOT_TOKEN = "123456789:" + "A" * 35
DEFAULT_CHAT_ID = -1001234567890
# The env-configured admin ops chat (D-09); the test env leaves it unset (ops_settings).
OPS_BOT_TOKEN = "555555555:" + "C" * 35
OPS_CHAT_ID = -1005555555555
# check --deploy (security.W009) wants at least 50 characters and 5 distinct ones.
PRODUCTION_SECRET_KEY = ("test-only-not-a-secret-" * 3)[:64]


_STATEMENT_TIMEOUT = re.compile(r"statement_timeout=[0-9]+")


def without_statement_timeout(options: dict[str, Any]) -> dict[str, Any]:
    """A copy of DATABASES OPTIONS whose libpq ``options`` set ``statement_timeout=0``."""
    text = options.get("options", "")
    return {**options, "options": _STATEMENT_TIMEOUT.sub("statement_timeout=0", text)}


@pytest.fixture(scope="session")
def django_db_modify_db_settings(
    django_db_modify_db_settings: None,
) -> Iterator[dict[str, Any]]:
    """Create the test database (and migrate it) without the 5 s statement cap.

    The web role's ``statement_timeout`` (5 s, 02-03 D-16) would also bound pytest-django's
    own sessions. ``CREATE DATABASE`` and ``DROP DATABASE`` on the bind-mounted data
    directory can take longer: a cancelled session-end drop left ``test_powermon`` behind
    for the next session to drop (02-06 deferred item). Yields the capped OPTIONS, which
    ``django_db_setup`` puts back for the tests.
    """
    capped = connection.settings_dict["OPTIONS"]
    connection.settings_dict["OPTIONS"] = without_statement_timeout(capped)
    try:
        yield capped
    finally:
        connection.settings_dict["OPTIONS"] = capped


@pytest.fixture(scope="session")
def django_db_setup(
    django_db_setup: None, django_db_modify_db_settings: dict[str, Any]
) -> Iterator[None]:
    """Tests run with the web cap; the session-end ``DROP DATABASE`` runs without it.

    The setup's sessions are closed, so every test connection opens with the cap again
    (tests/test_db_sessions.py asserts "5s"). pytest-django's teardown runs after this
    fixture's, on a fresh connection built from the uncapped OPTIONS.
    """
    uncapped = connection.settings_dict["OPTIONS"]
    connection.settings_dict["OPTIONS"] = django_db_modify_db_settings
    connections.close_all()
    yield
    connection.settings_dict["OPTIONS"] = uncapped
    connections.close_all()


@pytest.fixture
def fixed_now() -> datetime:
    """A fixed aware UTC instant: 2026-10-01 08:00 UTC (11:00 in Europe/Kyiv)."""
    return datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


class FakeClock:
    """A ``powermon.clock.Clock`` whose time moves only when the test moves it.

    It refuses naive datetimes, so no test can feed naive time into engine code.
    """

    def __init__(self, start: datetime) -> None:
        self._now = _aware_utc(start)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, **kwargs: float) -> None:
        """Move both clocks forward, e.g. ``advance(seconds=90)``."""
        delta = timedelta(**kwargs)
        if delta < timedelta(0):
            raise ValueError("FakeClock.advance() only moves forward; use set() to step back")
        self._now += delta
        self._mono += delta.total_seconds()

    def set(self, dt: datetime) -> None:
        """Step the wall clock to ``dt``. monotonic() is unchanged, as after a real clock step."""
        self._now = _aware_utc(dt)


def _aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("FakeClock needs an aware datetime (tzinfo=UTC), not a naive one")
    return dt.astimezone(UTC)


class ChartCall(NamedTuple):
    """One chart call the fake accepted: the method, the bot, its fields and its files.

    ``fields`` are the multipart text fields (str values) or the JSON body; ``files`` maps
    a multipart file field name to its bytes (empty for a JSON call).
    """

    method: str
    token: str
    fields: dict[str, Any]
    files: dict[str, bytes]


def parse_multipart(request: PreparedRequest) -> tuple[dict[str, str], dict[str, bytes]]:
    """The text fields (UTF-8) and the file contents of a multipart/form-data request."""
    body = request.body
    if not isinstance(body, bytes):
        raise TypeError("parse_multipart() needs a multipart request with a bytes body")
    content_type = request.headers["Content-Type"].encode("ascii")
    message: Any = BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type + b"\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if part.get_filename() is None:
            fields[name] = payload.decode("utf-8")
        else:
            files[name] = payload
    return fields, files


class FakeTelegram:
    """The Telegram Bot API, faked at the HTTP boundary with ``responses``.

    ``accept(token)`` answers that bot's sendMessage calls with ok and records each JSON
    body in ``sent`` (the n-th accepted message gets the message id n); ``fail(token, ...)``
    answers them with an HTTP error or raises an exception. ``accept_chart(token)`` answers
    that bot's chart calls and its deleteMessage calls (the calls that name a stored
    message, ``CHART_METHODS``) with ok and records each one in ``chart_calls``: sendPhoto
    answers the message ids 1001, 1002, ..., editMessageMedia the edited message,
    pinChatMessage, unpinChatMessage and deleteMessage ``true``.
    ``fail_method`` and ``answer_method`` are ``fail`` and ``answer`` for one method;
    ``count(token, method)`` counts every request, failed ones included. Several
    registrations for one URL answer in registration order and the last one repeats, so
    ``fail_method`` before ``accept_chart`` fails the first call only. A call to any bot or
    method that was not registered raises ``requests.ConnectionError``.
    """

    API = TELEGRAM_API
    CHART_METHODS = (
        "sendPhoto",
        "editMessageMedia",
        "pinChatMessage",
        "unpinChatMessage",
        "deleteMessage",
    )
    # The chart calls with a multipart body (a PNG upload); the others send JSON.
    MULTIPART_METHODS = ("sendPhoto", "editMessageMedia")
    FIRST_PHOTO_ID = 1001

    def __init__(self, rsps: responses.RequestsMock) -> None:
        self.rsps = rsps
        self.sent: list[dict[str, Any]] = []
        self.chart_calls: list[ChartCall] = []
        self._next_photo_id = self.FIRST_PHOTO_ID

    @property
    def calls(self) -> Any:
        """Every request the fake received, in order (``responses`` CallList)."""
        return self.rsps.calls

    def accept(self, token: str) -> None:
        def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
            self.sent.append(json.loads(request.body or b"{}"))
            body = {"ok": True, "result": {"message_id": len(self.sent)}}
            return 200, {}, json.dumps(body)

        self.rsps.add_callback(
            responses.POST,
            self._url(token),
            callback=callback,
            content_type="application/json",
        )

    def fail(
        self,
        token: str,
        *,
        status: int | None = None,
        json_body: Any = None,
        exc: BaseException | None = None,
    ) -> None:
        """Answer with ``status`` (and ``json_body``), or raise ``exc`` from the transport."""
        if exc is not None:
            self.rsps.add(responses.POST, self._url(token), body=exc)
        elif status is not None:
            self.rsps.add(responses.POST, self._url(token), status=status, json=json_body)
        else:
            raise TypeError("FakeTelegram.fail() needs status= or exc=")

    def answer(
        self,
        token: str,
        during: Callable[[], None],
        *,
        status: int = 200,
        json_body: Any = None,
        exc: BaseException | None = None,
    ) -> None:
        """Answer the bot's next sendMessage call only after ``during`` has run.

        ``during`` runs while the request is in flight: ``lambda: clock.advance(seconds=10)``
        is a send that takes 10 s, ``stop.set`` is a SIGTERM that arrives mid-send. The call
        then raises ``exc``, or answers ``status`` with ``json_body``, or (200 and no body)
        accepts the message as ``accept`` does.
        """

        def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
            during()
            if exc is not None:
                raise exc
            body = json_body
            if status == 200 and body is None:
                self.sent.append(json.loads(request.body or b"{}"))
                body = {"ok": True, "result": {"message_id": len(self.sent)}}
            return status, {}, json.dumps(body)

        self.rsps.add_callback(
            responses.POST,
            self._url(token),
            callback=callback,
            content_type="application/json",
        )

    def accept_chart(self, token: str) -> None:
        """Answer the bot's chart calls with ok and record each one in ``chart_calls``."""
        for method in self.CHART_METHODS:
            self.rsps.add_callback(
                responses.POST,
                self._url(token, method),
                callback=self._chart_callback(token, method),
                content_type="application/json",
            )

    def fail_method(
        self,
        token: str,
        method: str,
        *,
        status: int | None = None,
        json_body: Any = None,
        exc: BaseException | None = None,
    ) -> None:
        """``fail`` for one Bot API method: answer ``status`` (and ``json_body``) or raise."""
        if exc is not None:
            self.rsps.add(responses.POST, self._url(token, method), body=exc)
        elif status is not None:
            self.rsps.add(responses.POST, self._url(token, method), status=status, json=json_body)
        else:
            raise TypeError("FakeTelegram.fail_method() needs status= or exc=")

    def answer_method(
        self,
        token: str,
        method: str,
        during: Callable[[], None],
        *,
        status: int = 200,
        json_body: Any = None,
        exc: BaseException | None = None,
    ) -> None:
        """``answer`` for one chart method: run ``during``, then answer.

        The call raises ``exc``, or answers ``status`` with ``json_body``, or (200 and no
        body) is accepted and recorded as ``accept_chart`` does.
        """
        if method not in self.CHART_METHODS:
            raise ValueError(f"answer_method() fakes the chart calls; use answer() for {method}")

        def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
            during()
            if exc is not None:
                raise exc
            body = json_body
            if status == 200 and body is None:
                body = self._accept_chart_call(token, method, request)
            return status, {}, json.dumps(body)

        self.rsps.add_callback(
            responses.POST,
            self._url(token, method),
            callback=callback,
            content_type="application/json",
        )

    def count(self, token: str, method: str) -> int:
        """How many requests reached the bot's ``method``, failed ones included."""
        url = self._url(token, method)
        return sum(1 for call in self.rsps.calls if call.request.url == url)

    def _chart_callback(
        self, token: str, method: str
    ) -> Callable[[PreparedRequest], tuple[int, dict[str, str], str]]:
        def callback(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
            return 200, {}, json.dumps(self._accept_chart_call(token, method, request))

        return callback

    def _accept_chart_call(
        self, token: str, method: str, request: PreparedRequest
    ) -> dict[str, Any]:
        """Record one accepted chart call and return Telegram's ok body for it."""
        fields: dict[str, Any]
        files: dict[str, bytes]
        if method in self.MULTIPART_METHODS:
            text_fields, files = parse_multipart(request)
            fields = dict(text_fields)
        else:
            fields, files = json.loads(request.body or b"{}"), {}
        self.chart_calls.append(ChartCall(method, token, fields, files))
        if method == "sendPhoto":
            message_id = self._next_photo_id
            self._next_photo_id += 1
            return {"ok": True, "result": {"message_id": message_id}}
        if method == "editMessageMedia":
            return {"ok": True, "result": {"message_id": int(fields["message_id"])}}
        return {"ok": True, "result": True}

    def _url(self, token: str, method: str = "sendMessage") -> str:
        return f"{self.API}/bot{token}/{method}"


@pytest.fixture
def fake_telegram() -> Iterator[FakeTelegram]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeTelegram(rsps)


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``predicate`` every 0.02 s until it is true or ``timeout`` seconds have passed.

    Returns the predicate's last value, so ``assert wait_for(...)`` fails on a timeout.
    Measured on ``time.monotonic()``: it is for real threads, not for the injected clock.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Actor(threading.Thread):
    """Run ``fn`` in a daemon thread on its own Django connection (a race participant).

    Before ``fn`` runs, the thread records its PostgreSQL backend pid in ``pid`` and names
    its session ``APPLICATION_NAME``, so a test can check ``blocked_on_lock(actor.pid)`` and,
    if an actor is stuck after a failed assertion, end it with ``terminate_backends``. The
    return value lands in ``result`` and any exception (BaseException) in ``exc``. The
    connection is always closed at the end, or pytest-django could not truncate or drop
    the test database.
    """

    APPLICATION_NAME = "powermon-test-actor"

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self.fn = fn
        self.result: Any = None
        self.exc: BaseException | None = None
        self.pid: int | None = None

    def run(self) -> None:
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_backend_pid(), set_config('application_name', %s, false)",
                    [self.APPLICATION_NAME],
                )
                self.pid = cur.fetchone()[0]
            self.result = self.fn()
        except BaseException as exc:
            self.exc = exc
        finally:
            connection.close()


def blocked_on_lock(pid: int) -> bool:
    """True while the backend ``pid`` waits for a lock (``pg_stat_activity``)."""
    with connection.cursor() as cur:
        cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pid])
        row = cur.fetchone()
    return row is not None and row[0] == "Lock"


def terminate_backends(application_name: str) -> int:
    """End every session of this test database named ``application_name``; return how many.

    The way to drop a stuck actor's transaction (so teardown never waits on it), or to
    kill a worker's lease session from outside as a DB restart would.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pid FROM pg_stat_activity "
            "WHERE application_name = %s AND datname = current_database()",
            [application_name],
        )
        pids = [row[0] for row in cur.fetchall()]
        for pid in pids:
            cur.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
    return len(pids)


@pytest.fixture
def location_factory(fixed_now: datetime) -> Callable[..., Any]:
    """``make(**overrides) -> Location`` plus its LocationState in status "waiting".

    The factory does not request the ``db`` fixture: each test carries its own
    ``django_db`` mark.
    """

    def make(**overrides: Any) -> Any:
        # Imported here, not at module level: these modules arrive after this harness (01-03).
        from powermon.engine.models import LocationState
        from powermon.locations.keys import generate_device_key
        from powermon.locations.models import Location

        fields: dict[str, Any] = {
            "name": "Test location",
            "period_s": 60,
            "grace_s": 30,
            "router_grace": False,
            "language": "en",
            "bot_token": DEFAULT_BOT_TOKEN,
            "chat_id": DEFAULT_CHAT_ID,
            "created_at": fixed_now,
            **overrides,
        }
        if "device_key" not in fields:
            fields["device_key"] = generate_device_key()
        with transaction.atomic():
            location = Location.objects.create(**fields)
            LocationState.objects.create(location=location, status="waiting")
        return location

    return make


@pytest.fixture
def ops_settings(settings: Any) -> Any:
    """``settings.CFG`` with the admin ops chat configured (OPS_BOT_TOKEN + OPS_CHAT_ID, D-09).

    Code reads ``settings.CFG`` at call time, so the override applies to the whole test and
    pytest-django restores the original afterwards. Without this fixture the test env has
    no ops chat, and ops notices go to the log.
    """
    settings.CFG = dataclasses.replace(
        settings.CFG, ops_bot_token=OPS_BOT_TOKEN, ops_chat_id=OPS_CHAT_ID
    )
    return settings


@pytest.fixture
def production_settings(settings: Any) -> Any:
    """Django settings as APP_ENV=production builds them (SEC-02, INV-22)."""
    settings.DEBUG = False
    settings.SESSION_COOKIE_SECURE = True
    settings.CSRF_COOKIE_SECURE = True
    settings.SECURE_HSTS_SECONDS = 31_536_000
    settings.SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    settings.SECURE_HSTS_PRELOAD = False
    settings.SECRET_KEY = PRODUCTION_SECRET_KEY
    settings.ALLOWED_HOSTS = ["testserver", "power.example.org", "127.0.0.1"]
    return settings
