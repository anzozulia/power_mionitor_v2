"""The Python side of the admin's feedback copy (UI-09; 06-UI-SPEC "Python-owned copy").

- The five instructive successes are queued sticky (``extra_tags="sticky"``): location
  created, key regenerated, channel changed, location deleted, history reset. Every other
  flash, "Changes saved." included, carries no sticky tag (06-CONTEXT Toasts).
- Amendment A1: a test message that met an HTTP 5xx (``transient``) says Telegram had a
  server error; ``not_sent`` keeps "could not be reached". Amendments A4 and A7 change the
  removal-deferred and outage-removed flashes. The texts are pinned here verbatim, once;
  every other test imports the constants (TEST-STRATEGY §3.3).
- R6 as amended on 2026-10-04: the sign-out response, and no other, sends
  ``Clear-Site-Data: "cache"``.
- N11: the throttled sign-in page's context carries ``retry_after`` (the 429's
  ``Retry-After``), the countdown's start, next to the unchanged throttle message (R9).
- R3: the setup page's context carries the public bot id, never the secret part.

Everything is read from the message storage, the response headers or the template
context, never from the HTML.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import Client

from powermon.engine import transitions
from powermon.telegram.client import SendResult
from powermon.throttle import rules
from powermon.web import history_views, location_views, views
from powermon.web.admin_sync import sync_admin
from powermon.web.location_views import flash_for_test_message, regenerate_marker

User = get_user_model()

# 06-UI-SPEC "Amendments applied in Phase 6", verbatim: pinned here once.
A1_SERVER_ERROR = (
    "Telegram had a server error ({code}), so the test message was not sent. Try again in a minute."
)
A1_NOT_SENT_KEPT = (
    "Telegram could not be reached ({code}), so the test message was not sent. Try again in a "
    "minute."
)
A4_REMOVAL_DEFERRED = (
    "Not removed: an alert about this outage is being sent to the channel right now. Try "
    "again in a minute."
)
A7_OUTAGE_REMOVED = (
    "Outage from {start} removed: its time now counts as power on. The removal sent no "
    "message. If it is within the last 7 days, the pinned chart shows the change at its next "
    "update."
)
CLEAR_SITE_DATA = "Clear-Site-Data"
CHAT_B = -1009876543210
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _sticky(message: Message) -> bool:
    return "sticky" in str(message.extra_tags or "").split()


def _only(client: Client, response: Any) -> Message:
    """The one message the request queued, read from its storage, never from the HTML.

    The client's messages cookie is dropped afterwards, so the next request of the same
    test starts with no message left over from this one.
    """
    flashes = list(get_messages(response.wsgi_request))
    client.cookies.pop(CookieStorage.cookie_name, None)
    assert len(flashes) == 1, [str(m) for m in flashes]
    return flashes[0]


# UI-09: the five instructive successes are sticky at the source


Make = Callable[..., Any]


def _created(admin: Client, make: Make, monkeypatch: pytest.MonkeyPatch, now: datetime) -> Any:
    data = {
        "name": "Office",
        "period_s": "60",
        "grace_s": "30",
        "bot_token": DEFAULT_BOT_TOKEN,
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "en",
        "chart_refresh_min": "15",
    }
    return admin.post("/locations/new/", data)


def _edit_data(location: Any, **overrides: str) -> dict[str, str]:
    return {
        "name": location.name,
        "period_s": str(location.period_s),
        "grace_s": str(location.grace_s),
        "bot_token": "",
        "chat_id": str(location.chat_id),
        "language": location.language,
        "chart_refresh_min": str(location.chart_refresh_min),
        **overrides,
    }


def _regenerated(admin: Client, make: Make, monkeypatch: pytest.MonkeyPatch, now: datetime) -> Any:
    location = make()
    marker = regenerate_marker(location.device_key)
    return admin.post(f"/locations/{location.pk}/setup/regenerate/", {"marker": marker})


def _channel_changed(
    admin: Client, make: Make, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> Any:
    location = make()
    data = _edit_data(location, chat_id=str(CHAT_B))
    return admin.post(f"/locations/{location.pk}/edit/", data)


def _deleted(admin: Client, make: Make, monkeypatch: pytest.MonkeyPatch, now: datetime) -> Any:
    location = make()
    return admin.post(f"/locations/{location.pk}/delete/")


def _reset(admin: Client, make: Make, monkeypatch: pytest.MonkeyPatch, now: datetime) -> Any:
    location = make()
    # One heartbeat starts monitoring: the location has history and is not off.
    assert transitions.record_heartbeat(location.pk, now) == "started"
    clock = FakeClock(now + timedelta(hours=1))
    monkeypatch.setattr(history_views.HistoryResetView, "clock", clock)
    return admin.post(f"/locations/{location.pk}/reset/")


Act = Callable[[Client, Make, pytest.MonkeyPatch, datetime], Any]
STICKY_CASES: dict[str, tuple[Act, str]] = {
    "created": (_created, views.LOCATION_CREATED_MESSAGE),
    "regenerated": (_regenerated, location_views.REGENERATED_MESSAGE),
    "channel-changed": (_channel_changed, location_views.CHANNEL_CHANGED_MESSAGE),
    "deleted": (_deleted, location_views.LOCATION_DELETED_MESSAGE),
    "history-reset": (_reset, history_views.HISTORY_RESET_MESSAGE),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(STICKY_CASES))
def test_UI09_sticky_successes(
    admin: Client,
    location_factory: Make,
    monkeypatch: pytest.MonkeyPatch,
    fixed_now: datetime,
    case: str,
) -> None:
    act, text = STICKY_CASES[case]

    message = _only(admin, act(admin, location_factory, monkeypatch, fixed_now))

    assert (message.level, message.message) == (messages.SUCCESS, text)
    assert _sticky(message)


@pytest.mark.django_db
def test_UI09_other_flashes_are_not_sticky(admin: Client, location_factory: Make) -> None:
    location = location_factory()
    page = f"/locations/{location.pk}"

    saved = _only(admin, admin.post(f"{page}/edit/", _edit_data(location, name="B")))
    switched = _only(admin, admin.post(f"{page}/alerts/", {"value": "off"}))
    again = _only(admin, admin.post(f"{page}/alerts/", {"value": "off"}))
    deleted = _only(admin, admin.post(f"{page}/delete/"))
    already = _only(admin, admin.post(f"{page}/delete/"))
    signed_out = _only(admin, admin.post("/logout/"))

    alerts = location_views.ALERTS_COPY
    assert (saved.level, saved.message) == (messages.SUCCESS, location_views.CHANGES_SAVED_MESSAGE)
    assert (switched.level, switched.message) == (messages.SUCCESS, alerts["off"])
    assert (again.level, again.message) == (messages.INFO, alerts["already_off"])
    # The delete itself is one of the five sticky ones; its repeat is plain info.
    assert _sticky(deleted)
    assert already.message == location_views.ALREADY_DELETED_MESSAGE
    assert signed_out.message == views.SIGNED_OUT_MESSAGE
    for message in (saved, switched, again, already, signed_out):
        assert not _sticky(message), message.message


# Amendments A1, A4 and A7


def test_A1_transient_is_a_server_error() -> None:
    level, text = flash_for_test_message(SendResult("transient", code="http_502"), False)

    assert (level, text) == (messages.ERROR, A1_SERVER_ERROR.format(code="http_502"))
    assert location_views.TEST_SERVER_ERROR_MESSAGE == A1_SERVER_ERROR


def test_A1_not_sent_keeps_unreachable() -> None:
    # The failure path is unchanged: nothing left the client, so Telegram was not reached.
    result = flash_for_test_message(SendResult("not_sent", code="connect_error"), False)

    assert result == (messages.ERROR, A1_NOT_SENT_KEPT.format(code="connect_error"))
    assert location_views.TEST_UNREACHABLE_MESSAGE == A1_NOT_SENT_KEPT


def test_A4_A7_copy() -> None:
    assert history_views.REMOVAL_DEFERRED_MESSAGE == A4_REMOVAL_DEFERRED
    assert history_views.OUTAGE_REMOVED_MESSAGE == A7_OUTAGE_REMOVED
    removed = history_views.OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 11:00")
    assert removed.startswith("Outage from 2026-10-01 11:00 removed:")
    assert "The removal sent no message." in removed
    assert "No message was sent." not in removed


# R6 (amended 2026-10-04): Clear-Site-Data on the sign-out response only


@pytest.mark.django_db
def test_R6_clear_site_data_only_on_sign_out(admin: Client, location_factory: Make) -> None:
    location = location_factory()
    anonymous = Client()

    others = {
        "GET /login/": anonymous.get("/login/"),
        "GET /": admin.get("/"),
        "switch POST": admin.post(f"/locations/{location.pk}/alerts/", {"value": "off"}),
        "GET /logout/ (405)": admin.get("/logout/"),
    }
    signed_out = admin.post("/logout/")
    after = admin.get("/login/")

    assert signed_out.status_code == 302
    assert signed_out.headers.get(CLEAR_SITE_DATA) == '"cache"'
    assert after.status_code == 200
    for label, response in {**others, "GET /login/ after sign-out": after}.items():
        assert CLEAR_SITE_DATA not in response.headers, label


# N11: the throttle countdown's server value (R9 unchanged)


@pytest.mark.django_db
def test_N11_throttled_context_has_retry_after(client: Client) -> None:
    sync_admin("admin", "pw-one")
    for _ in range(rules.MAX_FAILURES):
        assert client.post("/login/", {"username": "admin", "password": "wrong"}).status_code == 200

    response = client.post("/login/", {"username": "admin", "password": "pw-one"})

    assert response.status_code == 429
    # The same value as the 429's Retry-After header: whole seconds, as a string.
    assert response.context.get("retry_after") == rules.RETRY_AFTER == "300"
    assert response.context.get("throttled") is True
    assert response["Retry-After"] == rules.RETRY_AFTER
    assert response.context.get("throttle_message") == rules.THROTTLE_MESSAGE


@pytest.mark.django_db
def test_N11_unthrottled_sign_in_has_no_retry_after(client: Client) -> None:
    sync_admin("admin", "pw-one")

    response = client.post("/login/", {"username": "admin", "password": "wrong"})

    assert response.status_code == 200
    assert response.context.get("retry_after") is None


# R3: the setup page shows the public bot id only


@pytest.mark.django_db
def test_R3_setup_context_has_only_the_public_bot_id(admin: Client, location_factory: Make) -> None:
    location = location_factory(bot_token=TOKEN)

    for response in (
        admin.get(f"/locations/{location.pk}/setup/"),
        admin.post(f"/locations/{location.pk}/setup/"),
        admin.get(f"/locations/{location.pk}/"),
    ):
        assert response.status_code == 200
        assert response.context.get("token_bot_id") == "987654321"
        assert SECRET not in response.content.decode()
