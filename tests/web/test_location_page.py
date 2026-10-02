"""The location page (LOC-03; D-13, SEC-04; UI-SPEC screen B, UI-D2, UI-D11, UI-D15).

- The status panel uses the one Phase 4 vocabulary: "Maintenance" whenever the flag is on,
  else the stored status. Under maintenance the stored status shows as the power state,
  with its help line. Rows that do not apply are omitted (E2).
- The settings panel is the setup page's, shared through one include, so both pages show
  identical values; with router grace on, "Reported OFF after" names the longer timeout
  right after power returns.
- The page shows no device key, not even masked, and the bot token only masked (SEC-04).
- Breadcrumbs sit before the flash messages and the h1 on the location and setup pages
  (UI-D2, E10). The admin-typed name is escaped everywhere and never truncated (E2).
- Every location URL answers 404 for an unknown or deleted location, and every page needs
  the signed-in admin.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

from powermon.engine import rules
from powermon.engine.models import LocationState
from powermon.locations import keys
from powermon.locations.models import Location

User = get_user_model()

SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
MASKED_TOKEN = "987654321:••••••••"
XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
ROUTER_GRACE_S = int(rules.ROUTER_GRACE.total_seconds())
HELP_POWER_ON = "OFF is not detected during maintenance."
HELP_POWER_OFF = "The outage goes on. When power returns, the ON alert is sent as usual."
DEVICE_SETUP_SENTENCE = "Heartbeat URL, device key and copy-paste examples for the device."
DELETE_SENTENCE = (
    "Stops this location's alerts, drops the alerts still queued, unpins its weekly chart "
    "where the bot still can, and hides it from the admin panel. There is no undo."
)
CSS_PATH = Path(settings.BASE_DIR) / "powermon" / "web" / "static" / "web" / "app.css"


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


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _page(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _set_state(location: Any, **fields: Any) -> None:
    LocationState.objects.filter(location=location).update(**fields)


def _set_maintenance(location: Any) -> None:
    Location.objects.filter(pk=location.pk).update(maintenance=True)


def _text(fragment: str) -> str:
    """The visible text of an HTML fragment: tags dropped, whitespace collapsed."""
    return unescape(" ".join(re.sub(r"<[^>]+>", " ", fragment).split()))


def _status_rows(page: str) -> list[tuple[str, str, str]]:
    """The status panel as (term, value, help line) triples; "" when a value has no help."""
    panel = re.search(r'<dl class="panel settings">(.*?)</dl>', page, re.S)
    assert panel is not None, "no status panel"
    rows = []
    for term, value in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", panel.group(1), re.S):
        help_line = re.search(r'<p class="help">(.*?)</p>', value, re.S)
        shown = re.sub(r'<p class="help">.*?</p>', "", value, flags=re.S)
        rows.append((_text(term), _text(shown), _text(help_line.group(1)) if help_line else ""))
    return rows


def _settings_rows(page: str) -> list[tuple[str, str]]:
    """The shared settings panel (the same parse as tests/web/test_setup_page.py)."""
    panel = re.search(r'<dl class="panel settings name">(.*?)</dl>', page, re.S)
    assert panel is not None, "no settings panel"
    pairs = re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", panel.group(1), re.S)
    return [(unescape(term), unescape(value)) for term, value in pairs]


def _crumbs(page: str) -> list[tuple[str, str]]:
    """The breadcrumb items as (li attributes, inner HTML)."""
    trail = re.search(
        r'<nav aria-label="Breadcrumb">\s*<ol class="crumbs">(.*?)</ol>\s*</nav>', page, re.S
    )
    assert trail is not None, "no breadcrumb trail"
    items = re.findall(r"<li\b([^>]*)>(.*?)</li>", trail.group(1), re.S)
    return [(attrs, inner.strip()) for attrs, inner in items]


# The status panel (LOC-03, E2)


@pytest.mark.django_db
def test_LOC03_location_page_status_panel(
    admin: Client, kyiv: Any, location_factory: Callable[..., Any]
) -> None:
    on = location_factory(name="On")
    _set_state(on, status="on", on_since=_at(7, 30), last_heartbeat_at=_at(8, 0))
    off = location_factory(name="Off")
    _set_state(off, status="off", outage_started_at=_at(7, 58), last_heartbeat_at=_at(7, 58))
    waiting = location_factory(name="Waiting")
    stateless = location_factory(name="No state row")
    LocationState.objects.filter(location=stateless).delete()

    on_since = ("On since", "2026-10-01 10:30:00 EEST", "")
    outage_since = ("Outage since", "2026-10-01 10:58:00 EEST", "")
    on_beat = ("Last heartbeat", "2026-10-01 11:00:00 EEST", "")
    off_beat = ("Last heartbeat", "2026-10-01 10:58:00 EEST", "")
    never = ("Last heartbeat", "Never", "")

    assert _status_rows(admin.get(_page(on)).content.decode()) == [
        ("Status", "On", ""),
        on_since,
        on_beat,
    ]
    assert _status_rows(admin.get(_page(off)).content.decode()) == [
        ("Status", "Off", ""),
        outage_since,
        off_beat,
    ]
    # Waiting: no On since / Outage since row (E2), and no state row counts as waiting.
    for place in (waiting, stateless):
        assert _status_rows(admin.get(_page(place)).content.decode()) == [
            ("Status", "Waiting for first heartbeat", ""),
            never,
        ]

    for place in (on, off, waiting):
        _set_maintenance(place)
    maintenance_on = admin.get(_page(on)).content.decode()

    assert _status_rows(maintenance_on) == [
        ("Status", "Maintenance", ""),
        ("Power state", "On", HELP_POWER_ON),
        on_since,
        on_beat,
    ]
    # UI-D11: the maintenance label gets its own (grey) dot; the power state keeps its own.
    assert '<span class="status status--maintenance">Maintenance</span>' in maintenance_on
    assert '<span class="status status--on">On</span>' in maintenance_on
    assert _status_rows(admin.get(_page(off)).content.decode()) == [
        ("Status", "Maintenance", ""),
        ("Power state", "Off", HELP_POWER_OFF),
        outage_since,
        off_beat,
    ]
    assert _status_rows(admin.get(_page(waiting)).content.decode()) == [
        ("Status", "Maintenance", ""),
        ("Power state", "Waiting for first heartbeat", ""),
        never,
    ]


# Sections: settings (shared with the setup page) and device setup


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

    page = admin.get(_page(location)).content.decode()
    setup = admin.get(f"/locations/{location.pk}/setup/").content.decode()

    expected = [
        ("Language", "Russian"),
        ("Heartbeat period", "45 s"),
        ("Grace period", "20 s"),
        (
            "Reported OFF after",
            f"65 s without a heartbeat ({65 + ROUTER_GRACE_S} s right after power returns, "
            "router grace on)",
        ),
        ("Channel chat ID", "-1009876543210"),
        ("Bot token", MASKED_TOKEN),
    ]
    assert ROUTER_GRACE_S == 180
    assert _settings_rows(page) == expected
    assert _settings_rows(setup) == expected
    assert re.findall(r"<h2>(.*?)</h2>", page) == [
        "Status",
        "Switches",
        "Settings",
        "Device setup",
        "Delete location",
    ]
    # UI-D10: "Edit location" is a secondary link-button under the settings panel.
    settings_section = page[page.index("<h2>Settings</h2>") : page.index("<h2>Device setup</h2>")]
    assert settings_section.index("</dl>") < settings_section.index("Edit location")
    assert (
        f'<p><a class="btn btn--secondary" href="/locations/{location.pk}/edit/">'
        "Edit location</a></p>"
    ) in settings_section
    device = page[page.index("<h2>Device setup</h2>") : page.index("<h2>Delete location</h2>")]
    assert f"<p>{DEVICE_SETUP_SENTENCE}</p>" in device
    assert (
        f'<a class="btn btn--secondary" href="/locations/{location.pk}/setup/">'
        "Open device setup</a>"
    ) in device

    Location.objects.filter(pk=location.pk).update(router_grace=False)

    plain = admin.get(_page(location)).content.decode()
    assert ("Reported OFF after", "65 s without a heartbeat") in _settings_rows(plain)


@pytest.mark.django_db
def test_location_page_delete_section(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")

    page = admin.get(_page(location)).content.decode()

    # UI-D5: the last section, a sentence and a secondary link-button that only opens the
    # confirmation page; the destructive button is on that page, not here.
    section = page[page.index("<h2>Delete location</h2>") :]
    section = section[: section.index("</main>")]
    assert f"<p>{DELETE_SENTENCE}</p>" in section
    assert (
        f'<p><a class="btn btn--secondary" href="/locations/{location.pk}/delete/">'
        "Delete location</a></p>"
    ) in section
    assert "<form" not in section
    assert "btn--danger" not in page


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

    page = admin.get(_page(location)).content.decode()
    setup = admin.get(f"/locations/{location.pk}/setup/").content.decode()

    assert f'<h1 class="name">{ESCAPED_XSS_NAME}</h1>' in page
    assert f"<title>{ESCAPED_XSS_NAME} · Power Monitor</title>" in page
    assert _crumbs(page)[-1] == (' class="name" aria-current="page"', ESCAPED_XSS_NAME)
    assert _crumbs(setup)[1] == (
        "",
        f'<a class="name" href="/locations/{location.pk}/">{ESCAPED_XSS_NAME}</a>',
    )
    # E3 loading / E10 loading: plain forms and static links, no script at all.
    for html in (page, setup):
        assert "<script" not in html


@pytest.mark.django_db
def test_long_name_is_shown_whole(admin: Client, location_factory: Callable[..., Any]) -> None:
    name = "x" * 100
    location = location_factory(name=name)

    page = admin.get(_page(location)).content.decode()

    assert f'<h1 class="name">{name}</h1>' in page
    assert f"<title>{name} · Power Monitor</title>" in page
    assert _crumbs(page)[-1] == (' class="name" aria-current="page"', name)


# Breadcrumbs (UI-D2, E10)


@pytest.mark.django_db
def test_breadcrumbs_on_location_and_setup_pages(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    detail = f"/locations/{location.pk}/"

    page = admin.post(f"{detail}maintenance/", {"value": "on"}, follow=True).content.decode()
    setup = admin.get(f"{detail}setup/").content.decode()

    assert _crumbs(page) == [
        ("", '<a href="/">Locations</a>'),
        (' class="name" aria-current="page"', "Office"),
    ]
    assert _crumbs(setup) == [
        ("", '<a href="/">Locations</a>'),
        ("", f'<a class="name" href="{detail}">Office</a>'),
        (' aria-current="page"', "Device setup"),
    ]
    # Order inside <main>: breadcrumbs, then the flash, then the h1.
    main = page[page.index("<main") :]
    assert main.index('<nav aria-label="Breadcrumb">') < main.index('<div class="messages')
    assert main.index('<div class="messages') < main.index("<h1")
    setup_main = setup[setup.index("<main") :]
    assert setup_main.index('<nav aria-label="Breadcrumb">') < setup_main.index("<h1")


# Accent and destructive buttons (UI-D15, UI-D5)


@pytest.mark.django_db
def test_location_page_has_one_accent_button_at_most(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory()

    page = admin.get(_page(location)).content.decode()

    # The switches, "Edit location", "Open device setup" and the "Delete location" entry are
    # all secondary: the page's one accent button is "Send test message" (04-08), and the
    # destructive style is used only on the delete confirmation page (UI-D5, UI-D15).
    assert "btn--primary" not in page
    assert "btn--danger" not in page


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


# Styles for narrow screens (E3 and E10 overflow)


def test_switch_rows_stack_and_crumbs_wrap_on_narrow_screens() -> None:
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(encoding="utf-8"), flags=re.S)
    narrow = re.search(r"@media \(width < 640px\) \{(.*)\}\s*$", css, re.S)
    assert narrow is not None
    switch = re.search(r"\.switch \{([^}]*)\}", narrow.group(1))
    assert switch is not None, "switch rows do not stack below 640px"
    assert "flex-direction: column" in switch.group(1)
    crumbs = re.search(r"\.crumbs \{([^}]*)\}", css)
    assert crumbs is not None
    assert "flex-wrap: wrap" in crumbs.group(1)
    assert "list-style: none" in crumbs.group(1)
