"""Shared pytest harness: clock, fake Telegram, location factory, production settings.

Defaults follow docs/v1-lessons.md section 1: heartbeat period 60 s, grace 30 s,
router-reconnect grace off, language en, display TZ Europe/Kyiv.

- Time comes only from an injected clock. Engine code gets ``now`` as an argument, and
  views get a ``FakeClock`` by constructor injection (``SomeView.as_view(clock=...)``).
  There is no freezegun and no monkeypatching of time.
- Telegram is faked at the HTTP boundary (``responses``), and any unregistered URL raises.
- pytest-socket (``--allow-hosts`` in pyproject.toml) fails every real outbound connection
  except the database and localhost.

Test modules import the helper classes directly: ``from conftest import FakeClock``.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import responses
from django.db import transaction
from requests import PreparedRequest

TELEGRAM_API = "https://api.telegram.org"
# A token with the shape the location form accepts (digits, colon, 30+ characters).
DEFAULT_BOT_TOKEN = "123456789:" + "A" * 35
DEFAULT_CHAT_ID = -1001234567890
# check --deploy (security.W009) wants at least 50 characters and 5 distinct ones.
PRODUCTION_SECRET_KEY = ("test-only-not-a-secret-" * 3)[:64]


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


class FakeTelegram:
    """The Telegram Bot API, faked at the HTTP boundary with ``responses``.

    ``accept(token)`` answers that bot's sendMessage calls with ok and records each JSON
    body in ``sent``; ``fail(token, ...)`` answers them with an HTTP error or raises an
    exception. A call to any bot that was not registered raises ``requests.ConnectionError``.
    """

    API = TELEGRAM_API

    def __init__(self, rsps: responses.RequestsMock) -> None:
        self.rsps = rsps
        self.sent: list[dict[str, Any]] = []

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

    def _url(self, token: str) -> str:
        return f"{self.API}/bot{token}/sendMessage"


@pytest.fixture
def fake_telegram() -> Iterator[FakeTelegram]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeTelegram(rsps)


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
