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
(``_loaded_form``). Where the save's time matters (make-due), the view gets a
``FakeClock`` by constructor injection through ``RequestFactory`` (``_save``). The chart
helpers (``_monitor``, the detection cursor in ``_pass``, ``_chart_steps``) are copied from
tests/chart, not imported: tests have no ``__init__.py``.
"""

import dataclasses
import re
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    Actor,
    FakeClock,
    FakeTelegram,
    blocked_on_lock,
    wait_for,
)
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.http import HttpResponse
from django.test import Client, RequestFactory

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart import source
from powermon.chart.models import ChartMessage
from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.i18n import chart_texts
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.locations.validators import TOKEN_FORMAT
from powermon.web import location_views
from powermon.web.location_views import LocationEditView
from powermon.worker import detection, io_loop

User = get_user_model()

KYIV = "Europe/Kyiv"
CHANGES_SAVED = "Changes saved."
CHANNEL_CHANGED = (
    "Changes saved. The weekly chart is posted again with the new bot or chat. The old one is "
    "unpinned if this location's bot is an admin of the old channel."
)
FORM_ERROR = "The changes were not saved. Fix the fields marked below."
REPASTE_NOTE = "Paste the token again: it is never sent back to the browser."
PERIOD_TOO_SHORT = "The heartbeat period must be at least 10 seconds."
GRACE_TOO_SHORT = "The grace period must be at least 10 seconds."
SECONDS_TOO_LONG = "Use at most 3600 seconds (1 hour)."
NAME_EMPTY = "Enter a name."
NAME_TOO_LONG = "Use at most 100 characters."
CHAT_ID_EMPTY = "Enter the channel's numeric chat ID."
TOKEN_HELP = (
    "Leave this empty to keep the current token, <code>{masked}</code>. To use another bot, "
    "paste its token from @BotFather. The token is saved but never shown again."
)
EDIT_NOTE = (
    "<strong>Note:</strong> New period and grace values apply from the next check and never "
    "change past history. Lower values can report OFF at the next check if the device has "
    "already been silent that long. A new chat ID or bot token moves the weekly chart: a new "
    "one is posted and pinned, and the old one is unpinned if this location's bot is an admin "
    "of the old channel. Otherwise the old pin stays: unpin it by hand in Telegram. "
    "Maintenance, alerts and router grace are switched on the location page."
)
CHAT_B = -1009876543210
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
MASKED_TOKEN = "987654321:••••••••"
NEW_SECRET = "Nw_4-Zp8Kd" * 4
NEW_TOKEN = f"123123123:{NEW_SECRET}"
NEW_MASKED_TOKEN = "123123123:••••••••"
XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
MIN_US = 60_000_000
KICKED = {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member"}


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


def _form(location: Any, **overrides: str) -> dict[str, str]:
    """The edit POST of the location's stored values (an untouched form), with ``overrides``."""
    return {
        "name": location.name,
        "period_s": str(location.period_s),
        "grace_s": str(location.grace_s),
        "bot_token": "",
        "chat_id": str(location.chat_id),
        "language": location.language,
        **overrides,
    }


def _save(
    rf: RequestFactory, location: Any, clock: FakeClock, **overrides: str
) -> tuple[HttpResponse, list[str]]:
    """POST the edit form to the view with an injected clock; the response and its flashes."""
    request = rf.post(_edit(location), _form(location, **overrides))
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    response = LocationEditView.as_view(clock=clock)(request, pk=location.pk)
    return response, [str(m) for m in request._messages]  # type: ignore[attr-defined]


def _held_alert(location: Any, recorded_at: datetime, held_until: datetime) -> OutboxMessage:
    """A pending OFF alert refused once (http_403) and held until ``held_until``."""
    with transaction.atomic():
        row = outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=recorded_at,
            recorded_at=recorded_at,
            payload={"was_on_us": 5 * MIN_US},
        )
    OutboxMessage.objects.filter(pk=row.pk).update(
        attempts=1, last_error="http_403", next_attempt_at=held_until
    )
    row.refresh_from_db()
    return row


def _input(page: str, name: str) -> str:
    """The rendered ``<input>`` tag whose name attribute is ``name``."""
    match = re.search(rf'<input\b[^>]*\bname="{name}"[^>]*>', page)
    assert match is not None, f"no input named {name!r}"
    return match.group(0)


def _field_error(page: str, field: str) -> str | None:
    match = re.search(rf'<p class="error" id="id_{field}_error">(.*?)</p>', page, re.S)
    return unescape(match.group(1)) if match else None


def _help(page: str, field: str) -> str:
    """The inner HTML of the field's help paragraph."""
    match = re.search(rf'<p class="help" id="id_{field}_helptext">(.*?)</p>', page, re.S)
    assert match is not None, f"no help for {field!r}"
    return match.group(1)


def _alerts(page: str) -> list[str]:
    return [unescape(t.strip()) for t in re.findall(r'role="alert"[^>]*>([^<]*)<', page)]


def _intervals(location: Any) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    return list(
        PowerInterval.objects.filter(location=location)
        .order_by("start_at")
        .values_list("state", "start_at", "end_at", "outage_start_at")
    )


def _kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-01 12:00"`` (fold 0) as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(KYIV)).astimezone(UTC)


def _monitor(location: Any, since: datetime) -> None:
    """On since ``since``: live state "on" and one open on piece (chart_fixtures.monitor)."""
    LocationState.objects.filter(location=location).update(
        status="on",
        on_since=since,
        last_heartbeat_at=since,
        state_version=F("state_version") + 1,
    )
    PowerInterval.objects.create(location=location, state="on", start_at=since)


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass with charts, the detection cursor moved to the clock's now (never back)."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
    return io_loop.run_iteration(clock, state, charts=True)


def _chart_steps(fake: FakeTelegram, start: int = 0) -> list[tuple[str, int, int | None]]:
    """(method, chat id, message id) of every accepted chart call from index ``start`` on."""
    out = []
    for call in fake.chart_calls[start:]:
        message_id = call.fields.get("message_id")
        out.append(
            (
                call.method,
                int(call.fields["chat_id"]),
                None if message_id is None else int(message_id),
            )
        )
    return out


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


@pytest.mark.django_db
def test_edit_of_a_location_deleted_meanwhile_is_404(
    admin: Client, location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    location = location_factory(name="Office")
    found = location_views.location_or_404

    def found_then_deleted(pk: int) -> Location:
        # The page's lookup finds the location; a delete commits before the save runs.
        result = found(pk)
        assert actions.delete_location(pk, _at(9, 0)) is True
        return result

    monkeypatch.setattr(location_views, "location_or_404", found_then_deleted)

    response = admin.post(_edit(location), _form(location, name="Renamed"))

    # The save's locking read sees the tombstone: nothing is written, the answer is 404.
    assert response.status_code == 404
    assert Location.objects.get(pk=location.pk).name == "Office"


@pytest.mark.django_db
def test_update_config_refuses_a_naive_now(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    data = {**_form(location, name="Renamed"), "chat_id": CHAT_B}

    with pytest.raises(ValueError, match="aware"):
        actions.update_config(location.pk, data, datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001

    assert Location.objects.get(pk=location.pk).name == "Office"


# SEC-04: the bot token is write-only on the edit form (D-07, UI-D12)


@pytest.mark.django_db
def test_edit_token_is_write_only(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    pages: list[str] = []
    redirects: list[str] = []

    page = admin.get(_edit(location)).content.decode()
    pages.append(page)
    tag = _input(page, "bot_token")
    assert 'type="password"' in tag
    assert "value=" not in tag
    assert 'autocomplete="off"' in tag
    assert 'spellcheck="false"' in tag
    assert '<label for="id_bot_token">New bot token</label>' in page
    assert _help(page, "bot_token") == TOKEN_HELP.format(masked=MASKED_TOKEN)
    loaded = _loaded_form(page)
    assert loaded["bot_token"] == ""

    # An empty token keeps the current one.
    kept = admin.post(_edit(location), loaded)
    assert kept.status_code == 302
    redirects.append(kept.url)
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN
    pages.append(admin.get(kept.url).content.decode())

    # An invalid submit with a new token typed: the input is empty again, nothing saved.
    invalid = admin.post(_edit(location), {**loaded, "bot_token": NEW_TOKEN, "period_s": "9"})
    assert invalid.status_code == 200
    pages.append(invalid.content.decode())
    assert "value=" not in _input(invalid.content.decode(), "bot_token")
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN

    # A new valid token replaces the current one, and only its mask is ever shown.
    replaced = admin.post(_edit(location), {**loaded, "bot_token": f"  {NEW_TOKEN}  "})
    assert replaced.status_code == 302
    redirects.append(replaced.url)
    assert Location.objects.get(pk=location.pk).bot_token == NEW_TOKEN
    pages.append(admin.get(replaced.url).content.decode())
    again = admin.get(_edit(location)).content.decode()
    pages.append(again)
    assert _help(again, "bot_token") == TOKEN_HELP.format(masked=NEW_MASKED_TOKEN)
    assert "value=" not in _input(again, "bot_token")

    for html in pages:
        for secret in (TOKEN, SECRET, NEW_TOKEN, NEW_SECRET):
            assert secret not in html
    for url in redirects:
        assert SECRET not in url
        assert NEW_SECRET not in url
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_edit_invalid_token_shows_the_repaste_note_only_when_typed(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    loaded = _loaded_form(admin.get(_edit(location)).content.decode())

    # A typed token that is not a token: its field error, an empty input and the note.
    typed = admin.post(_edit(location), {**loaded, "bot_token": "not-a-token"})
    assert typed.status_code == 200
    page = typed.content.decode()
    assert _alerts(page) == [FORM_ERROR]
    assert _field_error(page, "bot_token") == TOKEN_FORMAT
    tag = _input(page, "bot_token")
    assert "value=" not in tag
    assert f'<p class="help" id="id_bot_token_note">{REPASTE_NOTE}</p>' in page
    assert "id_bot_token_note" in tag
    assert "not-a-token" not in page
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN

    # An invalid period with no token typed (or only spaces): no note to re-paste.
    for blank in ("", "   "):
        untyped = admin.post(_edit(location), {**loaded, "bot_token": blank, "period_s": "9"})
        assert untyped.status_code == 200
        page = untyped.content.decode()
        assert _alerts(page) == [FORM_ERROR]
        assert _field_error(page, "period_s") == PERIOD_TOO_SHORT
        assert _field_error(page, "bot_token") is None
        assert REPASTE_NOTE not in page
        assert "id_bot_token_note" not in _input(page, "bot_token")
        # E5 error: every value is kept except the token.
        assert 'value="Office"' in _input(page, "name")
        assert 'value="9"' in _input(page, "period_s")
        assert f'value="{DEFAULT_CHAT_ID}"' in _input(page, "chat_id")
    assert Location.objects.get(pk=location.pk).period_s == 60


# K-6 on edit, and the other required fields (E5 partial, long-text)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("period_s", "9", PERIOD_TOO_SHORT),
        ("grace_s", "9", GRACE_TOO_SHORT),
        ("period_s", "3601", SECONDS_TOO_LONG),
        ("grace_s", "3601", SECONDS_TOO_LONG),
    ],
)
def test_K6_edit_rejects_short_period_and_grace(
    admin: Client,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    field: str,
    value: str,
    message: str,
) -> None:
    location = location_factory(name="Office", period_s=60, grace_s=30)

    response = admin.post(_edit(location), _form(location, name="Renamed", **{field: value}))

    assert response.status_code == 200
    page = response.content.decode()
    assert _alerts(page) == [FORM_ERROR]
    assert _field_error(page, field) == message
    saved = Location.objects.get(pk=location.pk)
    assert (saved.name, saved.period_s, saved.grace_s) == ("Office", 60, 30)
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_edit_rejects_blank_fields_and_a_long_name(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    page = admin.get(_edit(location)).content.decode()
    assert 'maxlength="100"' in _input(page, "name")
    assert "autofocus" in _input(page, "name")

    blank = admin.post(_edit(location), _form(location, name="", chat_id=""))
    page = blank.content.decode()
    assert blank.status_code == 200
    assert _field_error(page, "name") == NAME_EMPTY
    assert _field_error(page, "chat_id") == CHAT_ID_EMPTY
    assert "This field is required." not in page

    long = admin.post(_edit(location), _form(location, name="x" * 101))
    assert long.status_code == 200
    assert _field_error(long.content.decode(), "name") == NAME_TOO_LONG
    assert Location.objects.get(pk=location.pk).name == "Office"


# DATA-04: threshold changes never rewrite history (INV-06)


@pytest.mark.django_db
def test_INV06_1_threshold_changes_keep_yesterdays_row(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    today = date(2026, 10, 1)
    yesterday = date(2026, 9, 30)
    now = _kyiv("2026-10-01 11:00")
    location = location_factory(name="Office", period_s=60, grace_s=30)
    # Yesterday's outage 10:00-10:08, between two on pieces (as the engine stores it).
    for state, start, end, outage_start in (
        ("on", "2026-09-29 08:00", "2026-09-30 10:00", None),
        ("off", "2026-09-30 10:00", "2026-09-30 10:08", "2026-09-30 10:00"),
        ("on", "2026-09-30 10:08", None, None),
    ):
        PowerInterval.objects.create(
            location=location,
            state=state,
            start_at=_kyiv(start),
            end_at=None if end is None else _kyiv(end),
            outage_start_at=None if outage_start is None else _kyiv(outage_start),
        )
    LocationState.objects.filter(location=location).update(
        status="on", on_since=_kyiv("2026-09-30 10:08"), last_heartbeat_at=now
    )

    def week() -> Any:
        return source.load_week(location.pk, today=today, now=now, tz=KYIV, live=True)

    before = week()
    [row] = [r for r in before.rows if r.day == yesterday]
    assert (row.off_us, row.count) == (8 * MIN_US, 1)
    assert chart_texts.row_total(row.off_us, row.count, row.monitored, "en") == ("8m", " · 1")
    stored = _intervals(location)

    # Grace raised to 600 s (ROADMAP SC3: raised)...
    raised = admin.post(_edit(location), _form(location, grace_s="600"))
    assert raised.status_code == 302
    assert Location.objects.get(pk=location.pk).grace_s == 600
    assert _intervals(location) == stored
    assert week().rows == before.rows

    # ...then the period and the grace lowered to 20 s and 10 s (lowered).
    lowered = admin.post(_edit(location), _form(location, period_s="20", grace_s="10"))
    assert lowered.status_code == 302
    saved = Location.objects.get(pk=location.pk)
    assert (saved.period_s, saved.grace_s) == (20, 10)
    assert _intervals(location) == stored
    assert week().rows == before.rows
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_INV06_2_lowering_grace_records_off_at_the_next_cycle(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _anchors(_at(9, 0))
    location = location_factory(name="Office", period_s=60, grace_s=30)
    # Heartbeats until T = 12:00:00.
    for minute in range(55, 61):
        transitions.record_heartbeat(location.pk, _at(11 + minute // 60, minute % 60))
    last = _at(12, 0)
    later = last + timedelta(seconds=80)

    # 80 s of silence is within period + grace (90 s): no OFF yet.
    assert detection.run_cycle(later) == 0
    loaded = _loaded_form(admin.get(_edit(location)).content.decode())

    # The admin lowers grace from 30 s to 10 s: 80 s is now past period + grace (70 s).
    response = admin.post(_edit(location), {**loaded, "grace_s": "10"})
    assert response.status_code == 302

    # The next cycle records OFF, starting at that last heartbeat.
    assert detection.run_cycle(later) == 1
    state = LocationState.objects.get(location=location)
    assert (state.status, state.outage_started_at) == ("off", last)
    assert _intervals(location) == [
        ("on", _at(11, 55), last, None),
        ("off", last, None, last),
    ]
    assert _power_off_rows(location) == 1
    assert len(fake_telegram.calls) == 0


# D-08: a chat or token change is a channel change


@pytest.mark.django_db(transaction=True)
def test_D08_chat_change_makes_alerts_due_and_moves_the_chart(
    rf: RequestFactory,
    settings: Any,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    location = location_factory(name="Office")
    _monitor(location, _kyiv("2026-10-01 08:00"))
    # The bot's first alert is refused with 403; every later message is accepted.
    fake_telegram.fail(DEFAULT_BOT_TOKEN, status=403, json_body=KICKED)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(_kyiv("2026-10-01 12:05"))
    relay = io_loop.RelayState()

    # Today's chart is posted and pinned in chat A.
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is True
    [old] = ChartMessage.objects.all()
    assert (old.chat_id, old.message_id, old.pinned) == (DEFAULT_CHAT_ID, 1001, True)

    # An OFF alert is refused with 403: one attempt, then held for 15 minutes.
    clock.advance(seconds=30)
    with transaction.atomic():
        alert = outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=clock.now() - timedelta(minutes=5),
            recorded_at=clock.now(),
            payload={"was_on_us": 4 * 60 * MIN_US},
        )
    assert _pass(clock, relay) is True
    alert.refresh_from_db()
    assert (alert.status, alert.attempts, alert.last_error) == ("pending", 1, "http_403")
    assert alert.next_attempt_at == clock.now() + timedelta(minutes=15)

    # Five minutes later the alert is still held for 10 minutes: the admin moves the
    # location to chat B.
    clock.advance(minutes=5)
    calls_before_save = len(fake_telegram.calls)
    response, flashes = _save(rf, location, clock, chat_id=str(CHAT_B))

    assert response.status_code == 302
    assert response["Location"] == f"/locations/{location.pk}/"
    assert flashes == [CHANNEL_CHANGED]
    alert.refresh_from_db()
    assert alert.next_attempt_at == clock.now()
    # The save itself made no Telegram call (KD2).
    assert len(fake_telegram.calls) == calls_before_save

    # The next pass sends the alert to chat B at once: the 15-minute hold the 403 earned
    # belongs to chat A's channel, and the save made the row due.
    sent_before = len(fake_telegram.sent)
    charts_before = len(fake_telegram.chart_calls)
    assert _pass(clock, relay) is True
    alert.refresh_from_db()
    assert alert.status == "sent"
    [off] = fake_telegram.sent[sent_before:]
    assert off["chat_id"] == CHAT_B
    assert "POWER OFF" in off["text"]
    # The old chart's release goes to chat A, whose channel still waits out that hold
    # (shared per-chat backoff, Phase 3 D-06), and nothing is posted in chat B before the
    # release (Pitfall 1).
    assert _chart_steps(fake_telegram, charts_before) == []
    assert _pass(clock, relay) is False

    # Once chat A's hold is over, the old chart is unpinned there by its message id, then
    # today's chart is posted and pinned in chat B.
    clock.set(_kyiv("2026-10-01 12:20:30"))
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is True
    assert _chart_steps(fake_telegram, charts_before) == [
        ("unpinChatMessage", DEFAULT_CHAT_ID, 1001),
        ("sendPhoto", CHAT_B, None),
        ("pinChatMessage", CHAT_B, 1002),
    ]
    old.refresh_from_db()
    assert old.retired_at is not None
    pinned = ChartMessage.objects.filter(pinned=True).values_list("chat_id", "message_id")
    assert list(pinned) == [(CHAT_B, 1002)]
    # Every message after the save went to chat B; the old message is never named again.
    assert all(body["chat_id"] == CHAT_B for body in fake_telegram.sent[sent_before:])
    assert all(m != 1001 for _, _, m in _chart_steps(fake_telegram, charts_before + 1))


@pytest.mark.django_db
def test_D08_token_change_is_a_channel_change(
    rf: RequestFactory, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    clock = FakeClock(_at(9, 0))
    held = _held_alert(location, _at(8, 55), held_until=_at(9, 10))

    response, flashes = _save(rf, location, clock, bot_token=NEW_TOKEN)

    assert response.status_code == 302
    assert flashes == [CHANNEL_CHANGED]
    held.refresh_from_db()
    assert held.next_attempt_at == clock.now()
    assert Location.objects.get(pk=location.pk).bot_token == NEW_TOKEN
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_same_values_twice_change_nothing(
    rf: RequestFactory, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    other = location_factory(name="Other")
    clock = FakeClock(_at(9, 0))
    held = _held_alert(location, _at(8, 55), held_until=_at(9, 10))
    other_held = _held_alert(other, _at(8, 55), held_until=_at(9, 10))
    before = Location.objects.filter(pk=location.pk).values().get()

    # The same values twice, then the same token typed again: never a channel change.
    for overrides in ({}, {}, {"bot_token": TOKEN}):
        response, flashes = _save(rf, location, clock, **overrides)
        assert response.status_code == 302
        assert flashes == [CHANGES_SAVED]

    assert Location.objects.filter(pk=location.pk).values().get() == before
    held.refresh_from_db()
    assert held.next_attempt_at == _at(9, 10)

    # A chat change makes only this location's held alerts due.
    response, flashes = _save(rf, location, clock, chat_id=str(CHAT_B))
    assert flashes == [CHANNEL_CHANGED]
    held.refresh_from_db()
    other_held.refresh_from_db()
    assert (held.next_attempt_at, other_held.next_attempt_at) == (clock.now(), _at(9, 10))
    assert len(fake_telegram.calls) == 0


# SEC-04 concurrency: an edit racing a key rotation never brings the old key back


@pytest.mark.django_db(transaction=True)
def test_edit_racing_a_regenerate_keeps_the_new_key(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    old_key = location.device_key
    rotated = threading.Event()
    commit = threading.Event()

    def regenerate() -> bool:
        # The rotation holds the location row until the test lets it commit.
        with transaction.atomic():
            replaced = actions.regenerate_key(location.pk, old_key)
            rotated.set()
            assert commit.wait(5)
        return replaced

    data = {**_form(location, name="Renamed", grace_s="45"), "chat_id": DEFAULT_CHAT_ID}
    rotation = Actor(regenerate)
    edit = Actor(lambda: actions.update_config(location.pk, data, _at(9, 0)))
    try:
        rotation.start()
        assert rotated.wait(5)
        edit.start()
        # The save's locking read waits for the uncommitted rotation.
        assert wait_for(lambda: edit.pid is not None and blocked_on_lock(edit.pid))
    finally:
        commit.set()
        rotation.join(10)
        edit.join(10)

    assert (rotation.exc, edit.exc) == (None, None)
    assert rotation.result is True
    assert edit.result == actions.ConfigSaved(found=True, channel_changed=False)
    saved = Location.objects.get(pk=location.pk)
    assert saved.device_key != old_key
    assert (saved.name, saved.grace_s) == ("Renamed", 45)


# UI rule 1 and E5: the admin-typed name is escaped, the page has no script


@pytest.mark.django_db
def test_edit_escapes_the_name_and_has_no_script(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name=XSS_NAME)

    page = admin.get(_edit(location)).content.decode()
    invalid = admin.post(_edit(location), _form(location, period_s="9")).content.decode()

    for html in (page, invalid):
        assert f"<title>{ESCAPED_XSS_NAME} · Edit · Power Monitor</title>" in html
        assert f'<a class="name" href="/locations/{location.pk}/">{ESCAPED_XSS_NAME}</a>' in html
        assert f'value="{ESCAPED_XSS_NAME}"' in _input(html, "name")
        assert "<script" not in html
    assert "<h1>Edit location</h1>" in page


@pytest.mark.django_db
def test_edit_page_layout(admin: Client, location_factory: Callable[..., Any]) -> None:
    name = "x" * 100
    location = location_factory(name=name, language="ru")
    detail = f"/locations/{location.pk}/"

    page = admin.get(_edit(location)).content.decode()

    # Long-text: the 100-character name is whole in the title and the breadcrumbs.
    assert f"<title>{name} · Edit · Power Monitor</title>" in page
    trail = re.search(r'<ol class="crumbs">(.*?)</ol>', page, re.S)
    assert trail is not None
    assert re.findall(r"<li\b([^>]*)>(.*?)</li>", trail.group(1), re.S) == [
        ("", '<a href="/">Locations</a>'),
        ("", f'<a class="name" href="{detail}">{name}</a>'),
        (' aria-current="page"', "Edit"),
    ]
    # The stored values are the initial values; the only hidden field is CSRF's.
    assert _loaded_form(page) == {
        "name": name,
        "period_s": "60",
        "grace_s": "30",
        "bot_token": "",
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "ru",
    }
    form = page[page.index('<form class="form"') :]
    form = form[: form.index("</form>")]
    assert re.findall(r'<input type="hidden" name="([^"]*)"', form) == ["csrfmiddlewaretoken"]
    assert f'<p class="callout">{EDIT_NOTE}</p>' in form
    assert '<button class="btn btn--primary" type="submit">Save changes</button>' in form
    assert f'<a class="btn btn--secondary" href="{detail}">Discard changes</a>' in form
    assert "btn--danger" not in page
