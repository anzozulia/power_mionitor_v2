"""Regenerate the device key (LOC-06, SEC-04; D-14, D-15, D-17; UI-D5, UI-D7, UI-D9; INV-24 #1).

- A GET confirmation page (UI-SPEC screen F), then a POST. The page shows exactly one D-15
  state block, chosen by maintenance and the stored status, and exactly one form: the
  destructive Regenerate POST. It never shows the key, not even masked. Its hidden marker
  is a salted HMAC of the key it replaces (UI-D7), so it carries no key characters.
- The POST replaces the key with one conditional UPDATE, only while the marker still
  matches the current key and the UPDATE still finds that key. It answers 200 with the
  setup page revealed (the new key in the key block and every example), ``Cache-Control:
  no-store`` and the success flash (D-14). A resubmitted or raced POST never replaces the
  key a second time: it shows the current key with the UI-D7 info flash.
- INV-24 #1: the old key gets 401 at once and changes nothing, the new key gets 200, and the
  history (timeline, outbox, chart records) is untouched.

No admin action here makes a Telegram call (KD2). The heartbeat endpoint is served through
``RequestFactory`` with an injected ``FakeClock``, as in tests/web/test_heartbeat.py.

The confirmation page (S9) is read through ``pages.py`` and the 06-UI-SPEC hooks (06-16):
the ``confirm`` root, ``confirm-title``, one ``state-block``, ``confirm-form`` with the
marker, ``keep`` and ``confirm-submit``. Its modal fragment is tested in
tests/web/test_fragments.py. The POST tests still read the revealed setup page's markup;
they move to the hooks with the setup page (06-19), so this file keeps its marker.
"""

# class-guard: pending migration

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from html import unescape
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.utils.crypto import salted_hmac
from pages import (
    all_by_testid,
    assert_page,
    breadcrumbs,
    by_testid,
    h1,
    hidden_value,
    main,
    messages,
    post_form,
    text,
)

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart.models import ChartMessage
from powermon.engine import transitions
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import actions
from powermon.locations.keys import KEY_ALPHABET, KEY_LENGTH, generate_device_key
from powermon.locations.models import Location
from powermon.web import views
from powermon.worker import io_loop

User = get_user_model()

REGENERATED_FLASH = (
    "New key saved. The old key no longer works. Copy the new key or an example below to "
    "the device."
)
ALREADY_FLASH = "The key was already regenerated. The key below is the current one."
LEAD = (
    "The old key stops working at once. Until the device has the new key, it gets HTTP 401 "
    "and its heartbeats are not recorded. The history is kept."
)
# 06-UI-SPEC copy rows regen.warning_on, regen.power_off, regen.waiting, regen.maintenance.
WARNING_BLOCK = (
    "While maintenance is off, this location can be reported OFF as soon as {off_after} "
    "seconds after its last heartbeat, and subscribers then get an OFF alert if alerts are on. "
    "Turn maintenance on first on the location page, update the device, then turn maintenance "
    "off there."
)
OFF_BLOCK = (
    "This location is off now. Until the device has the new key, the return of power is not "
    "seen: the outage is recorded until the first heartbeat with the new key, so the chart, "
    "the day totals and the ON alert (if alerts are on) count that time as off. Turn "
    "maintenance on first to have that time shown as not monitored instead."
)
WAITING_BLOCK = (
    "This location has had no heartbeat yet, so nothing is reported while you update the "
    "device."
)
MAINTENANCE_BLOCK = (
    "Maintenance is on, so OFF is not detected while you update the device. Turn maintenance "
    "off on the location page once the device sends heartbeats with the new key."
)
EXAMPLE_BLOCKS = ("example-curl", "example-cron", "example-wget-gnu", "example-wget-busybox")


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _confirm(location: Any) -> str:
    return f"/locations/{location.pk}/setup/regenerate/"


def _key(location: Any) -> str:
    return str(Location.objects.get(pk=location.pk).device_key)


def _marker(page: HttpResponse | str, location: Any) -> str:
    """The hidden marker of the confirmation's one regenerate form, whatever its markup."""
    return hidden_value(post_form(page, _confirm(location)), "marker")


def _flashes(page: str) -> list[str]:
    """The text of each flash announced as status, toast or legacy callout alike (UI-09)."""
    return [flash.text for flash in messages(page) if flash.role == "status"]


def _block(page: str, block_id: str) -> str:
    """The unescaped text of the code block with this id."""
    match = re.search(rf'<pre class="copy" id="{block_id}"><code>(.*?)</code></pre>', page, re.S)
    assert match is not None, f"no code block {block_id!r}"
    return unescape(match.group(1))


def _without_tokens(page: str) -> str:
    """The page without its CSRF token values, which are random [A-Za-z0-9] text too."""
    return re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", page)


def _heartbeat(key: str, at: datetime) -> HttpResponse:
    """GET /hb with ``key`` in the Authorization header, received at ``at``."""
    request = RequestFactory().get("/hb", HTTP_AUTHORIZATION=f"Bearer {key}")
    response: HttpResponse = views.HeartbeatView.as_view(clock=FakeClock(at))(request)
    return response


def _history() -> tuple[list[dict[str, Any]], ...]:
    """Every timeline, outbox and chart row, as stored."""
    return (
        list(PowerInterval.objects.order_by("pk").values()),
        list(OutboxMessage.objects.order_by("pk").values()),
        list(ChartMessage.objects.order_by("pk").values()),
    )


# INV-24 #1: the old key dies at once, the new one works, the history stays


@pytest.mark.django_db
def test_INV24_1_regeneration_kills_the_old_key_and_keeps_history(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office")
    other = location_factory(name="Other")
    old_key = location.device_key
    # History: an earlier closed piece, the open on piece from the heartbeats, a queued
    # alert and a chart record (posted with the location's own bot, 04-06's release rule).
    PowerInterval.objects.create(
        location=location,
        state="off",
        start_at=_at(7, 0),
        end_at=_at(7, 30),
        outage_start_at=_at(7, 0),
    )
    assert _heartbeat(old_key, _at(8, 0)).status_code == 200
    assert _heartbeat(old_key, _at(8, 1)).status_code == 200
    with transaction.atomic():
        outbox.enqueue(
            outbox.KIND_POWER_ON,
            location.pk,
            event_at=_at(8, 0),
            recorded_at=_at(8, 0),
            payload={"was_off_us": 1_800_000_000},
        )
    ChartMessage.objects.create(
        location=location,
        local_date=date(2026, 10, 1),
        chat_id=DEFAULT_CHAT_ID,
        message_id=501,
        pinned=True,
        last_rendered_at=_at(8, 0),
        created_at=_at(8, 0),
        bot_key=io_loop.bot_key(DEFAULT_BOT_TOKEN),
    )
    history = _history()
    locations = list(Location.objects.order_by("pk").values_list("pk", "name", "deleted_at"))

    confirm = admin.get(_confirm(location))
    response = admin.post(_confirm(location), {"marker": _marker(confirm, location)})

    # D-14: the setup page revealed with the new key, not cached, with the success flash.
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    page = response.content.decode()
    new_key = _key(location)
    assert new_key != old_key
    assert len(new_key) == KEY_LENGTH
    assert set(new_key) <= set(KEY_ALPHABET)
    assert _flashes(page) == [REGENERATED_FLASH]
    assert _block(page, "device-key") == new_key
    for block in EXAMPLE_BLOCKS:
        assert new_key in _block(page, block)
    assert old_key not in page
    assert f'<a class="btn btn--secondary" href="/locations/{location.pk}/setup/">Hide key</a>' in (
        page
    )
    # E7 loading: the regenerated page is server-rendered, with no script.
    assert "<script" not in page
    # Only that location's key changed.
    assert _key(other) == other.device_key

    # The old key gets 401 at once and changes nothing.
    states = list(LocationState.objects.order_by("pk").values())
    rejected = _heartbeat(old_key, _at(8, 2))
    assert rejected.status_code == 401
    assert list(LocationState.objects.order_by("pk").values()) == states
    assert _history() == history

    # The new key gets 200: a plain heartbeat, so the history still has the same rows.
    assert _heartbeat(new_key, _at(8, 3)).status_code == 200
    assert LocationState.objects.get(location=location).last_heartbeat_at == _at(8, 3)
    assert _history() == history
    assert list(Location.objects.order_by("pk").values_list("pk", "name", "deleted_at")) == (
        locations
    )
    assert len(fake_telegram.calls) == 0


# UI-D7: a resubmitted or raced POST never replaces the key a second time


@pytest.mark.django_db
def test_regenerate_resubmit_never_replaces_the_key_twice(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office")
    marker = _marker(admin.get(_confirm(location)), location)

    first = admin.post(_confirm(location), {"marker": marker})
    regenerated = _key(location)
    # A reload, a double click or a second tab sends the same form again.
    second = admin.post(_confirm(location), {"marker": marker})

    assert regenerated != location.device_key
    assert _key(location) == regenerated
    assert _flashes(first.content.decode()) == [REGENERATED_FLASH]
    page = second.content.decode()
    assert second.status_code == 200
    assert "no-store" in second["Cache-Control"]
    assert _flashes(page) == [ALREADY_FLASH]
    assert _block(page, "device-key") == regenerated
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_regenerate_without_a_marker_changes_nothing(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    for data in ({}, {"marker": ""}, {"marker": "0" * 64}, {"marker": location.device_key}):
        page = admin.post(_confirm(location), data).content.decode()
        assert _flashes(page) == [ALREADY_FLASH]

    assert _key(location) == location.device_key


@pytest.mark.django_db
def test_regenerate_race_one_winner(
    admin: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The marker is valid, but another tab's regenerate commits between this POST's read
    # of the key and its UPDATE: the conditional UPDATE finds the old key gone.
    location = location_factory(name="Office")
    marker = _marker(admin.get(_confirm(location)), location)
    real = actions.regenerate_key
    outcomes: list[bool] = []

    def raced(location_id: int, current_key: str) -> bool:
        assert real(location_id, current_key) is True
        outcomes.append(real(location_id, current_key))
        return outcomes[-1]

    monkeypatch.setattr(actions, "regenerate_key", raced)

    page = admin.post(_confirm(location), {"marker": marker}).content.decode()

    assert outcomes == [False]
    winner = _key(location)
    assert winner != location.device_key
    assert _flashes(page) == [ALREADY_FLASH]
    assert _block(page, "device-key") == winner


@pytest.mark.django_db
def test_regenerate_key_action_is_conditional(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    old_key = location.device_key

    assert actions.regenerate_key(location.pk, old_key) is True
    new_key = _key(location)
    # The same old key again (a stale caller) and a wrong key change nothing.
    assert actions.regenerate_key(location.pk, old_key) is False
    assert actions.regenerate_key(location.pk, generate_device_key()) is False
    assert _key(location) == new_key
    # A deleted location keeps its tombstoned key.
    assert actions.regenerate_key(gone.pk, gone.device_key) is False
    assert _key(gone) == gone.device_key


# D-15, UI-D9: the confirmation page and its one state block


def _set(location: Any, *, maintenance: bool, status: str) -> None:
    Location.objects.filter(pk=location.pk).update(maintenance=maintenance)
    if status == "waiting":
        return
    LocationState.objects.filter(location=location).update(
        status=status,
        on_since=_at(8, 0),
        last_heartbeat_at=_at(8, 0),
        outage_started_at=_at(8, 0) if status == "off" else None,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("maintenance", "status", "block", "tone"),
    [
        (False, "on", "warning-on", "warning"),
        (False, "off", "power-off", "info"),
        (False, "waiting", "waiting", "info"),
        (True, "on", "maintenance", "info"),
        (True, "off", "maintenance", "info"),
        (True, "waiting", "maintenance", "info"),
    ],
)
def test_regenerate_confirmation_blocks(
    admin: Client,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    maintenance: bool,
    status: str,
    block: str,
    tone: str,
) -> None:
    location = location_factory(name="Office", period_s=45, grace_s=20)
    _set(location, maintenance=maintenance, status=status)

    response = admin.get(_confirm(location))

    assert "no-store" in response["Cache-Control"]
    # assert_page also proves there is no injected or inline script (R5).
    soup = assert_page(response, app=True, title="Office · Regenerate key")
    root = by_testid(main(soup), "confirm")
    assert text(h1(soup)) == "Regenerate the device key?"
    assert LEAD in text(root)
    expected = {
        "warning-on": WARNING_BLOCK.format(off_after=65),
        "power-off": OFF_BLOCK,
        "waiting": WAITING_BLOCK,
        "maintenance": MAINTENANCE_BLOCK,
    }
    # Exactly one state block (D-15), its tone and its copy.
    [state] = all_by_testid(soup, "state-block")
    assert (state.get("data-state-block"), state.get("data-tone")) == (block, tone)
    assert expected[block] in text(state)
    # The "maintenance first" path is a plain link, never a second form (UI-D9).
    links = [(link.get("href"), text(link)) for link in state.find_all("a")]
    expected_links = [(f"/locations/{location.pk}/", "Open the location page")]
    assert links == (expected_links if block == "warning-on" else [])
    # Exactly one form in main: the destructive POST with its marker (UI-D9), no autofocus.
    [form] = main(soup).find_all("form")
    assert form is post_form(soup, _confirm(location))
    assert form is by_testid(root, "confirm-form")
    assert form.find("input", attrs={"name": "csrfmiddlewaretoken"}) is not None
    assert hidden_value(form, "marker") == _marker(soup, location)
    submit = by_testid(form, "confirm-submit")
    assert (submit.get("type"), submit.get("data-variant"), text(submit)) == (
        "submit",
        "danger",
        "Regenerate key",
    )
    assert soup.find_all(autofocus=True) == []
    page = main(soup)
    dangers = page.find_all(attrs={"data-variant": "danger"})
    assert [found.get("data-testid") for found in dangers] == ["confirm-submit"]
    assert page.find_all(attrs={"data-variant": "primary"}) == []
    keep = by_testid(root, "keep")
    assert (keep.get("href"), text(keep)) == (
        f"/locations/{location.pk}/setup/",
        "Keep current key",
    )
    # Keep comes first, the destructive button last.
    controls = root.find_all(["a", "button"])
    assert controls[-2:] == [keep, submit]
    # Nothing was changed by the GET.
    assert _key(location) == location.device_key
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_regenerate_confirmation_shows_no_key(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # A key whose tail has upper-case letters, which a hex marker can never contain.
    key = generate_device_key()[:28] + "QZXK"
    location = location_factory(name="Office", device_key=key)

    page = _without_tokens(admin.get(_confirm(location)).content.decode())

    assert key not in page
    assert "•" * 12 + key[-4:] not in page
    assert key[-4:] not in page
    # UI-D7: the marker is a salted HMAC of the key, with no key characters (T-04-18).
    expected = salted_hmac("powermon.regenerate-key", key, algorithm="sha256").hexdigest()
    assert _marker(page, location) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)


@pytest.mark.django_db
def test_regenerate_breadcrumbs_and_long_name(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "x" * 100
    location = location_factory(name=name)
    xss = location_factory(name="<script>alert(1)</script>")

    page = assert_page(admin.get(_confirm(location)), app=True, title=f"{name} · Regenerate key")
    escaped = assert_page(
        admin.get(_confirm(xss)), app=True, title="<script>alert(1)</script> · Regenerate key"
    )

    # E10 long-text: the whole name in both trails, never truncated.
    trail = [
        ("Locations", "/"),
        (name, f"/locations/{location.pk}/"),
        ("Device setup", f"/locations/{location.pk}/setup/"),
        ("Regenerate key", None),
    ]
    assert breadcrumbs(page) == trail
    assert breadcrumbs(page, "breadcrumbs-compact") == trail
    # The h1 carries no name; the name is escaped everywhere it shows (R1).
    assert text(h1(page)) == "Regenerate the device key?"
    assert breadcrumbs(escaped)[1] == ("<script>alert(1)</script>", f"/locations/{xss.pk}/")
    assert escaped.find_all("script", string="alert(1)") == []


# Failure cases: unknown, deleted, anonymous, GET has no effect


@pytest.mark.django_db
def test_regenerate_unknown_or_deleted_is_404(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    unknown = gone.pk + 1000

    for pk in (gone.pk, unknown):
        url = f"/locations/{pk}/setup/regenerate/"
        assert admin.get(url).status_code == 404
        assert admin.post(url, {"marker": "0" * 64}).status_code == 404

    assert _key(gone) == gone.device_key
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_regenerate_anonymous_redirects_to_sign_in(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    url = _confirm(location)

    for response in (client.get(url), client.post(url, {"marker": "0" * 64})):
        assert response.status_code == 302
        assert response.url == f"/login/?next={url}"

    assert _key(location) == location.device_key


@pytest.mark.django_db
def test_regenerate_without_a_csrf_token_is_refused(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(User.objects.create_user("admin", password="not-used-here"))
    page = browser.get(_confirm(location)).content.decode()

    response = browser.post(_confirm(location), {"marker": _marker(page, location)})

    assert response.status_code == 403
    assert _key(location) == location.device_key


@pytest.mark.django_db
def test_regenerate_page_records_nothing_for_a_heartbeat_in_between(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # A heartbeat between the confirmation and the POST changes nothing about the guard:
    # the marker is of the key, not of the state, so the POST still regenerates once.
    location = location_factory(name="Office")
    marker = _marker(admin.get(_confirm(location)), location)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"

    page = admin.post(_confirm(location), {"marker": marker}).content.decode()

    assert _flashes(page) == [REGENERATED_FLASH]
    assert _key(location) != location.device_key
    assert LocationState.objects.get(location=location).last_heartbeat_at == _at(8, 0)
