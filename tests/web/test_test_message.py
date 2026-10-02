"""The admin's test message (LOC-07, D-11, D-12; INV-16 #1 end to end; UI-SPEC screen B).

- D-11: "Send test message" on the location page is a POST (CSRF) that makes exactly one
  ``sendMessage`` with the location's current token and chat: the fixed text in the
  location's language, sent silently (``disable_notification: true``). It is never
  retried, never queued in the outbox and sent whatever the switches say. The view then
  redirects to the location page with the result's flash (UI-D4), so a reload never sends
  it again.
- D-12: after a success, one transaction makes the location's queued alerts due at the
  view clock's time and closes an open ``delivery_failing`` incident with one recovery
  notice. A failed test message never opens the incident (D-10).
- INV-16 #1 (docs/v1-lessons.md), end to end: a 403 on an alert gives 1 attempt and 1
  failing notice. After the bot is fixed, a successful test message clears the badge,
  sends 1 recovery notice, and the queued alert goes out in the worker's next pass with
  its event time, still inside the 15-minute hold the 403 earned.

Tests that run the relay are ``django_db(transaction=True)``. One FakeClock drives the
relay and the view (``SendTestMessageView.as_view(clock=...)`` through RequestFactory, the
LOC-02 injection pattern); Telegram is faked at the HTTP boundary (``fake_telegram``), and
``ops_settings`` configures the admin chat.
"""

import dataclasses
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any

import pytest
import requests
import responses
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    OPS_CHAT_ID,
    TELEGRAM_API,
    FakeClock,
    FakeTelegram,
)
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory
from urllib3.exceptions import MaxRetryError, NewConnectionError

from powermon.alerts import delivery, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.locations.models import Location
from powermon.telegram.client import SendResult
from powermon.web.location_views import (
    LocationDetailView,
    SendTestMessageView,
    flash_for_test_message,
)
from powermon.web.views import LocationListView
from powermon.worker import io_loop

User = get_user_model()

# The OFF alert is recorded (and so due) at 10:06:31 UTC, 13:06:31 in Kyiv; its outage
# started 91 s earlier, at 13:05.
T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
# An admin-typed name with HTML in it: the notices escape it (Telegram HTML).
NAME = "Office <1> & Co"
ESCAPED = "Office &lt;1&gt; &amp; Co"
KICKED = {
    "ok": False,
    "error_code": 403,
    "description": "Forbidden: bot was kicked from the channel chat",
}
RESTORED = f"✅ Alerts for {ESCAPED} are delivered again."
# Sent 5 minutes after it was recorded (past LATE_AFTER), so it states its event time.
LATE_OFF = "🔴 13:05 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
# The test message texts, verbatim from 04-CONTEXT.md D-11.
D11_TEST_TEXTS = {
    "uk": "🔧 Тестове повідомлення Power Monitor: бот може публікувати тут.",
    "en": "🔧 Power Monitor test message: the bot can post here.",
    "ru": "🔧 Тестовое сообщение Power Monitor: бот может публиковать здесь.",
}
# UI-SPEC Copywriting › Test message, verbatim.
SENT_FLASH = "Test message sent. Check that it arrived in the channel."
RECOVERED_FLASH = (
    "Test message sent. Delivery is marked OK again, and any queued alerts go out next."
)
NOT_IN_CHAT_FLASH = (
    "Test message not sent ({code}): the bot is not in the chat, or the chat was not found. "
    "Make the bot an admin of the channel and check the chat ID."
)
BOT_REJECTED_FLASH = (
    "Test message not sent ({code}): Telegram rejected the bot token. Paste the current token "
    "from @BotFather in Edit location."
)
REFUSED_FLASH = (
    "Test message not sent ({code}): Telegram refused it. Check the bot token and the chat ID."
)
MAYBE_FLASH = (
    "No answer from Telegram in time ({code}). The message may have been sent: check the "
    "channel before you try again."
)
UNREACHABLE_FLASH = (
    "Telegram could not be reached ({code}), so the test message was not sent. Try again in a "
    "minute."
)
RATE_LIMITED_FLASH = "Telegram asks to wait before the next message. Try again in {wait}."
# Telegram's description texts below must never reach a flash (D-11, OPS-08).
DESCRIPTIONS = (
    "Forbidden: bot was kicked from the channel chat",
    "Bad Request: chat not found",
    "Unauthorized",
    "Not Found",
    "Conflict: terminated by other request",
    "Service Unavailable",
    "Too Many Requests: retry after",
)


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    """Event times in the expected texts are Kyiv times, whatever the env says."""
    settings.CFG = dataclasses.replace(settings.CFG, display_tz="Europe/Kyiv")
    settings.TIME_ZONE = "Europe/Kyiv"
    return settings


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _url(location: Any) -> str:
    return f"/locations/{location.pk}/test-message/"


def _queue(location: Any, at: datetime = T0) -> OutboxMessage:
    """One OFF alert recorded (and so due) at ``at``, as a transition would queue it."""
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - timedelta(seconds=91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


def _row(message: OutboxMessage) -> OutboxMessage:
    return OutboxMessage.objects.get(pk=message.pk)


def _body(text: str, chat_id: int = DEFAULT_CHAT_ID) -> dict[str, Any]:
    return {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}


def _test_body(lang: str, chat_id: int = DEFAULT_CHAT_ID) -> dict[str, Any]:
    """The JSON body of the test message: an alert's keys plus the silent flag (D-11)."""
    return {**_body(D11_TEST_TEXTS[lang], chat_id), "disable_notification": True}


def _calls_to(fake: FakeTelegram, token: str) -> int:
    return len([call for call in fake.calls if f"/bot{token}/" in call.request.url])


def _ops_rows(kind: str) -> list[OutboxMessage]:
    rows = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS, kind=kind)
    return list(rows.order_by("id"))


def _incidents(location: Any) -> list[tuple[datetime, datetime | None]]:
    rows = OpsIncident.objects.filter(kind=delivery.KIND_DELIVERY_FAILING, location=location)
    return [(row.started_at, row.ended_at) for row in rows.order_by("id")]


def _post_test_message(
    rf: RequestFactory, clock: FakeClock, location: Any
) -> tuple[HttpResponse, list[tuple[int, str]]]:
    """POST the test message through the view with the injected clock; its flashes."""
    request = rf.post(_url(location))
    request.session = SessionStore()
    storage = FallbackStorage(request)
    request._messages = storage  # type: ignore[attr-defined]
    response = SendTestMessageView.as_view(clock=clock)(request, pk=location.pk)
    return response, [(message.level, message.message) for message in storage]


def _flashes(html: str) -> list[tuple[str, str]]:
    """Each flash on a page as (role, text)."""
    return [
        (role, unescape(text).strip())
        for role, text in re.findall(r'role="(status|alert)">([^<]*)<', html)
    ]


def _get(rf: RequestFactory, view: Callable[..., HttpResponse], path: str, **kwargs: Any) -> str:
    """A page rendered by ``view`` for a GET of ``path`` (RequestFactory, no middleware)."""
    request = rf.get(path)
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    response = view(request, **kwargs)
    assert response.status_code == 200
    return response.content.decode()


def _list_delivery(rf: RequestFactory, clock: FakeClock) -> str:
    """The Delivery cell of the list's only row, rendered at the clock's time."""
    html = _get(rf, LocationListView.as_view(clock=clock), "/")
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", html, re.S)
    assert len(cells) == 4, "the list has one row of four cells"
    return cells[3]


def _page_delivery(rf: RequestFactory, location: Any) -> str:
    """The location page's Delivery value, its help lines dropped."""
    html = _get(rf, LocationDetailView.as_view(), f"/locations/{location.pk}/", pk=location.pk)
    row = re.search(r"<dt>Delivery</dt>\s*<dd>(.*?)</dd>", html, re.S)
    assert row is not None, "no Delivery row on the location page"
    return re.sub(r'<p class="help">.*?</p>', "", row.group(1), flags=re.S).strip()


def _error(status: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


def _refused(token: str) -> requests.ConnectionError:
    # A real connect-phase exception carries the URL, and so the token, in its text (P-12).
    path = f"/bot{token}/sendMessage"
    reason = NewConnectionError(None, f"Failed to establish a new connection for {path}")
    return requests.ConnectionError(MaxRetryError(None, path, reason))


# INV-16 #1 end to end (D-12): the test message releases a failing location's queue


@pytest.mark.django_db(transaction=True)
def test_INV16_1_test_message_clears_failing_and_releases_the_queue(
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    ops_settings: Any,
) -> None:
    clock = FakeClock(T0)
    location = location_factory(name=NAME)
    off = _queue(location)
    # The bot was removed from the channel, so the alert gets a 403; once it is back,
    # every later call is accepted (the test message, then the alert).
    fake_telegram.fail(DEFAULT_BOT_TOKEN, status=403, json_body=KICKED)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True

    # One attempt, the location is failing, and the admin got one notice.
    assert _calls_to(fake_telegram, DEFAULT_BOT_TOKEN) == 1
    assert (_row(off).status, _row(off).next_attempt_at) == ("pending", T0 + timedelta(minutes=15))
    assert _incidents(location) == [(T0, None)]
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)) == 1
    # The badge: the list's Delivery cell and the location page's Delivery row (D-13).
    assert _list_delivery(rf, clock) == (
        '<span class="status status--failing">Failing since 13:06 (http_403)</span>'
    )
    assert _page_delivery(rf, location) == (
        '<span class="status status--failing">'
        "Failing since 2026-10-01 13:06:31 EEST (http_403)</span>"
    )

    # Five minutes later the 403's 15-minute hold is still in force: nothing is sent.
    clock.advance(minutes=5)
    tested_at = clock.now()
    assert io_loop.run_iteration(clock, state) is False
    assert _calls_to(fake_telegram, DEFAULT_BOT_TOKEN) == 1

    response, flashes = _post_test_message(rf, clock, location)

    assert response.status_code == 302
    assert response["Location"] == f"/locations/{location.pk}/"
    assert flashes == [(messages.SUCCESS, RECOVERED_FLASH)]
    # One silent sendMessage with the location's token and chat, the en D-11 text.
    assert _calls_to(fake_telegram, DEFAULT_BOT_TOKEN) == 2
    assert fake_telegram.sent[-1] == _test_body("en")
    # D-12, one transaction at the view clock's time: the incident closed with exactly one
    # recovery notice, and the held alert is due now.
    assert _incidents(location) == [(T0, tested_at)]
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)) == 1
    assert _row(off).next_attempt_at == tested_at
    # The badge clears on both pages.
    assert _list_delivery(rf, clock) == "OK"
    assert _page_delivery(rf, location) == "OK"

    # The worker's next pass, still at the test time (10 minutes before the hold would
    # end), sends the OFF with its event time, then the recovery notice to the admin.
    assert io_loop.run_iteration(clock, state) is True

    assert _row(off).status == "sent"
    assert fake_telegram.sent[-2:] == [_body(LATE_OFF), _body(RESTORED, OPS_CHAT_ID)]
    assert _calls_to(fake_telegram, DEFAULT_BOT_TOKEN) == 3
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_FAILING)) == 1
    assert len(_ops_rows(outbox.KIND_OPS_DELIVERY_RESTORED)) == 1
    # The test message itself never became an outbox row: only the alert and the notices.
    assert OutboxMessage.objects.filter(channel=outbox.CHANNEL_SUBSCRIBER).count() == 1


# D-11: one silent call, never an alert


@pytest.mark.django_db
@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_LOC07_test_message_is_one_silent_call(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram, lang: str
) -> None:
    token = "246813579:" + "Tk_-9" * 7
    chat_id = -1009876543210
    location = location_factory(name="Office", bot_token=token, chat_id=chat_id, language=lang)
    fake_telegram.accept(token)

    response = admin.post(_url(location), follow=True)

    assert response.redirect_chain == [(f"/locations/{location.pk}/", 302)]
    assert _flashes(response.content.decode()) == [("status", SENT_FLASH)]
    # Exactly one request, to the location's bot.
    assert len(fake_telegram.calls) == 1
    assert fake_telegram.calls[0].request.url == f"{TELEGRAM_API}/bot{token}/sendMessage"
    # The body keys are exactly an alert's plus the silent flag; the text is the D-11
    # string of the location's language, code point for code point (emoji, Cyrillic).
    [body] = fake_telegram.sent
    assert body == _test_body(lang, chat_id)
    assert list(body) == ["chat_id", "text", "parse_mode", "disable_notification"]
    # Not an alert: nothing is queued, and no incident or notice appears.
    assert not OutboxMessage.objects.exists()
    assert not OpsIncident.objects.exists()


@pytest.mark.django_db
def test_test_message_ignores_the_switches(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(maintenance=True, alerts_enabled=False, router_grace=True)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)

    response = admin.post(_url(location), follow=True)

    # Sent while maintenance is on and alerts are off (UI-SPEC screen B), and the
    # switches stay as they were.
    assert _flashes(response.content.decode()) == [("status", SENT_FLASH)]
    assert fake_telegram.sent == [_test_body("en")]
    stored = Location.objects.get(pk=location.pk)
    assert (stored.maintenance, stored.alerts_enabled, stored.router_grace) == (True, False, True)


@pytest.mark.django_db
def test_test_message_is_never_retried(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    fake_telegram.fail(DEFAULT_BOT_TOKEN, status=403, json_body=KICKED)

    admin.post(_url(location))

    # One attempt, whatever the answer; a reload of the page after the redirect is a GET
    # of the location page and sends nothing (UI-D4).
    assert len(fake_telegram.calls) == 1
    page = admin.get(f"/locations/{location.pk}/")
    assert page.status_code == 200
    assert len(fake_telegram.calls) == 1


# Failure responses: method, location, sign-in, CSRF


@pytest.mark.django_db
def test_test_message_get_is_405(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()

    for method in (admin.get, admin.put, admin.delete):
        assert method(_url(location)).status_code == 405

    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_test_message_deleted_is_404(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    gone = location_factory(deleted_at=T0)

    assert admin.post(_url(gone)).status_code == 404
    assert admin.post(f"/locations/{gone.pk + 1000}/test-message/").status_code == 404
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_test_message_needs_sign_in_and_a_csrf_token(
    client: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()

    anonymous = client.post(_url(location))

    assert anonymous.status_code == 302
    assert anonymous["Location"] == f"/login/?next={_url(location)}"

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(User.objects.create_user("admin", password="not-used-here"))

    assert strict.post(_url(location)).status_code == 403
    assert len(fake_telegram.calls) == 0


# D-11: a flash for every result kind (UI-SPEC Copywriting › Test message, E4 error)

# (answer registered for the bot, flash level, flash text)
FLASH_TABLE: dict[str, tuple[dict[str, Any], int, str]] = {
    # Permanent: the bot is not in the chat, or the chat was not found.
    "http_400": (
        {"status": 400, "json_body": _error(400, "Bad Request: chat not found")},
        messages.ERROR,
        NOT_IN_CHAT_FLASH.format(code="http_400"),
    ),
    "http_403": (
        {"status": 403, "json_body": KICKED},
        messages.ERROR,
        NOT_IN_CHAT_FLASH.format(code="http_403"),
    ),
    # Permanent: a bad token.
    "http_401": (
        {"status": 401, "json_body": _error(401, "Unauthorized")},
        messages.ERROR,
        BOT_REJECTED_FLASH.format(code="http_401"),
    ),
    "http_404": (
        {"status": 404, "json_body": _error(404, "Not Found")},
        messages.ERROR,
        BOT_REJECTED_FLASH.format(code="http_404"),
    ),
    # Another 4xx the client classes as permanent (a 5xx is transient, a 429 rate limited).
    "http_409": (
        {"status": 409, "json_body": _error(409, "Conflict: terminated by other request")},
        messages.ERROR,
        REFUSED_FLASH.format(code="http_409"),
    ),
    # The request went out and no answer came in time: it may have been sent.
    "read_timeout": (
        {"exc": requests.ReadTimeout("read timed out")},
        messages.WARNING,
        MAYBE_FLASH.format(code="read_timeout"),
    ),
    # Nothing left the client, or Telegram answered with a 5xx.
    "connect_error": (
        {"exc": _refused(DEFAULT_BOT_TOKEN)},
        messages.ERROR,
        UNREACHABLE_FLASH.format(code="connect_error"),
    ),
    "connect_timeout": (
        {"exc": requests.ConnectTimeout("connect timed out")},
        messages.ERROR,
        UNREACHABLE_FLASH.format(code="connect_timeout"),
    ),
    "http_503": (
        {"status": 503, "json_body": _error(503, "Service Unavailable")},
        messages.ERROR,
        UNREACHABLE_FLASH.format(code="http_503"),
    ),
    # Rate limited: the wait in whole seconds, "1 second" when it is 1.
    "429-1s": (
        {
            "status": 429,
            "json_body": _error(429, "Too Many Requests: retry after 1", retry_after=1),
        },
        messages.WARNING,
        RATE_LIMITED_FLASH.format(wait="1 second"),
    ),
    "429-30s": (
        {
            "status": 429,
            "json_body": _error(429, "Too Many Requests: retry after 30", retry_after=30),
        },
        messages.WARNING,
        RATE_LIMITED_FLASH.format(wait="30 seconds"),
    ),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(FLASH_TABLE))
def test_test_message_flash_table(
    rf: RequestFactory, location_factory: Callable[..., Any], fake_telegram: FakeTelegram, case: str
) -> None:
    answer, level, text = FLASH_TABLE[case]
    location = location_factory()
    fake_telegram.fail(DEFAULT_BOT_TOKEN, **answer)

    response, flashes = _post_test_message(rf, FakeClock(T0), location)

    # Always the redirect to the location page with one flash; one attempt, no retry.
    assert response.status_code == 302
    assert response["Location"] == f"/locations/{location.pk}/"
    assert flashes == [(level, text)]
    assert len(fake_telegram.calls) == 1
    # Fixed copy, the short code and integers only: never Telegram's text or the token.
    for description in DESCRIPTIONS:
        assert description not in text
    assert DEFAULT_BOT_TOKEN.split(":")[1] not in text


@pytest.mark.django_db
def test_test_message_answer_without_json_shows_its_code(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    # A proxy's HTML error page with status 200: no JSON body at all.
    fake_telegram.rsps.add(
        responses.POST,
        f"{TELEGRAM_API}/bot{DEFAULT_BOT_TOKEN}/sendMessage",
        status=200,
        body="<html><body><h1>Bad gateway</h1></body></html>",
        content_type="text/html",
    )

    response = admin.post(_url(location), follow=True)

    # No 500: the client's short code in the "refused" copy, as an error (role="alert").
    assert response.status_code == 200
    assert _flashes(response.content.decode()) == [("alert", REFUSED_FLASH.format(code="http_200"))]
    assert "Bad gateway" not in response.content.decode()


@pytest.mark.parametrize(
    ("result", "recovered", "expected"),
    [
        (SendResult("ok"), False, (messages.SUCCESS, SENT_FLASH)),
        (SendResult("ok"), True, (messages.SUCCESS, RECOVERED_FLASH)),
        # sendMessage never answers edit_target_missing; it would read as a refusal.
        (
            SendResult("edit_target_missing", code="target_missing"),
            False,
            (messages.ERROR, REFUSED_FLASH.format(code="target_missing")),
        ),
        # A rate limit without a usable wait gets the client's default wait.
        (
            SendResult("rate_limited", code="429"),
            False,
            (messages.WARNING, RATE_LIMITED_FLASH.format(wait="30 seconds")),
        ),
        # "recovered" means nothing for a failure: it is never a success flash.
        (
            SendResult("permanent", code="http_403"),
            True,
            (messages.ERROR, NOT_IN_CHAT_FLASH.format(code="http_403")),
        ),
    ],
    ids=["sent", "recovered", "edit-target-missing", "429-no-wait", "failure-not-recovered"],
)
def test_flash_for_test_message_edges(
    result: SendResult, recovered: bool, expected: tuple[int, str]
) -> None:
    assert flash_for_test_message(result, recovered) == expected


# D-10: a failed test message never opens (or closes) the delivery-failing incident


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["http_403", "http_401", "read_timeout", "http_503", "429-30s"])
def test_failed_test_message_never_opens_failing(
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    ops_settings: Any,
    case: str,
) -> None:
    answer, _, _ = FLASH_TABLE[case]
    healthy = location_factory(name="Healthy")
    failing = location_factory(name="Failing")
    held = T0 + timedelta(minutes=15)
    alerts = {place.pk: _queue(place) for place in (healthy, failing)}
    OutboxMessage.objects.filter(pk__in=[row.pk for row in alerts.values()]).update(
        next_attempt_at=held
    )
    with transaction.atomic():
        delivery.open_failing(failing.pk, T0 - timedelta(minutes=10), 403)
    notices = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS).count()
    fake_telegram.fail(DEFAULT_BOT_TOKEN, **answer)

    for place in (healthy, failing):
        _post_test_message(rf, FakeClock(T0), place)

    # No incident opened for the healthy location, the failing one stays open, no notice
    # was queued, and nothing was made due.
    assert _incidents(healthy) == []
    assert _incidents(failing) == [(T0 - timedelta(minutes=10), None)]
    assert OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS).count() == notices
    for row in alerts.values():
        assert _row(row).next_attempt_at == held
