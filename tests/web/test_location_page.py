"""The location page S5 (LOC-03; D-13, SEC-04; 06-UI-SPEC Page Contracts › S5; UI-01, UI-12).

- The page extends the app layout: breadcrumbs Locations › {name}; a header
  (``location-header``) with the h1 name, the status pill and the tags; the meta line
  (``location-meta``) with "On since" / "Outage since" (none while waiting) and "Last
  heartbeat"; the page actions (Edit and the kebab); the section nav; the seven cards in the
  test-pinned DOM order status, controls, weekly-chart, recent-outages, settings,
  device-setup, danger-zone.
- The Status card (``status-panel``) uses the one Phase 4 vocabulary: "Maintenance" whenever
  the flag is on, else the stored status. Under maintenance the stored status shows as the
  power state; its help line is in the maintenance banner. Rows that do not apply are left
  out.
- Its last row, Delivery, shows "OK" with its help, or the failing pill "Failing since
  {display_time} ({code})"; the cause line for that code (or the supergroup line with the
  new chat ID) and the retry line are in the delivery-failing banner (D-10, D-13).
- The settings list is the setup page's, shared through one partial, so both pages show
  identical values; with router grace on, "Reported OFF after" names the longer timeout
  right after power returns.
- The page shows no device key, not even masked, and the bot token only masked (SEC-04).
- The admin-typed name is escaped everywhere and never truncated (E2).
- Every location URL answers 404 for an unknown or deleted location, and every page needs
  the signed-in admin.

Pages are read through tests/web/pages.py and the 06-UI-SPEC test hooks only. Python-owned
copy is imported; template-owned copy is pinned against the 06-UI-SPEC copy table. The view
clock is pinned (``clock``), so the relative times are known.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client
from django.urls import reverse
from pages import (
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    assert_page,
    breadcrumbs,
    by_testid,
    code_block,
    definitions,
    h1,
    hidden_value,
    main,
    messages,
    parse,
    post_form,
    section,
    text,
    title,
)
from secret_fixtures import MASKED as MASKED_TOKEN
from secret_fixtures import SECRET, TOKEN

from powermon.alerts import delivery
from powermon.engine import rules
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import examples, keys
from powermon.locations.models import Location
from powermon.web.location_views import (
    ALERTS_COPY,
    DELIVERY_BOT_REJECTED_CAUSE,
    DELIVERY_CANNOT_POST_CAUSE,
    DELIVERY_MIGRATE_LINE,
    DELIVERY_NOT_IN_CHAT_CAUSE,
    DELIVERY_OK_HELP,
    DELIVERY_OTHER_CAUSE,
    DELIVERY_RETRY_LINE,
    MAINTENANCE_COPY,
    LocationDetailView,
)
from powermon.web.status import STATUS_LABELS

User = get_user_model()

XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
ROUTER_GRACE_S = int(rules.ROUTER_GRACE.total_seconds())
# 06-UI-SPEC copy table, loc.banner_mnt_body.
HELP_POWER_ON = "OFF is not detected during maintenance."
HELP_POWER_OFF = "The outage goes on. When power returns, the ON alert is sent as usual."
MIGRATED_CHAT_ID = -1001234567999
MIGRATE_LINE = DELIVERY_MIGRATE_LINE.format(new_chat_id=MIGRATED_CHAT_ID)
DELIVERY_OK_ROW = ("Delivery", f"OK {DELIVERY_OK_HELP}")
# 06-UI-SPEC copy table, loc.setup_intro, loc.test_body, loc.delete_desc.
DEVICE_SETUP_SENTENCE = "Heartbeat URL, device key and copy-paste examples for the device."
TEST_MESSAGE_PARAGRAPH = (
    "Sends one silent message to the channel with this location's bot, to check the bot "
    "token and the chat ID. It is not an alert: it is sent even while alerts are off or "
    "maintenance is on. Telegram can take up to 15 seconds to answer."
)
DELETE_SENTENCE = (
    "Stops this location's alerts, drops the alerts still queued, unpins its weekly chart "
    "where the bot still can, and hides it from the admin panel. There is no undo."
)
# The cards in their test-pinned DOM order, with their titles (loc.cards).
CARDS = [
    ("status", "Status"),
    ("controls", "Controls"),
    ("weekly-chart", "Weekly chart"),
    ("recent-outages", "Recent outages"),
    ("settings", "Settings"),
    ("device-setup", "Device setup"),
    ("danger-zone", "Danger zone"),
]
# The section nav (loc.sections).
SECTION_LINKS = [
    ("Overview", "#status"),
    ("Chart", "#weekly-chart"),
    ("Outages", "#recent-outages"),
    ("Settings", "#settings"),
    ("Danger zone", "#danger-zone"),
]
# The page's "now": 2026-10-01 08:00:12 UTC, 11:00:12 in Kyiv.
NOW = datetime(2026, 10, 1, 8, 0, 12, tzinfo=UTC)


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """Pin the display TZ, so the expected times do not depend on the env file."""
    settings.TIME_ZONE = "Europe/Kyiv"
    return settings


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The location page's clock at ``NOW``: its relative times are then known."""
    fake = FakeClock(NOW)
    monkeypatch.setattr(LocationDetailView, "clock", fake)
    return fake


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _page(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _set_state(location: Any, **fields: Any) -> None:
    LocationState.objects.filter(location=location).update(**fields)


def _set_maintenance(location: Any) -> None:
    Location.objects.filter(pk=location.pk).update(maintenance=True)


def _get(admin: Client, location: Any) -> Any:
    """The location page, parsed."""
    response = admin.get(_page(location))
    assert response.status_code == 200
    return parse(response)


def _status_rows(page: Any) -> list[tuple[str, str]]:
    """The Status card as (term, value) pairs, in order."""
    return definitions(page, "status-panel")


def _status_row(page: Any, term: str) -> Tag:
    """The ``dd`` of the Status card row named ``term``."""
    panel = by_testid(page, "status-panel")
    for dt in panel.find_all("dt"):
        if text(dt) == term:
            dd = dt.find_next_sibling("dd")
            assert isinstance(dd, Tag)
            return dd
    raise AssertionError(f"no Status row {term!r}")


def _delivery_value(page: Any) -> Tag:
    """The Status card's Delivery value, ``[data-delivery]``."""
    found = by_testid(page, "status-panel").select("[data-delivery]")
    assert len(found) == 1, f"expected one [data-delivery] in the Status card, found {len(found)}"
    return found[0]


def _meta_items(page: Any) -> list[str]:
    """The meta line's items as text, the JS-only live-chip slot left out."""
    meta = by_testid(by_testid(page, "location-header"), "location-meta")
    items = [li for li in meta.find_all("li", recursive=False) if isinstance(li, Tag)]
    return [text(li) for li in items if li.get("data-testid") != "live-chip"]


def _tags(page: Any) -> list[str]:
    header = by_testid(page, "location-header")
    return [str(tag["data-tag"]) for tag in all_by_testid(header, "tag")]


def _header_pill(page: Any) -> Tag:
    return by_testid(by_testid(page, "location-header"), "status-pill")


# The shell, the header and the card order (UI-01, UI-12)


@pytest.mark.django_db
def test_UI01_location_page_shell(
    admin: Client, kyiv: Any, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Kyiv office", alerts_enabled=False, router_grace=True)
    _set_state(location, status="on", on_since=_at(7, 30), last_heartbeat_at=_at(8, 0))
    other = location_factory(name="Lviv home")

    response = admin.get(_page(location))

    soup = assert_page(response, title="Kyiv office", app=True)
    assert breadcrumbs(soup) == [("Locations", "/"), ("Kyiv office", None)]
    # The sidebar marks this location as the current one, and only it.
    current = {
        str(link["data-location-id"]): link.get("aria-current")
        for link in all_by_testid(soup, "sidebar-location")
    }
    assert current == {str(location.pk): "page", str(other.pk): None}
    # The header: the h1 name, the status pill, the tags in the order Alerts off, Router grace.
    header = by_testid(soup, "location-header")
    assert header.find("h1") is h1(soup)
    assert text(h1(soup)) == "Kyiv office"
    pill = _header_pill(soup)
    assert (pill["data-status"], text(pill)) == ("on", STATUS_LABELS["on"])
    assert _tags(soup) == ["alerts-off", "router-grace"]
    actions = by_testid(soup, "page-actions")
    edit = by_testid(actions, "header-edit-location")
    assert (edit["href"], text(edit)) == (f"/locations/{location.pk}/edit/", "Edit location")
    # The seven cards in the pinned DOM order, each a section named by its title.
    cards = [found for found in soup.find_all("section") if isinstance(found, Tag)]
    assert [card.get("id") for card in cards] == [card_id for card_id, _ in CARDS]
    for card, (_, card_title) in zip(cards, CARDS, strict=True):
        labelled = soup.find(id=str(card["aria-labelledby"]))
        assert isinstance(labelled, Tag)
        assert text(labelled) == card_title
        assert card.get("tabindex") == "-1"
    # The section nav jumps to five of the cards.
    nav = by_testid(soup, "section-nav")
    assert (nav.name, nav.get("aria-label")) == ("nav", "Sections")
    links = [(text(link), link["href"]) for link in nav.find_all("a")]
    assert links == SECTION_LINKS
    for _, href in links:
        assert section(soup, str(href)[1:]).name == "section"


@pytest.mark.django_db
def test_UI01_location_meta(
    admin: Client, kyiv: Any, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    on = location_factory(name="On")
    _set_state(on, status="on", on_since=_at(7, 30), last_heartbeat_at=_at(8, 0))
    off = location_factory(name="Off")
    _set_state(off, status="off", outage_started_at=_at(7, 58), last_heartbeat_at=_at(7, 58))
    waiting = location_factory(name="Waiting")

    on_page = _get(admin, on)
    assert _meta_items(on_page) == [
        "On since 2026-10-01 10:30:00 EEST (30 min ago)",
        "Last heartbeat 2026-10-01 11:00:00 EEST (12 s ago)",
    ]
    # Each time is a <time datetime> of the stored instant beside one [data-relative].
    meta = by_testid(on_page, "location-meta")
    stamps = [datetime.fromisoformat(str(found["datetime"])) for found in meta.find_all("time")]
    assert stamps == [_at(7, 30), _at(8, 0)]
    relatives = [found["data-relative"] for found in meta.select("[data-relative]")]
    assert relatives == [found["datetime"] for found in meta.find_all("time")]

    assert _meta_items(_get(admin, off)) == [
        "Outage since 2026-10-01 10:58:00 EEST (2 min ago)",
        "Last heartbeat 2026-10-01 10:58:00 EEST (2 min ago)",
    ]
    # Edge: waiting has no since item, and "Never" has no <time>.
    waiting_page = _get(admin, waiting)
    assert _meta_items(waiting_page) == ["Last heartbeat Never"]
    assert by_testid(waiting_page, "location-meta").find("time") is None


# The Status card (LOC-03, E2)


@pytest.mark.django_db
def test_LOC03_location_page_status_panel(
    admin: Client, kyiv: Any, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    on = location_factory(name="On")
    _set_state(on, status="on", on_since=_at(7, 30), last_heartbeat_at=_at(8, 0))
    off = location_factory(name="Off")
    _set_state(off, status="off", outage_started_at=_at(7, 58), last_heartbeat_at=_at(7, 58))
    waiting = location_factory(name="Waiting")
    stateless = location_factory(name="No state row")
    LocationState.objects.filter(location=stateless).delete()

    on_since = ("On since", "2026-10-01 10:30:00 EEST (30 min ago)")
    outage_since = ("Outage since", "2026-10-01 10:58:00 EEST (2 min ago)")
    on_beat = ("Last heartbeat", "2026-10-01 11:00:00 EEST (12 s ago)")
    off_beat = ("Last heartbeat", "2026-10-01 10:58:00 EEST (2 min ago)")
    never = ("Last heartbeat", "Never")

    # Delivery is the last row, "OK" while no delivery-failing incident is open (D-13).
    assert _status_rows(_get(admin, on)) == [("Status", "On"), on_since, on_beat, DELIVERY_OK_ROW]
    assert _status_rows(_get(admin, off)) == [
        ("Status", "Off"),
        outage_since,
        off_beat,
        DELIVERY_OK_ROW,
    ]
    # Waiting: no On since / Outage since row (E2), and no state row counts as waiting.
    for place in (waiting, stateless):
        page = _get(admin, place)
        assert _status_rows(page) == [
            ("Status", "Waiting for first heartbeat"),
            never,
            DELIVERY_OK_ROW,
        ]
        assert not all_by_testid(page, "maintenance-banner")

    for place in (on, off, waiting):
        _set_maintenance(place)
    maintenance_on = _get(admin, on)

    assert _status_rows(maintenance_on) == [
        ("Status", "Maintenance"),
        ("Power state", "On"),
        on_since,
        on_beat,
        DELIVERY_OK_ROW,
    ]
    # The maintenance label has its own pill; the power state keeps its own; its help line
    # is in the maintenance banner.
    assert _header_pill(maintenance_on)["data-status"] == "maintenance"
    power = by_testid(maintenance_on, "status-panel").select("[data-power]")
    assert [found["data-power"] for found in power] == ["on"]
    assert by_testid(power[0], "status-pill")["data-status"] == "on"
    banner = by_testid(maintenance_on, "maintenance-banner")
    assert text(banner) == f"Maintenance is on {HELP_POWER_ON}"
    maintenance_off = _get(admin, off)
    assert _status_rows(maintenance_off) == [
        ("Status", "Maintenance"),
        ("Power state", "Off"),
        outage_since,
        off_beat,
        DELIVERY_OK_ROW,
    ]
    assert text(by_testid(maintenance_off, "maintenance-banner")) == (
        f"Maintenance is on {HELP_POWER_OFF}"
    )
    maintenance_waiting = _get(admin, waiting)
    assert _status_rows(maintenance_waiting) == [
        ("Status", "Maintenance"),
        ("Power state", "Waiting for first heartbeat"),
        never,
        DELIVERY_OK_ROW,
    ]
    assert text(by_testid(maintenance_waiting, "maintenance-banner")) == (
        f"Maintenance is on {HELP_POWER_ON}"
    )


# The Delivery row (LOC-03, D-10, D-13)


@pytest.mark.django_db
def test_location_page_delivery_row_ok(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    # Another location's failure and this location's closed one never show here.
    other = location_factory(name="Other")
    with transaction.atomic():
        delivery.open_failing(other.pk, _at(7, 0), 403)
        delivery.open_failing(location.pk, _at(7, 0), 403)
        delivery.close_failing(location.pk, _at(7, 30))

    page = _get(admin, location)

    # Plain "OK" with its help line, no failing pill and no banner.
    value = _delivery_value(page)
    assert (value["data-delivery"], text(value)) == ("ok", f"OK {DELIVERY_OK_HELP}")
    assert not all_by_testid(page, "delivery-banner")
    assert _status_rows(page)[-1] == DELIVERY_OK_ROW


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "migrate_to", "cause"),
    [
        (403, None, DELIVERY_CANNOT_POST_CAUSE),
        (400, None, DELIVERY_NOT_IN_CHAT_CAUSE),
        (401, None, DELIVERY_BOT_REJECTED_CAUSE),
        (404, None, DELIVERY_BOT_REJECTED_CAUSE),
        (409, None, DELIVERY_OTHER_CAUSE),
        # Telegram reported a supergroup: the migrate line replaces the cause line (D-10).
        (400, MIGRATED_CHAT_ID, MIGRATE_LINE),
        (403, MIGRATED_CHAT_ID, MIGRATE_LINE),
    ],
    ids=["http_403", "http_400", "http_401", "http_404", "other", "migrate-400", "migrate-403"],
)
def test_location_page_delivery_row(
    admin: Client,
    kyiv: Any,
    clock: FakeClock,
    location_factory: Callable[..., Any],
    status: int,
    migrate_to: int | None,
    cause: str,
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    with transaction.atomic():
        delivery.open_failing(location.pk, _at(8, 0, 2), status, migrate_to)

    response = admin.get(_page(location))
    page = parse(response)

    # The full display_time of the start (seconds and zone), whatever the day (UI-D6), in
    # the failing pill; its relative time beside it.
    value = _delivery_value(page)
    assert value["data-delivery"] == "failing"
    pill = value.select_one("[data-label]")
    assert pill is not None
    assert text(pill) == f"Failing since 2026-10-01 11:00:02 EEST (http_{status})"
    assert text(value) == f"Failing since 2026-10-01 11:00:02 EEST (http_{status}) 10 s ago"
    assert _status_rows(page)[-1][0] == "Delivery"
    # The cause (or supergroup) line and the retry line are in the banner.
    banner = by_testid(page, "delivery-banner")
    assert text(by_testid(banner, "delivery-cause")) == cause
    assert text(by_testid(banner, "delivery-retry")) == DELIVERY_RETRY_LINE
    # The chat stays as stored: the new ID is only shown (PITFALLS 6e); no secret shows.
    assert Location.objects.get(pk=location.pk).chat_id != MIGRATED_CHAT_ID
    html = response.content.decode()
    assert TOKEN not in html
    assert SECRET not in html


# Cards: settings (shared with the setup page) and device setup


@pytest.mark.django_db
def test_location_page_settings_and_setup_sections(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(
        name="Office",
        bot_token=TOKEN,
        period_s=45,
        grace_s=20,
        chat_id=-1009876543210,
        language="ru",
        router_grace=True,
    )

    page = _get(admin, location)

    # The setup page shows these same rows: *_on_setup in tests/web/test_setup_page.py.
    expected = [
        ("Language", "Russian"),
        ("Chart update period", "15 min"),
        ("Heartbeat period", "45 s"),
        ("Grace period", "20 s"),
        (
            "Reported OFF after",
            f"65 s without a heartbeat ({65 + ROUTER_GRACE_S} s right after power returns, "
            "router grace on)",
        ),
        ("Channel chat ID", "-1009876543210"),
        ("Bot token", "987654321, the rest is hidden"),
    ]
    assert ROUTER_GRACE_S == 180
    settings_card = section(page, "settings")
    assert definitions(settings_card, "settings-panel") == expected
    # The token shows only as its aria-hidden mask (R3).
    mask = by_testid(settings_card, "masked-token").find("code")
    assert isinstance(mask, Tag)
    assert (mask.get_text(), mask.get("aria-hidden")) == (MASKED_TOKEN, "true")
    # "Edit" is the Settings card's header action, a link to the edit form.
    edit = by_testid(settings_card, "edit-location")
    assert (edit.name, edit["href"], text(edit)) == (
        "a",
        f"/locations/{location.pk}/edit/",
        "Edit location",
    )
    device = section(page, "device-setup")
    assert DEVICE_SETUP_SENTENCE in text(device)
    setup = by_testid(device, "open-setup")
    assert (setup.name, setup["href"], text(setup)) == (
        "a",
        f"/locations/{location.pk}/setup/",
        "Open device setup",
    )

    Location.objects.filter(pk=location.pk).update(router_grace=False)

    plain = _get(admin, location)
    assert ("Reported OFF after", "65 s without a heartbeat") in definitions(
        plain, "settings-panel"
    )


@pytest.mark.django_db
def test_location_page_delete_section(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")

    page = _get(admin, location)

    # The last card's Delete row: a sentence and a link that only opens the confirmation
    # page; the destructive button is on that page, not here (R7).
    danger = section(page, "danger-zone")
    row = section(page, "delete-location")
    assert danger.find(id="delete-location") is row
    assert DELETE_SENTENCE in text(row)
    link = by_testid(row, "delete-location")
    assert (link.name, link["href"], text(link)) == (
        "a",
        f"/locations/{location.pk}/delete/",
        "Delete location…",
    )
    assert link.has_attr("data-confirm")
    assert danger.find("form") is None
    assert not page.select('[data-variant="danger"]')


# Secrets (SEC-04) and the admin-typed name (UI rule 1, E2)


@pytest.mark.django_db
def test_location_page_shows_no_secret(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    key = location.device_key

    page = admin.get(_page(location)).content.decode()
    switched = admin.post(f"/locations/{location.pk}/maintenance/", {"value": "on"}, follow=True)

    for html in (page, switched.content.decode()):
        assert key not in html
        assert keys.mask_key(key) not in html
        assert TOKEN not in html
        assert SECRET not in html
        assert MASKED_TOKEN in html
    for _url, _status in switched.redirect_chain:
        assert key not in _url
        assert SECRET not in _url


@pytest.mark.django_db
def test_location_page_escapes_the_name(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name=XSS_NAME)

    response = admin.get(_page(location))
    html = response.content.decode()
    page = parse(response)

    # The name is text everywhere: the h1, the title, the breadcrumbs, the sidebar.
    assert text(h1(page)) == XSS_NAME
    assert title(page) == f"{XSS_NAME} · Power Monitor"
    assert breadcrumbs(page)[-1] == (XSS_NAME, None)
    sidebar = [
        link
        for link in all_by_testid(page, "sidebar-location")
        if link["data-location-id"] == str(location.pk)
    ]
    assert [link["title"] for link in sidebar] == [XSS_NAME]
    assert ESCAPED_XSS_NAME in html
    # Plain forms and static links: no injected or inline script. The setup page's half is
    # *_on_setup in tests/web/test_setup_page.py (06-09).
    assert_no_injected_script(html, "location page")


@pytest.mark.django_db
def test_long_name_is_shown_whole(admin: Client, location_factory: Callable[..., Any]) -> None:
    name = "x" * 100
    location = location_factory(name=name)

    page = _get(admin, location)

    assert text(h1(page)) == name
    assert title(page) == f"{name} · Power Monitor"
    assert breadcrumbs(page)[-1] == (name, None)
    assert breadcrumbs(page, "breadcrumbs-compact")[-1] == (name, None)


# Breadcrumbs (UI-D2, E10) and the flash after a switch (UI-09)


@pytest.mark.django_db
def test_breadcrumbs_on_location_and_setup_pages(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    detail = f"/locations/{location.pk}/"

    response = admin.post(f"{detail}maintenance/", {"value": "on"}, follow=True)
    page = parse(response)

    assert breadcrumbs(page) == [("Locations", "/"), ("Office", None)]
    assert breadcrumbs(page, "breadcrumbs-compact") == [("Locations", "/"), ("Office", None)]
    # The top-bar breadcrumbs come before the h1; the flash is a toast, not page content.
    # The setup page's half is *_on_setup in tests/web/test_setup_page.py (06-09).
    elements = list(page.find_all(True))
    assert elements.index(by_testid(page, "breadcrumbs")) < elements.index(h1(page))
    assert [(message.level, message.text) for message in messages(page)] == [
        ("success", MAINTENANCE_COPY["on"])
    ]


# Primary and destructive buttons (06-UI-SPEC Components › Button)


@pytest.mark.django_db
def test_location_page_has_one_accent_button_at_most(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory()

    page = _get(admin, location)

    # With delivery OK the page has no primary button: the only primary on S5 is the
    # delivery banner's fix. The Controls card's "Send test message" is secondary, in its
    # own POST form (CSRF) to the test-message URL, and the destructive style is used only
    # on the confirmation pages.
    assert not page.select('[data-variant="primary"]')
    form = post_form(page, f"/locations/{location.pk}/test-message/")
    assert form is by_testid(page, "test-message-form")
    assert form.find("input", attrs={"name": "csrfmiddlewaretoken"}) is not None
    buttons = [found for found in form.find_all("button") if isinstance(found, Tag)]
    assert [(b.get("type"), b.get("data-variant"), text(b)) for b in buttons] == [
        ("submit", "secondary", "Send test message")
    ]
    assert not page.select('[data-variant="danger"]')


@pytest.mark.django_db
def test_location_page_test_message_section(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    response = admin.get(_page(location))
    page = parse(response)

    # 06-UI-SPEC S5 Controls: the three switches, then the Test message block: its micro
    # heading, the paragraph, then a plain POST form (the submit guard only adds a pending
    # label, E4).
    controls = section(page, "controls")
    forms = [found for found in controls.find_all("form") if isinstance(found, Tag)]
    assert [form.get("data-testid") for form in forms] == [
        "switch",
        "switch",
        "switch",
        "test-message-form",
    ]
    assert "Test message" in [text(found) for found in controls.find_all("h3")]
    assert TEST_MESSAGE_PARAGRAPH in text(controls)
    assert_no_injected_script(response.content.decode(), "location page")


# Edge responses: unknown or deleted locations, anonymous visitors


@pytest.mark.django_db
def test_location_urls_answer_404_for_unknown_or_deleted(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))
    unknown = gone.pk + 1000

    for pk in (gone.pk, unknown):
        assert admin.get(f"/locations/{pk}/").status_code == 404
        assert admin.post(f"/locations/{pk}/maintenance/", {"value": "on"}).status_code == 404

    assert Location.objects.get(pk=gone.pk).maintenance is False


@pytest.mark.django_db
def test_anonymous_location_page_redirects_to_sign_in(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory()
    page = f"/locations/{location.pk}/"
    switch = f"/locations/{location.pk}/maintenance/"

    assert client.get(page).url == f"/login/?next={page}"
    response = client.post(switch, {"value": "on"})

    assert response.status_code == 302
    assert response.url == f"/login/?next={switch}"
    assert Location.objects.get(pk=location.pk).maintenance is False


# The flash of a switch after the redirect is a toast on this page (UI-09)


@pytest.mark.django_db
def test_location_page_shows_the_switch_flash_as_a_toast(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    page = parse(admin.post(f"/locations/{location.pk}/alerts/", {"value": "off"}, follow=True))

    assert [(m.level, m.role, m.text) for m in messages(page)] == [
        ("success", "status", ALERTS_COPY["off"])
    ]
    assert _tags(page) == ["alerts-off"]


# S5 behaviour (06-UI-SPEC Page Contracts › S5; UI-01, UI-05, UI-07, UI-08, UI-12, R4, R7,
# R13). Histories are seeded straight into the timeline, as the engine would leave them.

RESET_REFUSED_IN_PROGRESS = (
    "An outage is in progress. Reset the history after power returns, or delete the location."
)
RESET_REFUSED_NO_HISTORY = "There is no power history to reset."


def _timeline(location: Any, *pieces: tuple[str, datetime, datetime | None]) -> None:
    """Seed the location's timeline; each off piece is its own outage."""
    for state, start, end in pieces:
        PowerInterval.objects.create(
            location=location,
            state=state,
            start_at=start,
            end_at=end,
            outage_start_at=start if state == "off" else None,
        )


def _power(location: Any, power: str) -> None:
    """Leave the location on since 07:30 or off since 07:58 (UTC), with that history; or
    waiting with none."""
    if power == "on":
        _timeline(location, ("on", _at(6, 0), _at(7, 0)), ("off", _at(7, 0), _at(7, 30)))
        _timeline(location, ("on", _at(7, 30), None))
        _set_state(location, status="on", on_since=_at(7, 30), last_heartbeat_at=_at(8, 0))
    elif power == "off":
        _timeline(location, ("on", _at(6, 0), _at(7, 58)), ("off", _at(7, 58), None))
        _set_state(
            location,
            status="off",
            on_since=_at(6, 0),
            outage_started_at=_at(7, 58),
            last_heartbeat_at=_at(7, 58),
        )
    else:
        assert power == "waiting"


def _fail(location: Any, status: int, migrate_to: int | None = None) -> None:
    """Open the location's delivery-failing incident at 07:58 (UTC)."""
    with transaction.atomic():
        delivery.open_failing(location.pk, _at(7, 58), status, migrate_to)


def _order(page: Any, *elements: Tag) -> list[int]:
    """The document positions of ``elements``."""
    every = list(page.find_all(True))
    return [every.index(element) for element in elements]


# (power, delivery failing (status, migrate_to) or None, maintenance, delivery cause or None,
# maintenance help line or None)
BANNER_CASES: dict[str, tuple[str, tuple[int, int | None] | None, bool, str | None, str | None]] = {
    "http_400": ("on", (400, None), False, DELIVERY_NOT_IN_CHAT_CAUSE, None),
    "http_401": ("on", (401, None), False, DELIVERY_BOT_REJECTED_CAUSE, None),
    "http_403": ("on", (403, None), False, DELIVERY_CANNOT_POST_CAUSE, None),
    "http_404": ("on", (404, None), False, DELIVERY_BOT_REJECTED_CAUSE, None),
    "http_418": ("on", (418, None), False, DELIVERY_OTHER_CAUSE, None),
    # The supergroup line replaces the cause line (D-10).
    "migrate": ("on", (400, MIGRATED_CHAT_ID), False, MIGRATE_LINE, None),
    "maintenance-power-on": ("on", None, True, None, HELP_POWER_ON),
    "maintenance-waiting": ("waiting", None, True, None, HELP_POWER_ON),
    "maintenance-power-off": ("off", None, True, None, HELP_POWER_OFF),
    # Both banners at once: delivery failing first, then maintenance.
    "both": ("off", (403, None), True, DELIVERY_CANNOT_POST_CAUSE, HELP_POWER_OFF),
    "none": ("on", None, False, None, None),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(BANNER_CASES))
def test_UI01_banners(
    admin: Client, kyiv: Any, clock: FakeClock, location_factory: Callable[..., Any], case: str
) -> None:
    power, failing, paused, cause, help_line = BANNER_CASES[case]
    location = location_factory(name="Office", bot_token=TOKEN, maintenance=paused)
    _power(location, power)
    if failing is not None:
        _fail(location, *failing)

    soup = assert_page(admin.get(_page(location)), title="Office", app=True)

    shown = all_by_testid(soup, "delivery-banner") + all_by_testid(soup, "maintenance-banner")
    # Banners sit after the header and before the section nav, in this order.
    header, nav = by_testid(soup, "location-header"), by_testid(soup, "section-nav")
    assert _order(soup, header, *shown, nav) == sorted(_order(soup, header, *shown, nav))
    delivery_banners = all_by_testid(soup, "delivery-banner")
    if cause is None:
        assert delivery_banners == []
    else:
        assert failing is not None
        [banner] = delivery_banners
        code = f"http_{failing[0]}"
        assert (banner["data-tone"], banner["data-delivery"]) == ("warning", "failing")
        assert not banner.has_attr("data-live")
        assert text(by_testid(banner, "delivery-cause")) == cause
        assert text(by_testid(banner, "delivery-retry")) == DELIVERY_RETRY_LINE
        assert text(banner) == (
            f"Warning: Delivery failing Failing since 2026-10-01 10:58:00 EEST ({code}) "
            f"· 2 min ago {cause} {DELIVERY_RETRY_LINE} Send test message"
        )
        # The since line: the full time as <time datetime> with its relative time.
        stamp = banner.find("time")
        assert isinstance(stamp, Tag)
        assert datetime.fromisoformat(str(stamp["datetime"])) == _at(7, 58)
        relatives = [found["data-relative"] for found in banner.select("[data-relative]")]
        assert relatives == [stamp["datetime"]]
        # The fix: a POST form with the CSRF token to the test message, a primary button.
        form = by_testid(banner, "banner-test-message-form")
        assert form is post_form(banner, reverse("location-test-message", args=[location.pk]))
        assert form.find("input", attrs={"name": "csrfmiddlewaretoken"}) is not None
        buttons = [found for found in form.find_all("button") if isinstance(found, Tag)]
        assert [
            (b.get("type"), b.get("data-variant"), b.get("data-pending-label"), text(b))
            for b in buttons
        ] == [("submit", "primary", "Sending… up to 15 s", "Send test message")]
    maintenance_banners = all_by_testid(soup, "maintenance-banner")
    if help_line is None:
        assert maintenance_banners == []
    else:
        [banner] = maintenance_banners
        assert banner["data-tone"] == "muted"
        assert text(banner) == f"Maintenance is on {help_line}"
    assert len(shown) == (cause is not None) + (help_line is not None)


@pytest.mark.django_db
def test_UI01_status_card(
    admin: Client, kyiv: Any, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    _power(location, "on")

    page = _get(admin, location)

    assert _status_rows(page) == [
        ("Status", "On"),
        ("On since", "2026-10-01 10:30:00 EEST (30 min ago)"),
        ("Last heartbeat", "2026-10-01 11:00:00 EEST (12 s ago)"),
        DELIVERY_OK_ROW,
    ]
    # Live elements: the pills and the times, each with this location's id; never the
    # delivery row, whose full-format text the poll's list-format text must not replace.
    live = main(page).select("[data-live]")
    assert {str(found["data-location-id"]) for found in live} == {str(location.pk)}
    assert sorted({str(found["data-live"]) for found in live}) == [
        "last-heartbeat",
        "since",
        "status",
    ]
    panel = by_testid(page, "status-panel")
    assert [found["data-live"] for found in panel.select("[data-live]")] == [
        "status",
        "since",
        "last-heartbeat",
    ]
    since = panel.select_one('[data-live="since"]')
    assert since is not None
    assert text(since.select_one("[data-since-label]") or since) == "On since"
    assert not _delivery_value(page).has_attr("data-live")
    # Every instant on the card is a <time datetime> with one [data-relative] of its value.
    for stamp in panel.find_all("time"):
        assert len(panel.select(f'[data-relative="{stamp["datetime"]}"]')) == 1

    # Delivery failing: the pill (all Inter) with the full start, then its relative time.
    _fail(location, 403)
    failing = _get(admin, location)
    value = _delivery_value(failing)
    label = value.select_one("[data-label]")
    assert label is not None
    assert text(label) == "Failing since 2026-10-01 10:58:00 EEST (http_403)"
    stamp = label.find("time")
    assert isinstance(stamp, Tag)
    assert datetime.fromisoformat(str(stamp["datetime"])) == _at(7, 58)
    relative = value.select("[data-relative]")
    assert [(found["data-relative"], text(found)) for found in relative] == [
        (stamp["datetime"], "2 min ago")
    ]
    assert relative[0] not in label.descendants
    assert not value.has_attr("data-live")
    # The cause, migrate and retry lines live in the banner only.
    assert DELIVERY_RETRY_LINE not in text(by_testid(failing, "status-panel"))

    # Maintenance: the Power state row (pill only) and no live since row, because the poll
    # does not read the power state.
    Location.objects.filter(pk=location.pk).update(maintenance=True)
    paused = _get(admin, location)
    assert [term for term, _value in _status_rows(paused)] == [
        "Status",
        "Power state",
        "On since",
        "Last heartbeat",
        "Delivery",
    ]
    power = by_testid(paused, "status-panel").select("[data-power]")
    assert [(found["data-power"], text(found)) for found in power] == [("on", "On")]
    assert not power[0].select("[data-live]")
    assert not main(paused).select('[data-live="since"]')
    assert not main(paused).select("[data-since-label]")
    assert sorted({str(found["data-live"]) for found in main(paused).select("[data-live]")}) == [
        "last-heartbeat",
        "status",
    ]


@pytest.mark.django_db
def test_UI05_detail_poll_hooks(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    _power(location, "on")

    page = _get(admin, location)

    shell = main(page)
    assert shell["x-data"] == "poll"
    assert shell["data-poll-url"] == reverse("location-status-json")
    assert shell["data-poll-page"] == "detail"
    assert shell["data-reload-url"] == reverse("location-detail", args=[location.pk])
    # The LIVE indicator in the top bar and the chip slot in the meta line, JS only.
    indicator = by_testid(by_testid(page, "topbar"), "live-status")
    assert indicator.has_attr("data-js-only")
    assert indicator.has_attr("hidden")
    chip = by_testid(by_testid(page, "location-meta"), "live-chip")
    assert chip.has_attr("data-js-only")
    assert chip.has_attr("hidden")
    # The reload chips ("Live updates paused · Reload page", "Status changed · Reload page")
    # link to this page.
    assert [link["href"] for link in chip.find_all("a")] == [_page(location), _page(location)]
    assert "Status changed · Reload page" in text(chip)


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["available", "in-progress", "no-history"])
def test_UI07_kebab_and_dialog(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any], state: str
) -> None:
    location = location_factory(name="Office")
    _power(location, {"available": "on", "in-progress": "off", "no-history": "waiting"}[state])
    base = _page(location)

    soup = assert_page(admin.get(base), title="Office", app=True)

    actions = by_testid(soup, "page-actions")
    edit = by_testid(actions, "header-edit-location")
    assert (edit["href"], text(edit)) == (f"{base}edit/", "Edit location")
    [kebab] = actions.select('button[popovertarget="location-menu"]')
    assert (kebab.get("type"), text(kebab)) == ("button", "More actions")
    menu = actions.find(id="location-menu")
    assert isinstance(menu, Tag)
    assert menu.has_attr("popover")
    assert [link["data-testid"] for link in menu.find_all("a")] == [
        "menu-device-setup",
        "menu-reset-history",
        "menu-delete-location",
    ]
    setup = by_testid(menu, "menu-device-setup")
    assert (setup["href"], text(setup)) == (f"{base}setup/", "Device setup")
    delete = by_testid(menu, "menu-delete-location")
    assert (delete["href"], text(delete)) == (f"{base}delete/", "Delete location…")
    assert delete.has_attr("data-confirm")
    reset = by_testid(menu, "menu-reset-history")
    assert text(reset) == "Reset history…"
    if state == "available":
        assert reset["href"] == f"{base}reset/"
        assert reset.has_attr("data-confirm")
        assert not all_by_testid(soup, "reset-unavailable")
    else:
        # No dead control: a plain link to the danger-zone row, described by its refusal.
        assert (reset["href"], reset["data-reason"]) == ("#reset-history", state)
        assert not reset.has_attr("data-confirm")
        assert reset["aria-describedby"] == "reset-unavailable"
        refusal = by_testid(soup, "reset-unavailable")
        assert soup.find(id="reset-unavailable") is refusal
        assert refusal["data-reason"] == state
    # One dialog shell, named by the fragment's title, in the layout's dialog block after main.
    dialogs = [found for found in soup.find_all("dialog") if isinstance(found, Tag)]
    assert [(d.get("data-testid"), d.get("aria-labelledby")) for d in dialogs] == [
        ("confirm-dialog", "confirm-title")
    ]
    assert dialogs[0].find_parent("main") is None
    assert _order(soup, main(soup), dialogs[0]) == sorted(_order(soup, main(soup), dialogs[0]))
    # One [data-confirm-scope], in main, starting confirmDialog.
    scopes = soup.select("[data-confirm-scope]")
    assert [scope.get("x-data") for scope in scopes] == ["confirmDialog"]
    assert scopes[0].find_parent("main") is main(soup)
    # Every confirmation entry is a same-origin link to its location's confirmation GET,
    # the kebab's entries outside the scope wrapper included (R7).
    entries = soup.select("[data-confirm]")
    assert entries
    for entry in entries:
        assert entry.name == "a"
        assert str(entry["href"]).startswith(base)
    assert by_testid(menu, "menu-delete-location") in entries
    assert not scopes[0].find(id="location-menu")


@pytest.mark.django_db
def test_UI08_device_setup_card(
    admin: Client, settings: Any, location_factory: Callable[..., Any]
) -> None:
    settings.PUBLIC_BASE_URL = "https://power.example.org"
    settings.ALLOWED_HOSTS = ["testserver", "other.example.org"]
    location = location_factory(name="Office")
    key = location.device_key

    # Another allowed Host: the URL still comes from PUBLIC_BASE_URL (R13).
    response = admin.get(_page(location), HTTP_HOST="other.example.org")

    soup = assert_page(response, title="Office", app=True)
    card = section(soup, "device-setup")
    assert DEVICE_SETUP_SENTENCE in text(card)
    url = card.find(id="heartbeat-url")
    assert isinstance(url, Tag)
    assert url.name == "code"
    assert code_block(card, "heartbeat-url") == examples.heartbeat_url("https://power.example.org")
    assert "other.example.org" not in response.content.decode()
    [copy] = card.select('button[data-testid="copy"]')
    assert (copy.get("type"), copy["data-copy-target"], text(copy)) == (
        "button",
        "heartbeat-url",
        "Copy heartbeat URL",
    )
    assert copy["data-copied-msg"] == "Heartbeat URL copied."
    assert copy.has_attr("data-js-only")
    assert copy.has_attr("hidden")
    # The one polite region the copy component announces in.
    assert len(soup.select('[data-copy-status][aria-live="polite"]')) == 1
    setup = by_testid(card, "open-setup")
    assert (setup.name, setup["href"]) == ("a", f"/locations/{location.pk}/setup/")
    # Never the device key, not even masked (R4).
    html = response.content.decode()
    assert key not in html
    assert keys.mask_key(key) not in html
    assert "Hidden key ending in" not in html
    assert not soup.select("#device-key")


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["available", "in-progress", "no-history"])
def test_danger_zone_states(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any], state: str
) -> None:
    location = location_factory(name="Office")
    _power(location, {"available": "on", "in-progress": "off", "no-history": "waiting"}[state])
    base = _page(location)

    page = _get(admin, location)

    danger = section(page, "danger-zone")
    assert text(danger.find("h2") or danger) == "Danger zone"
    rows = [section(page, "reset-history"), section(page, "delete-location")]
    for row in rows:
        assert row.find_parent("section") is danger
        assert row.get("tabindex") == "-1"
    reset_row, delete_row = rows
    links = all_by_testid(reset_row, "reset-history")
    refusals = all_by_testid(reset_row, "reset-unavailable")
    if state == "available":
        assert [(link.name, link["href"], text(link)) for link in links] == [
            ("a", f"{base}reset/", "Reset history…")
        ]
        assert links[0].has_attr("data-confirm")
        assert refusals == []
    else:
        line = RESET_REFUSED_IN_PROGRESS if state == "in-progress" else RESET_REFUSED_NO_HISTORY
        assert links == []
        assert [(found["data-reason"], text(found)) for found in refusals] == [(state, line)]
    delete = by_testid(delete_row, "delete-location")
    assert (delete.name, delete["href"], text(delete)) == (
        "a",
        f"{base}delete/",
        "Delete location…",
    )
    assert delete.has_attr("data-confirm")
    # Nothing here acts: no form and no button; destructive entries are links (R7).
    assert danger.find("form") is None
    assert danger.find("button") is None


# Every state of the page keeps the page invariants and holds no secret (TEST-STRATEGY
# §7.1 S5; UI-01, UI-12, R3, R4, INV-23 #2).

RENDER_STATES = [
    "outages-listed",
    "in-progress",
    "no-outage-in-14-days",
    "no-history",
    "failing-403",
    "failing-migrate",
    "maintenance-power-on",
    "maintenance-power-off",
]


@pytest.mark.django_db
@pytest.mark.parametrize("state", RENDER_STATES)
def test_UI12_location_page_render_matrix(
    admin: Client, kyiv: Any, clock: FakeClock, location_factory: Callable[..., Any], state: str
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    key = location.device_key
    if state in ("outages-listed", "failing-403", "failing-migrate", "maintenance-power-on"):
        _power(location, "on")
    elif state in ("in-progress", "maintenance-power-off"):
        _power(location, "off")
    elif state == "no-outage-in-14-days":
        month_ago = NOW.replace(day=1, month=9)
        _timeline(location, ("on", month_ago, None))
        _set_state(location, status="on", on_since=month_ago, last_heartbeat_at=_at(8, 0))
    if state == "failing-403":
        _fail(location, 403)
    elif state == "failing-migrate":
        _fail(location, 400, MIGRATED_CHAT_ID)
    if state.startswith("maintenance"):
        Location.objects.filter(pk=location.pk).update(maintenance=True)

    response = admin.get(_page(location))

    soup = assert_page(response, title="Office", app=True)
    assert_no_secrets(
        response.content.decode(),
        [TOKEN, SECRET, key, keys.mask_key(key)],
        label=state,
        headers=[str(response.get("Location", ""))],
    )
    # The settings list shows the token only as its mask.
    mask = by_testid(soup, "masked-token").find("code")
    assert isinstance(mask, Tag)
    assert mask.get_text() == MASKED_TOKEN
    # The seven cards in their pinned order, in every state.
    assert [found.get("id") for found in soup.find_all("section")] == [
        card_id for card_id, _ in CARDS
    ]
    # Each POST form on the page posts back to this location with the CSRF token.
    for form in soup.find_all("form"):
        if form.get("data-testid") in ("theme-form", "sign-out-form"):
            continue
        assert str(form["action"]).startswith(_page(location))
        assert hidden_value(form, "csrfmiddlewaretoken")
