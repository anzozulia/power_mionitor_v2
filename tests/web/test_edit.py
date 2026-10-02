"""Edit a location (LOC-04, DATA-04, SEC-04; D-07, D-08; UI-SPEC screen C, UI-D12).

- The edit form has the add form's fields with the stored values as initial values, plus
  an always-empty, write-only "New bot token". A valid save is one UPDATE of exactly the
  configuration columns (``actions.update_config``), never ``Model.save()``: the status,
  the outage start, the switches and the device key are never written, so a form loaded
  earlier can never revert what the engine or a switch did since (INV-02 #3).
- Thresholds apply from the next detection cycle and the stored timeline is never
  recomputed (DATA-04, INV-06 #1/#2). K-6 holds on edit as on create.
- A chat ID or token change is a channel change: the location's pending subscriber alerts
  become due at once, in the save's transaction, and the worker moves the chart (D-08).
- No save makes a Telegram call (KD2): every POST here is followed by an empty fake.

Detection-driven tests are ``django_db(transaction=True)``: ``detection.run_cycle`` and
``io_loop.run_iteration`` call ``close_old_connections()``, which no test-wide transaction
survives. The edit form is posted as the browser would post the page it loaded
(``_loaded_form``).
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeTelegram
from django.contrib.auth import get_user_model
from django.test import Client

from powermon.alerts.models import OutboxMessage
from powermon.engine import transitions
from powermon.engine.models import LocationState, SystemState
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.worker import detection

User = get_user_model()

CHANGES_SAVED = "Changes saved."
CHAT_B = -1009876543210


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _edit(location: Any) -> str:
    return f"/locations/{location.pk}/edit/"


def _anchors(resumed: datetime) -> None:
    """Detection resumed at ``resumed`` and the web start is unknown."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": resumed, "web_started_at": None}
    )


def _loaded_form(page: str) -> dict[str, str]:
    """What the edit page's form posts as loaded: every input's value and the selected language.

    The CSRF token is left out (the test client does not enforce it). An input without a
    value attribute posts "".
    """
    form = page[page.index('<form class="form"') :]
    form = form[: form.index("</form>")]
    values: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", form):
        name = re.search(r'\bname="([^"]*)"', tag)
        if name is None or name.group(1) == "csrfmiddlewaretoken":
            continue
        value = re.search(r'\bvalue="([^"]*)"', tag)
        values[name.group(1)] = unescape(value.group(1)) if value else ""
    select = re.search(r'<select name="language"[^>]*>(.*?)</select>', form, re.S)
    assert select is not None, "no language select"
    selected = re.findall(r'<option value="([^"]*)" selected>', select.group(1))
    assert len(selected) == 1
    values["language"] = selected[0]
    return values


def _flashes(admin: Client, url: str) -> list[str]:
    """The flash messages the page at ``url`` shows (success and info have role=status)."""
    page = admin.get(url).content.decode()
    return [unescape(t) for t in re.findall(r'role="(?:status|alert)">([^<]*)<', page)]


def _power_off_rows(location: Any) -> int:
    return OutboxMessage.objects.filter(location=location, kind="power_off").count()


# INV-02 #3: a stale edit never reverts the live status (the tracer)


@pytest.mark.django_db(transaction=True)
def test_INV02_3_stale_edit_never_reverts_the_status(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _anchors(_at(16, 0))
    location = location_factory(name="Office", period_s=60, grace_s=30)
    # Heartbeats every 60 s until 17:01:00, then silence: OFF after 17:02:30, not at it.
    for minute in range(56, 62):
        transitions.record_heartbeat(location.pk, _at(16 + minute // 60, minute % 60))

    # The admin opens the edit form while the location is on.
    page = admin.get(_edit(location))
    assert page.status_code == 200
    loaded = _loaded_form(page.content.decode())
    assert loaded == {
        "name": "Office",
        "period_s": "60",
        "grace_s": "30",
        "bot_token": "",
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "en",
    }

    # The detector records OFF at 17:02:31 (17:02:30 is not past the 90 s timeout).
    assert detection.run_cycle(_at(17, 2, 30)) == 0
    assert detection.run_cycle(_at(17, 2, 31)) == 1
    off = LocationState.objects.get(location=location)
    assert (off.status, off.outage_started_at) == ("off", _at(17, 1))
    assert _power_off_rows(location) == 1

    # A second later the admin saves the form loaded while the status was on.
    response = admin.post(_edit(location), {**loaded, "grace_s": "45"})

    assert response.status_code == 302
    assert response.url == f"/locations/{location.pk}/"
    assert _flashes(admin, response.url) == [CHANGES_SAVED]
    state = LocationState.objects.get(location=location)
    # The status stays off with its outage start; the save never wrote the state row.
    assert (state.status, state.outage_started_at) == ("off", _at(17, 1))
    assert state.state_version == off.state_version
    assert Location.objects.get(pk=location.pk).grace_s == 45
    # Exactly one OFF alert, and the next cycle records no second OFF.
    assert detection.run_cycle(_at(17, 3, 0)) == 0
    assert _power_off_rows(location) == 1
    assert OutboxMessage.objects.count() == 1
    # The save made no Telegram call (KD2).
    assert len(fake_telegram.calls) == 0


# D-07: the save writes only the configuration columns


@pytest.mark.django_db
def test_edit_save_is_column_limited(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office", language="uk")
    loaded = _loaded_form(admin.get(_edit(location)).content.decode())
    detail = f"/locations/{location.pk}/"

    # After the form was loaded: maintenance on, alerts off, router grace on, key rotated.
    assert admin.post(f"{detail}maintenance/", {"value": "on"}).status_code == 302
    assert admin.post(f"{detail}alerts/", {"value": "off"}).status_code == 302
    assert admin.post(f"{detail}router-grace/", {"value": "on"}).status_code == 302
    assert actions.regenerate_key(location.pk, location.device_key) is True
    new_key = Location.objects.get(pk=location.pk).device_key
    assert new_key != location.device_key

    posted = {
        **loaded,
        "name": "Office, 2nd floor",
        "period_s": "45",
        "grace_s": "20",
        "chat_id": str(CHAT_B),
        "language": "ru",
    }
    response = admin.post(_edit(location), posted)

    assert response.status_code == 302
    saved = Location.objects.get(pk=location.pk)
    # The five fields take the posted values; the empty token keeps the stored one.
    assert (saved.name, saved.period_s, saved.grace_s, saved.chat_id, saved.language) == (
        "Office, 2nd floor",
        45,
        20,
        CHAT_B,
        "ru",
    )
    assert saved.bot_token == DEFAULT_BOT_TOKEN
    # What changed after the form was loaded keeps its newer value.
    assert (saved.maintenance, saved.alerts_enabled, saved.router_grace) == (True, False, True)
    assert saved.device_key == new_key
    assert saved.deleted_at is None
    assert len(fake_telegram.calls) == 0


# Failure: unknown or deleted locations


@pytest.mark.django_db
def test_edit_unknown_or_deleted_location_is_404(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    unknown = gone.pk + 1000
    posted = {
        "name": "Back again",
        "period_s": "60",
        "grace_s": "30",
        "bot_token": "",
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "en",
    }

    for pk in (gone.pk, unknown):
        assert admin.get(f"/locations/{pk}/edit/").status_code == 404
        assert admin.post(f"/locations/{pk}/edit/", posted).status_code == 404

    assert Location.objects.get(pk=gone.pk).name == "Gone"
    # The action itself writes nothing for a deleted or unknown location.
    for pk in (gone.pk, unknown):
        saved = actions.update_config(pk, {**posted, "chat_id": DEFAULT_CHAT_ID}, _at(9, 5))
        assert saved == actions.ConfigSaved(found=False, channel_changed=False)
    assert Location.objects.get(pk=gone.pk).name == "Gone"
