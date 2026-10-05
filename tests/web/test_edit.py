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

The edit page is S6 of the 06-UI-SPEC (UI-01, UI-12, R3, R16): the app layout, the add
form's three sections with the stored values, the write-only "New bot token" whose help
shows only the mask in ``masked-token``, and the old Note callout split by topic
(amendment A6): ``edit-intro`` in the meta line, ``edit-note-monitoring`` and
``edit-note-telegram`` as info alerts. Its tests read the page only through
tests/web/pages.py and the S6 hooks; the Python copy (form errors, help, flashes) is
imported, and the template-owned copy is pinned against the 06-UI-SPEC copy table (form.*).
"""

import dataclasses
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from bs4 import Tag
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
from pages import (
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    assert_page,
    breadcrumbs,
    by_testid,
    field,
    field_error,
    form_values,
    h1,
    message_texts,
    parse,
    section,
    text,
    title,
)

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart import source
from powermon.chart.models import ChartMessage
from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.i18n import chart_texts
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.locations.validators import CHAT_ID_EMPTY, TOKEN_FORMAT
from powermon.web import location_views
from powermon.web.forms import (
    EDIT_FORM_ERROR,
    GRACE_TOO_SHORT,
    HELP_NEW_BOT_TOKEN,
    NAME_EMPTY,
    NAME_TOO_LONG,
    NEW_TOKEN_LABEL,
    PERIOD_TOO_SHORT,
    SECONDS_TOO_LONG,
    TOKEN_REPASTE_NOTE,
)
from powermon.web.location_views import (
    CHANGES_SAVED_MESSAGE,
    CHANNEL_CHANGED_MESSAGE,
    LocationEditView,
)
from powermon.worker import detection, io_loop

User = get_user_model()

KYIV = "Europe/Kyiv"
CHANGES_SAVED = CHANGES_SAVED_MESSAGE
CHANNEL_CHANGED = CHANNEL_CHANGED_MESSAGE
# Template-owned copy (06-UI-SPEC copy table, form.*; the A6 split of the old Note callout).
EDIT_TITLE = "{} · Edit"
EDIT_H1 = "Edit location"
EDIT_INTRO = "Maintenance, alerts and router grace are switched on the location page."
EDIT_NOTE_MONITORING = (
    "New period and grace values apply from the next check and never change past history. "
    "Lower values can report OFF at the next check if the device has already been silent "
    "that long."
)
EDIT_NOTE_TELEGRAM = (
    "A new chat ID or bot token moves the weekly chart: a new one is posted and pinned, and "
    "the old one is unpinned if this location's bot is an admin of the old channel. "
    "Otherwise the old pin stays: unpin it by hand in Telegram."
)
OFF_AFTER = "Reported OFF after {} s without a heartbeat."
SAVE = "Save changes"
DISCARD = "Discard changes"
SECTIONS = ["basics", "monitoring", "telegram"]
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


def _loaded_form(page: Any) -> dict[str, str]:
    """What the edit page's form posts as loaded: every control's value, the language too.

    The CSRF token is left out (the test client does not enforce it). An input without a
    value attribute posts "". The language select has exactly one selected option.
    """
    soup = page if isinstance(page, Tag) else parse(page)
    options = field(by_testid(soup, "location-form"), "language").find_all("option")
    assert len([option for option in options if option.has_attr("selected")]) == 1
    return form_values(soup, "location-form")


def _flashes(admin: Client, url: str) -> list[str]:
    """The flash messages the page at ``url`` shows, toast or legacy callout alike (UI-09)."""
    return message_texts(admin.get(url))


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


def _refused(response: Any) -> Tag:
    """An invalid edit POST's answer: 200 with S6 and the error summary as its only alert.

    The title keeps the stored name, whatever was posted.
    """
    page = assert_page(response, app=True)
    summary = by_testid(page, "error-summary")
    assert text(summary).startswith(f"Error: {EDIT_FORM_ERROR}")
    alerts = (text(element) for element in page.find_all(attrs={"role": "alert"}))
    assert [found for found in alerts if found] == [text(summary)]
    return page


def _token_help(page: Tag, masked: str) -> None:
    """The token help is HELP_NEW_BOT_TOKEN with ``masked`` alone in ``masked-token``."""
    help_text = section(page, "id_bot_token_helptext")
    assert text(help_text) == text(parse(HELP_NEW_BOT_TOKEN.format(masked=masked)))
    mask = by_testid(page, "masked-token")
    assert mask.name == "code"
    assert text(mask) == masked
    # The mask sits inside the help the token input is described by.
    assert by_testid(help_text, "masked-token") is mask


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
def test_edit_requires_sign_in_and_a_csrf_token(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    url = _edit(location)

    # Anonymous: the sign-in page, for the form and for the save (T-04-32).
    assert client.get(url).url == f"/login/?next={url}"
    anonymous = client.post(url, _form(location, name="Renamed"))
    assert anonymous.status_code == 302
    assert anonymous.url == f"/login/?next={url}"
    # Signed in but without the form's CSRF token: refused.
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(User.objects.create_user("admin", password="not-used-here"))
    assert strict.post(url, _form(location, name="Renamed")).status_code == 403

    assert Location.objects.get(pk=location.pk).name == "Office"


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
    bodies: list[str] = []
    redirects: list[str] = []

    response = admin.get(_edit(location))
    page = assert_page(response, app=True, title=EDIT_TITLE.format("Office"))
    bodies.append(response.content.decode())
    tag = field(page, "bot_token")
    assert tag.get("type") == "password"
    assert not tag.has_attr("value")
    assert (tag.get("autocomplete"), tag.get("spellcheck")) == ("off", "false")
    labels = page.find_all("label", attrs={"for": "id_bot_token"})
    assert [text(label) for label in labels] == [NEW_TOKEN_LABEL]
    _token_help(page, MASKED_TOKEN)
    loaded = _loaded_form(page)
    assert loaded["bot_token"] == ""

    # An empty token keeps the current one.
    kept = admin.post(_edit(location), loaded)
    assert kept.status_code == 302
    redirects.append(kept.url)
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN
    bodies.append(admin.get(kept.url).content.decode())

    # An invalid submit with a new token typed: the input is empty again, nothing saved.
    invalid = admin.post(_edit(location), {**loaded, "bot_token": NEW_TOKEN, "period_s": "9"})
    invalid_page = _refused(invalid)
    bodies.append(invalid.content.decode())
    assert not field(invalid_page, "bot_token").has_attr("value")
    # The help still shows only the stored token's mask, never the typed one's.
    _token_help(invalid_page, MASKED_TOKEN)
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN

    # A new valid token replaces the current one, and only its mask is ever shown.
    replaced = admin.post(_edit(location), {**loaded, "bot_token": f"  {NEW_TOKEN}  "})
    assert replaced.status_code == 302
    redirects.append(replaced.url)
    assert Location.objects.get(pk=location.pk).bot_token == NEW_TOKEN
    bodies.append(admin.get(replaced.url).content.decode())
    again = admin.get(_edit(location))
    bodies.append(again.content.decode())
    again_page = assert_page(again, app=True, title=EDIT_TITLE.format("Office"))
    _token_help(again_page, NEW_MASKED_TOKEN)
    assert not field(again_page, "bot_token").has_attr("value")

    for number, html in enumerate(bodies):
        assert_no_secrets(html, (TOKEN, SECRET, NEW_TOKEN, NEW_SECRET), label=f"body {number}")
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
    page = _refused(typed)
    assert field_error(page, "bot_token") == TOKEN_FORMAT
    tag = field(page, "bot_token")
    assert not tag.has_attr("value")
    assert text(section(page, "id_bot_token_note")) == TOKEN_REPASTE_NOTE
    assert "id_bot_token_note" in str(tag.get("aria-describedby")).split()
    assert "not-a-token" not in typed.content.decode()
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN

    # An invalid period with no token typed (or only spaces): no note to re-paste, and the
    # visible note follows the form's show_token_note just as aria-describedby does.
    for blank in ("", "   "):
        untyped = admin.post(_edit(location), {**loaded, "bot_token": blank, "period_s": "9"})
        page = _refused(untyped)
        assert field_error(page, "period_s") == PERIOD_TOO_SHORT
        assert field_error(page, "bot_token") is None
        assert page.find_all(id="id_bot_token_note") == []
        assert TOKEN_REPASTE_NOTE not in text(page)
        described = str(field(page, "bot_token").get("aria-describedby")).split()
        assert "id_bot_token_note" not in described
        # E5 error: every value is kept except the token.
        values = _loaded_form(page)
        assert (values["name"], values["period_s"], values["chat_id"]) == (
            "Office",
            "9",
            str(DEFAULT_CHAT_ID),
        )
        assert values["bot_token"] == ""
    assert Location.objects.get(pk=location.pk).period_s == 60


# K-6 on edit, and the other required fields (E5 partial, long-text)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field_name", "value", "message"),
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
    field_name: str,
    value: str,
    message: str,
) -> None:
    location = location_factory(name="Office", period_s=60, grace_s=30)

    response = admin.post(_edit(location), _form(location, name="Renamed", **{field_name: value}))

    page = _refused(response)
    assert field_error(page, field_name) == message
    # The title and the crumbs keep the stored name, not the posted one.
    assert title(page) == f"{EDIT_TITLE.format('Office')} · Power Monitor"
    assert breadcrumbs(page)[1][0] == "Office"
    saved = Location.objects.get(pk=location.pk)
    assert (saved.name, saved.period_s, saved.grace_s) == ("Office", 60, 30)
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_edit_rejects_blank_fields_and_a_long_name(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    name = field(parse(admin.get(_edit(location))), "name")
    assert name.get("maxlength") == "100"
    assert name.has_attr("autofocus")

    blank = admin.post(_edit(location), _form(location, name="", chat_id=""))
    page = _refused(blank)
    assert field_error(page, "name") == NAME_EMPTY
    assert field_error(page, "chat_id") == CHAT_ID_EMPTY
    assert "This field is required." not in blank.content.decode()

    long = admin.post(_edit(location), _form(location, name="x" * 101))
    page = _refused(long)
    assert field_error(page, "name") == NAME_TOO_LONG
    # Long-text: the h1 stays the fixed "Edit location"; the stored name stays in the title.
    assert text(h1(page)) == EDIT_H1
    assert title(page) == f"{EDIT_TITLE.format('Office')} · Power Monitor"
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
    # The same pass unpins the old chart in chat A by its message id, though chat A's hold
    # runs 10 more minutes: a release is one best-effort call, never held by the old
    # chat's refusal (D-08, W3-A1). Nothing is posted in chat B before it (Pitfall 1).
    assert _chart_steps(fake_telegram, charts_before) == [
        ("unpinChatMessage", DEFAULT_CHAT_ID, 1001)
    ]
    assert relay.not_before[io_loop.chat_key(DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID)] == _kyiv(
        "2026-10-01 12:20:30"
    )

    # The next two passes post and pin today's chart in chat B; nothing waits for 12:20:30.
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is False
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
    detail = f"/locations/{location.pk}/"

    shown = admin.get(_edit(location))
    invalid = admin.post(_edit(location), _form(location, period_s="9"))

    for response in (shown, invalid):
        # The page invariants include "no injected script" (the name is that payload).
        page = assert_page(response, status=200, app=True, title=EDIT_TITLE.format(XSS_NAME))
        html = response.content.decode()
        assert XSS_NAME not in html
        assert ESCAPED_XSS_NAME in html
        assert_no_injected_script(html, "edit page")
        assert breadcrumbs(page)[1] == (XSS_NAME, detail)
        assert field(page, "name").get("value") == XSS_NAME
        assert text(h1(page)) == EDIT_H1


@pytest.mark.django_db
def test_edit_page_layout(admin: Client, location_factory: Callable[..., Any]) -> None:
    name = "x" * 100
    location = location_factory(name=name, language="ru")
    detail = f"/locations/{location.pk}/"

    page = assert_page(admin.get(_edit(location)), app=True, title=EDIT_TITLE.format(name))

    # Long-text: the 100-character name is whole in the title, both crumb trails and the
    # sidebar's current row; the h1 is the fixed "Edit location".
    trail = [("Locations", "/"), (name, detail), ("Edit", None)]
    assert breadcrumbs(page) == trail
    assert breadcrumbs(page, "breadcrumbs-compact") == trail
    assert text(h1(page)) == EDIT_H1
    current = [
        link
        for link in all_by_testid(page, "sidebar-location")
        if link.get("aria-current") == "page"
    ]
    assert [link.get("title") for link in current] == [name]
    # The stored values are the initial values; the only hidden field is CSRF's.
    assert _loaded_form(page) == {
        "name": name,
        "period_s": "60",
        "grace_s": "30",
        "bot_token": "",
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "ru",
    }
    form = by_testid(page, "location-form")
    hidden = [
        found.get("name") for found in form.find_all("input") if found.get("type") == "hidden"
    ]
    assert hidden == ["csrfmiddlewaretoken"]
    # No destructive control on the edit page: switches and deletion live elsewhere.
    assert page.select('[data-variant="danger"], [data-variant="outline-danger"]') == []
    assert form.find_all(attrs={"role": "switch"}) == []


def _shown(element: Tag) -> str:
    """The element's text without its ``hidden`` parts: what shows before any script runs."""
    copy = parse(str(element))
    while (hidden := copy.find(hidden=True)) is not None:
        hidden.decompose()
    return text(copy)


@pytest.mark.django_db
def test_UI01_edit_form_renders(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(
        name="Office", period_s=120, grace_s=45, language="ru", bot_token=TOKEN
    )
    detail = f"/locations/{location.pk}/"

    # Expected (E5 populated): the app layout with the edit trail, the fixed h1, the intro
    # in the meta line, the add form's sections with the stored values, the masked token
    # help, the two topic notes, the hint for the stored P + G, discard then save.
    response = admin.get(_edit(location))
    page = assert_page(response, app=True, title=EDIT_TITLE.format("Office"))
    assert text(h1(page)) == EDIT_H1
    assert breadcrumbs(page) == [("Locations", "/"), ("Office", detail), ("Edit", None)]
    intro = by_testid(page, "edit-intro")
    assert text(intro) == EDIT_INTRO
    assert (intro.name, intro.find_parent("header") is not None) == ("li", True)
    form = by_testid(page, "location-form")
    assert (form.name, form.get("method"), form.get("action")) == ("form", "post", _edit(location))
    assert form.has_attr("novalidate")
    sections = all_by_testid(form, "form-section")
    assert [found.get("data-section") for found in sections] == SECTIONS
    assert _loaded_form(page) == {
        "name": "Office",
        "period_s": "120",
        "grace_s": "45",
        "bot_token": "",
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "ru",
    }
    hidden = [
        found.get("name") for found in form.find_all("input") if found.get("type") == "hidden"
    ]
    assert hidden == ["csrfmiddlewaretoken"]
    assert all_by_testid(page, "error-summary") == []

    # The write-only "New bot token": empty, its help shows only the mask (R3).
    token = field(page, "bot_token")
    assert (token.get("type"), token.has_attr("value")) == ("password", False)
    _token_help(page, MASKED_TOKEN)
    assert_no_secrets(response.content.decode(), (TOKEN, SECRET), label="edit GET")

    # The hint for the stored values, and the two notes (A6) in their sections: the
    # monitoring note after the hint, the Telegram note in the Telegram section.
    monitoring, telegram = sections[1], sections[2]
    assert _shown(by_testid(monitoring, "off-after-hint")) == OFF_AFTER.format(165)
    order = monitoring.find_all(attrs={"data-testid": ["off-after-hint", "edit-note-monitoring"]})
    assert [found["data-testid"] for found in order] == ["off-after-hint", "edit-note-monitoring"]
    for parent, testid, copy in (
        (monitoring, "edit-note-monitoring", EDIT_NOTE_MONITORING),
        (telegram, "edit-note-telegram", EDIT_NOTE_TELEGRAM),
    ):
        note = by_testid(parent, testid)
        assert note.get("data-tone") == "info"
        assert not note.has_attr("role")
        assert text(note) == copy

    # The action bar: discard (secondary, back to the location page) first, save last.
    cancel, submit = by_testid(form, "cancel"), by_testid(form, "submit")
    assert (cancel.name, cancel.get("href"), cancel.get("data-variant"), text(cancel)) == (
        "a",
        detail,
        "secondary",
        DISCARD,
    )
    assert (submit.name, submit.get("type"), submit.get("data-variant"), text(submit)) == (
        "button",
        "submit",
        "primary",
        SAVE,
    )
    order = form.find_all(attrs={"data-testid": ["cancel", "submit"]})
    assert [found["data-testid"] for found in order] == ["cancel", "submit"]
    assert form.find_all("button")[-1] is submit

    # After a rename the page shows the new stored name in the title and the crumbs.
    renamed = admin.post(_edit(location), _form(location, name="Office, 2nd floor"))
    assert renamed.status_code == 302
    again = assert_page(
        admin.get(_edit(location)), app=True, title=EDIT_TITLE.format("Office, 2nd floor")
    )
    assert breadcrumbs(again)[1] == ("Office, 2nd floor", detail)
    assert text(h1(again)) == EDIT_H1


@pytest.mark.django_db
def test_UI01_edit_page_notes_only_on_the_edit_page(admin: Client) -> None:
    # Edge: the add page shares the sections but has none of S6's notes or intro.
    page = parse(admin.get("/locations/new/"))
    for testid in ("edit-intro", "edit-note-monitoring", "edit-note-telegram", "masked-token"):
        assert all_by_testid(page, testid) == [], testid
