"""The device setup page S8 (LOC-05, HB-01, D-06, D-11; 06-UI-SPEC Page Contracts › S8; UI-01,
UI-05, UI-08, UI-12; INV-23 UI part, INV-24 #3 link).

- The page extends the app layout in three states: masked (the GET), revealed by the Reveal
  POST and revealed by the Regenerate POST (Phase 4 D-14). Both POSTs answer 200 with the
  page itself, and every response of the view is ``Cache-Control: no-store``.
- Breadcrumbs Locations › {name} › Device setup; the h1 name with the live status pill; the
  meta "Last heartbeat {abs} ({rel})" or "Last heartbeat Never"; no header actions; five
  numbered step cards (before, url, key, examples, first-heartbeat) and the rail card
  "Location settings" with the settings list and "Edit location".
- The key is masked on GET and shown in full only in the two POST responses, only as the
  text of ``#device-key`` and of the four example code blocks (R4). Both key states offer
  "Regenerate key…", a link to the confirmation page (R7).
- The heartbeat URL comes from ``PUBLIC_BASE_URL``, never from the request's Host header
  (R13). Every example block is exactly the string the 01-09 generator returns, the same
  string ``test_examples_verbatim`` runs against ``/hb``.
- The bot token is shown only as its mask ``{bot_id}:••••••••`` (R3).

Pages are read through tests/web/pages.py and the 06-UI-SPEC test hooks only. Python-owned
copy is imported; template-owned copy is pinned against the 06-UI-SPEC copy rows setup.*
and shell.copy*. The relative times read ``timefmt.CLOCK`` (the view puts no clock in the
context), pinned by the ``clock`` fixture.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock
from django.contrib.auth import get_user_model
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
    text,
    title,
)
from secret_fixtures import MASKED as MASKED_TOKEN
from secret_fixtures import SECRET, TOKEN

from powermon.engine import rules
from powermon.engine.models import LocationState
from powermon.locations import keys
from powermon.locations.examples import (
    cron_lines,
    curl_cmd,
    heartbeat_url,
    wget_busybox,
    wget_gnu,
)
from powermon.locations.keys import generate_device_key
from powermon.locations.models import Location
from powermon.web import context_processors
from powermon.web.location_views import ALREADY_REGENERATED_MESSAGE, REGENERATED_MESSAGE
from powermon.web.status import STATUS_LABELS
from powermon.web.templatetags import timefmt

User = get_user_model()

BASE_URL = "https://power.example.org"
HB_URL = f"{BASE_URL}/hb"
EXAMPLE_BLOCKS = ("example-curl", "example-cron", "example-wget-gnu", "example-wget-busybox")
XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
ROUTER_GRACE_S = int(rules.ROUTER_GRACE.total_seconds())
# The pages' "now": 2026-10-25 00:30:12 UTC, 12 s after the heartbeat the tests store
# (00:30 UTC on the fall-back day is the first 03:30 in Kyiv, still summer time).
NOW = datetime(2026, 10, 25, 0, 30, 12, tzinfo=UTC)
BEAT = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
BEAT_TEXT = "2026-10-25 03:30:00 EEST"

# 06-UI-SPEC copy rows (template-owned copy).
STEPS = [
    ("before", "Before you start"),
    ("url", "Heartbeat URL"),
    ("key", "Device key"),
    ("examples", "Examples"),
    ("first-heartbeat", "Check it works"),
]
BEFORE_1 = (
    "Use a device that loses power in a blackout: plugged into mains, with no UPS, power "
    "bank or battery. A device on backup power keeps reporting and hides the outage."
)
BEFORE_2 = (
    "A heartbeat needs both power and internet at the device. If the router or the "
    "provider's network is down, this location is reported OFF too."
)
BEFORE_3 = (
    "The device must send a heartbeat at least every {period} s. "
    "Power is reported OFF after {off_after} s without one."
)
URL_INTRO = (
    "Send a GET or POST request to this URL. Put the key in the Authorization header; "
    "use ?key= in the URL only for devices that cannot set headers."
)
KEY_NOTE = "Anyone with this key can send heartbeats for this location. Keep it private."
HIDDEN_AGAIN = "The key is hidden again the next time you open this page."
REGENERATE_NOTE = "If the key has leaked, regenerate it. The old key stops working at once."
REVEAL_NOTE = "Reveal the key above to fill it into these examples."
H3_CAPTIONS = {
    "example-curl": "curl, key in a header (recommended)",
    "example-cron": "Cron (router or Linux)",
    "example-wget-gnu": "wget, key in the URL (GNU wget)",
    "example-wget-busybox": "BusyBox wget or OpenWrt uclient-fetch, key in the URL",
}
CRON_HELP = (
    "Each line is one crontab entry; add them with crontab -e. Together they send a "
    "heartbeat at least every {period} s. No curl on the device? Use one of the wget "
    "commands from these examples in place of the curl command."
)
BUSYBOX_HELP = "These do not accept --max-redirect. The heartbeat URL never redirects."
PREVIEWER_WARNING = (
    "Warning: Do not paste a URL that contains the key into Telegram or any other chat. "
    "Chat apps open links to build previews, and every opening counts as a heartbeat: it "
    "can mark this location ON while the power is off."
)
RESPONSES = (
    "A working heartbeat gets HTTP 200 with the body ok. HTTP 401 means the key is missing "
    "or wrong, and nothing is recorded. After the first heartbeat this location shows On; "
    "the first heartbeat sends no alert."
)
WAITING_LINE = "Waiting for the first heartbeat… This updates on its own when the device sends it."
RECEIVED_LINE = "First heartbeat received."
# Copy buttons (UI-08) in DOM order: target id -> (accessible name, polite message); copy
# rows shell.copy, shell.copy_sr and shell.copied_msg.
COPY_BUTTONS = {
    "heartbeat-url": ("Copy heartbeat URL", "Heartbeat URL copied."),
    "device-key": ("Copy device key", "Device key copied."),
    "example-curl": ("Copy curl example", "Example copied."),
    "example-cron": ("Copy cron lines", "Example copied."),
    "example-wget-gnu": ("Copy GNU wget example", "Example copied."),
    "example-wget-busybox": ("Copy BusyBox wget example", "Example copied."),
}
# The elements whose text may hold the key (R4).
KEY_TARGETS = ("device-key", *EXAMPLE_BLOCKS)
# The tabs (copy row setup.tabs) and the code block of each panel.
TABS = [
    ("curl (recommended)", "example-curl"),
    ("Cron", "example-cron"),
    ("GNU wget", "example-wget-gnu"),
    ("BusyBox wget", "example-wget-busybox"),
]


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


@pytest.fixture(autouse=True)
def site(settings: Any) -> Any:
    """A production-like public base URL and the display TZ, independent of the env file."""
    settings.PUBLIC_BASE_URL = BASE_URL
    settings.TIME_ZONE = "Europe/Kyiv"
    return settings


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """One fixed "now" for the page's relative times and the sidebar."""
    fake = FakeClock(NOW)
    monkeypatch.setattr(timefmt, "CLOCK", fake)
    monkeypatch.setattr(context_processors, "CLOCK", fake)
    return fake


@pytest.fixture
def location(location_factory: Callable[..., Any]) -> Any:
    return location_factory(name="Office", bot_token=TOKEN, period_s=60, grace_s=30)


def _url(location: Any) -> str:
    return f"/locations/{location.pk}/setup/"


def _regenerate(location: Any) -> str:
    return f"/locations/{location.pk}/setup/regenerate/"


def _masked(key: str) -> str:
    return "•" * 12 + key[-4:]


def _beat(location: Any) -> None:
    """The location is on since its first heartbeat at ``BEAT``."""
    LocationState.objects.filter(location=location).update(
        status="on", last_heartbeat_at=BEAT, on_since=BEAT
    )


def _header(page: Any) -> Tag:
    """The page header: the first header element inside main."""
    found = main(page).find("header")
    assert isinstance(found, Tag), "no page header in main"
    return found


def _meta_items(page: Any) -> list[str]:
    """The meta line's items as text, the JS-only live-chip slot left out."""
    meta = _header(page).find("ul")
    assert isinstance(meta, Tag), "no meta line in the page header"
    items = [li for li in meta.find_all("li", recursive=False) if isinstance(li, Tag)]
    return [text(li) for li in items if li.get("data-testid") != "live-chip"]


def _steps(page: Any) -> list[Tag]:
    """The step cards of ``ol[data-testid=setup-steps]``, in order."""
    steps = by_testid(page, "setup-steps")
    assert steps.name == "ol", f"setup-steps is a <{steps.name}>, not an <ol>"
    return all_by_testid(steps, "setup-step")


def _step(page: Any, name: str) -> Tag:
    """The step card whose ``data-step`` is ``name``."""
    found = [step for step in _steps(page) if step.get("data-step") == name]
    assert len(found) == 1, f"expected one step {name!r}, found {len(found)}"
    return found[0]


def _step_title(step: Tag) -> str:
    heading = step.find("h2")
    assert isinstance(heading, Tag), "a step card without its h2 title"
    return text(heading)


def _rail(page: Any) -> Tag:
    """The rail card: the section holding the settings list."""
    rail = by_testid(page, "settings-panel").find_parent("section")
    assert isinstance(rail, Tag), "the settings list is not in a section"
    return rail


def _assert_setup_trail(page: Any, location: Any, name: str = "Office") -> None:
    """S8's trail in the top bar and under the h1 (06-UI-SPEC S8), in every state: exactly
    Locations › {name} › Device setup, and only the last item is the current page."""
    expected = [
        ("Locations", "/"),
        (name, f"/locations/{location.pk}/"),
        ("Device setup", None),
    ]
    for testid in ("breadcrumbs", "breadcrumbs-compact"):
        assert breadcrumbs(page, testid) == expected, testid
        items = by_testid(page, testid).select("ol > li")
        current = [
            index
            for index, item in enumerate(items)
            if item.get("aria-current") == "page" or item.select('[aria-current="page"]')
        ]
        assert current == [len(items) - 1], testid


def _rail_title(page: Tag, rail: Tag) -> str:
    """The text of the element that names the rail card (its aria-labelledby)."""
    labelled = page.find(id=str(rail["aria-labelledby"]))
    assert isinstance(labelled, Tag), "the rail card is not named by an element"
    return text(labelled)


# The page in its three states (UI-01, UI-12)


@pytest.mark.django_db
def test_UI01_setup_masked_page(
    admin: Client, location_factory: Callable[..., Any], location: Any
) -> None:
    other = location_factory(name="Other")

    response = admin.get(_url(location))

    assert "no-store" in response["Cache-Control"]
    soup = assert_page(response, app=True, title="Office · Device setup")
    _assert_setup_trail(soup, location)
    # The sidebar marks this location as the current one, and only it.
    current = {
        str(link["data-location-id"]): link.get("aria-current")
        for link in all_by_testid(soup, "sidebar-location")
    }
    assert current == {str(location.pk): "page", str(other.pk): None}
    # The header: the h1 name and the live status pill; the meta line; no actions.
    header = _header(soup)
    assert header.find("h1") is h1(soup)
    assert text(h1(soup)) == "Office"
    pill = by_testid(header, "status-pill")
    assert (pill["data-status"], text(pill)) == ("waiting", STATUS_LABELS["waiting"])
    assert (pill["data-live"], pill["data-location-id"]) == ("status", str(location.pk))
    assert _meta_items(soup) == ["Last heartbeat Never"]
    assert by_testid(soup, "page-actions").find_all(True) == []
    assert text(by_testid(soup, "page-actions")) == ""
    # The five step cards in order, each titled by its micro label.
    steps = _steps(soup)
    assert [(step.get("data-step"), _step_title(step)) for step in steps] == STEPS
    for step in steps:
        assert step.name == "li"
    # The rail card: the settings list (token as its mask only) and "Edit location".
    rail = _rail(soup)
    assert _rail_title(soup, rail) == "Location settings"
    mask = by_testid(rail, "masked-token").find("code")
    assert isinstance(mask, Tag)
    assert (mask.get_text(), mask.get("aria-hidden")) == (MASKED_TOKEN, "true")
    edit = by_testid(rail, "edit-location")
    assert (edit.name, edit["href"], text(edit)) == (
        "a",
        f"/locations/{location.pk}/edit/",
        "Edit location",
    )
    # The key is masked.
    assert by_testid(soup, "device-key")["data-state"] == "masked"
    assert location.device_key not in response.content.decode()


@pytest.mark.django_db
def test_UI01_setup_revealed_pages(admin: Client, location: Any) -> None:
    key = location.device_key

    revealed = admin.post(_url(location))

    assert "no-store" in revealed["Cache-Control"]
    soup = assert_page(revealed, app=True, title="Office · Device setup")
    _assert_setup_trail(soup, location)
    assert text(h1(soup)) == "Office"
    assert [(step.get("data-step"), _step_title(step)) for step in _steps(soup)] == STEPS
    assert by_testid(soup, "device-key")["data-state"] == "revealed"
    assert code_block(soup, "device-key") == key
    assert messages(soup) == []

    # The Regenerate POST answers with the same page, revealed with the new key.
    confirm = admin.get(_regenerate(location))
    marker = hidden_value(post_form(confirm, _regenerate(location)), "marker")
    regenerated = admin.post(_regenerate(location), {"marker": marker})
    resubmitted = admin.post(_regenerate(location), {"marker": marker})

    new_key = Location.objects.get(pk=location.pk).device_key
    assert new_key != key
    for response, level, flash in (
        (regenerated, "success", REGENERATED_MESSAGE),
        (resubmitted, "info", ALREADY_REGENERATED_MESSAGE),
    ):
        assert "no-store" in response["Cache-Control"]
        page = assert_page(response, app=True, title="Office · Device setup")
        assert text(h1(page)) == "Office"
        # The response is S8, so its trail is S8's whole trail, Device setup current, never
        # the S9 confirmation's "Regenerate key" (W6-A2).
        _assert_setup_trail(page, location)
        assert [(step.get("data-step"), _step_title(step)) for step in _steps(page)] == STEPS
        assert by_testid(page, "device-key")["data-state"] == "revealed"
        assert code_block(page, "device-key") == new_key
        assert [(message.level, message.text) for message in messages(page)] == [(level, flash)]
    # The success flash is sticky (instructive: the device needs the new key); the info is not.
    assert by_testid(parse(regenerated), "toast").has_attr("data-sticky")
    assert not by_testid(parse(resubmitted), "toast").has_attr("data-sticky")


# Masked by default, revealed only by POST


@pytest.mark.django_db
def test_LOC05_setup_masked_by_default(admin: Client, location: Any) -> None:
    key = location.device_key

    response = admin.get(_url(location))

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert key not in response.content.decode()
    page = parse(response)
    masked = _masked(key)
    assert len(masked) == 16
    assert keys.mask_key(key) == masked
    field = by_testid(page, "device-key")
    assert field["data-state"] == "masked"
    value = field.find(id="device-key")
    assert isinstance(value, Tag)
    assert (value.name, value.get_text(), value.get("aria-hidden")) == ("code", masked, "true")
    # A screen reader hears the tail only, never the mask glyphs.
    assert f"Hidden key ending in {key[-4:]}" in text(field)
    assert "•" not in text(field)
    for block in EXAMPLE_BLOCKS:
        assert masked in code_block(page, block)
    assert text(by_testid(page, "examples-masked-note")) == REVEAL_NOTE
    assert code_block(page, "heartbeat-url") == HB_URL
    # The Reveal form: a CSRF POST to this page, its token filled in, "Reveal key" primary.
    reveal = post_form(page, _url(location))
    assert reveal is by_testid(page, "reveal-form")
    assert hidden_value(reveal, "csrfmiddlewaretoken") != ""
    buttons = [found for found in reveal.find_all("button") if isinstance(found, Tag)]
    assert [(b.get("type"), b.get("data-variant"), text(b)) for b in buttons] == [
        ("submit", "primary", "Reveal key")
    ]
    assert all_by_testid(page, "hide-key") == []
    key_step = text(_step(page, "key"))
    assert HIDDEN_AGAIN not in key_step
    assert KEY_NOTE in key_step


@pytest.mark.django_db
def test_LOC05_reveal_shows_full_key_only_in_post_response(admin: Client, location: Any) -> None:
    key = location.device_key

    response = admin.post(_url(location))

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    page = parse(response)
    assert code_block(page, "device-key") == key
    for block in EXAMPLE_BLOCKS:
        example = code_block(page, block)
        assert key in example
        assert "•" not in example
    hide = by_testid(page, "hide-key")
    assert (hide.name, hide["href"], text(hide)) == ("a", _url(location), "Hide key")
    key_step = text(_step(page, "key"))
    assert HIDDEN_AGAIN in key_step
    assert KEY_NOTE in key_step
    assert all_by_testid(page, "examples-masked-note") == []
    assert all_by_testid(page, "reveal-form") == []
    assert "Reveal key" not in text(main(page))
    # No primary button in the revealed state.
    assert main(page).select('[data-variant="primary"]') == []

    again = admin.get(_url(location))

    assert key not in again.content.decode()
    assert code_block(again, "device-key") == _masked(key)


@pytest.mark.django_db
def test_full_key_appears_in_no_other_response(admin: Client) -> None:
    created = admin.post(
        "/locations/new/",
        {
            "name": "Home",
            "period_s": "60",
            "grace_s": "30",
            "bot_token": TOKEN,
            "chat_id": "-1001234567890",
            "language": "en",
            "chart_refresh_min": "15",
        },
    )
    location = LocationState.objects.get().location
    key = location.device_key
    setup = created.url
    regenerate = f"{setup}regenerate/"

    confirm = admin.get(regenerate)
    responses = [
        created,
        admin.get(setup),
        admin.get("/"),
        admin.get("/locations/new/"),
        admin.get("/locations/999999/setup/"),
        admin.get(f"/locations/{location.pk}/"),
        confirm,
        admin.get("/locations/999999/setup/regenerate/"),
    ]

    for response in responses:
        assert key not in response.content.decode()
        assert key not in response.get("Location", "")
    assert key in admin.post(setup).content.decode()

    # The only other response with the full key is the Regenerate POST, with the new one
    # (SEC-04, D-14); the old key is gone from it, and the new key from every page after.
    marker = hidden_value(post_form(confirm, regenerate), "marker")
    assert marker
    regenerated = admin.post(regenerate, {"marker": marker})
    new_key = Location.objects.get(pk=location.pk).device_key
    assert new_key != key
    assert new_key in regenerated.content.decode()
    assert key not in regenerated.content.decode()
    for response in (
        admin.get(setup),
        admin.get("/"),
        admin.get(f"/locations/{location.pk}/"),
        admin.get(regenerate),
    ):
        assert new_key not in response.content.decode()


@pytest.mark.django_db
def test_setup_page_offers_regenerate(admin: Client, location: Any) -> None:
    masked = parse(admin.get(_url(location)))
    revealed = parse(admin.post(_url(location)))

    for page in (masked, revealed):
        step = _step(page, "key")
        link = by_testid(step, "regenerate-key")
        assert (link.name, link["href"], text(link), link["data-variant"]) == (
            "a",
            _regenerate(location),
            "Regenerate key…",
            "outline-danger",
        )
        # The entry point only opens the server confirmation (R7): a link, not a form.
        assert link.has_attr("data-confirm")
        assert [form for form in page.find_all("form") if form.get("action") == link["href"]] == []
        assert page.select('[data-variant="danger"]') == []
        # The private-key note, then the regenerate intro, then the link (S8 step 3).
        words = text(step)
        assert words.index(KEY_NOTE) < words.index(REGENERATE_NOTE) < words.index(text(link))


# The examples are the generator's strings


@pytest.mark.django_db
@pytest.mark.parametrize("period", [10, 60, 120, 3600])
def test_setup_examples_match_the_generators(
    admin: Client, location_factory: Callable[..., Any], period: int
) -> None:
    location = location_factory(period_s=period)
    key = location.device_key
    url = heartbeat_url(BASE_URL)

    revealed = parse(admin.post(_url(location)))
    masked = parse(admin.get(_url(location)))

    for page, shown in ((revealed, key), (masked, _masked(key))):
        assert code_block(page, "heartbeat-url") == url
        assert code_block(page, "example-curl") == curl_cmd(url, shown, multiline=True)
        assert code_block(page, "example-cron") == "\n".join(cron_lines(url, shown, period))
        assert code_block(page, "example-wget-gnu") == wget_gnu(url, shown)
        assert code_block(page, "example-wget-busybox") == wget_busybox(url, shown)


@pytest.mark.django_db
def test_setup_cron_follows_the_period(admin: Client, location_factory: Callable[..., Any]) -> None:
    fast = location_factory(name="fast", period_s=10)
    slow = location_factory(name="slow", period_s=120)
    hourly = location_factory(name="hourly", period_s=3600)

    fast_lines = code_block(admin.get(_url(fast)), "example-cron").split("\n")
    slow_lines = code_block(admin.get(_url(slow)), "example-cron").split("\n")
    hourly_lines = code_block(admin.get(_url(hourly)), "example-cron").split("\n")

    assert len(fast_lines) == 6
    assert fast_lines[0].startswith("* * * * * curl ")
    for k, line in enumerate(fast_lines[1:], start=1):
        assert line.startswith(f"* * * * * sleep {10 * k}; curl ")
    assert len(slow_lines) == 1
    assert slow_lines[0].startswith("*/2 * * * * curl ")
    assert hourly_lines[0].startswith("0 * * * * curl ")


@pytest.mark.django_db
def test_setup_url_never_from_host_header(admin: Client, location: Any, settings: Any) -> None:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "evil.example"]

    for response in (
        admin.get(_url(location), HTTP_HOST="evil.example"),
        admin.post(_url(location), HTTP_HOST="evil.example"),
    ):
        assert response.status_code == 200
        assert "evil.example" not in response.content.decode()
        page = parse(response)
        assert code_block(page, "heartbeat-url") == HB_URL
        for block in EXAMPLE_BLOCKS:
            assert HB_URL in code_block(page, block)


# The bot token


@pytest.mark.django_db
def test_token_shown_only_as_bot_id(admin: Client, location: Any) -> None:
    for response in (admin.get(_url(location)), admin.post(_url(location))):
        html = response.content.decode()
        assert SECRET not in html
        assert TOKEN not in html
        page = parse(response)
        assert ("Bot token", "987654321, the rest is hidden") in definitions(page, "settings-panel")
        mask = by_testid(page, "masked-token").find("code")
        assert isinstance(mask, Tag)
        assert (mask.get_text(), mask.get("aria-hidden")) == (MASKED_TOKEN, "true")


# Failure paths: unknown, deleted, CSRF, signed out


@pytest.mark.django_db
def test_setup_unknown_or_deleted_location_404(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    deleted = location_factory(deleted_at=datetime(2026, 9, 1, tzinfo=UTC))

    for response in (
        admin.get("/locations/999999/setup/"),
        admin.post("/locations/999999/setup/"),
        admin.get(_url(deleted)),
        admin.post(_url(deleted)),
    ):
        assert response.status_code == 404
        assert text(h1(parse(response))) == "Page not found"
        assert deleted.device_key not in response.content.decode()


@pytest.mark.django_db
def test_reveal_requires_csrf(location: Any) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    refused = client.post(_url(location))

    assert refused.status_code == 403
    assert text(h1(parse(refused))) == "Form expired"
    assert location.device_key not in refused.content.decode()

    # The page's own Reveal form carries a working token (its partial is included with
    # only, so the caller passes the token on): posting it back reveals the key.
    form = by_testid(client.get(_url(location)), "reveal-form")
    token = hidden_value(form, "csrfmiddlewaretoken")
    assert token != ""
    accepted = client.post(_url(location), {"csrfmiddlewaretoken": token})
    assert accepted.status_code == 200
    assert code_block(accepted, "device-key") == location.device_key


@pytest.mark.django_db
def test_anonymous_setup_redirects_to_sign_in(client: Client, location: Any) -> None:
    for response in (client.get(_url(location)), client.post(_url(location))):
        assert response.status_code == 302
        assert response.url == f"/login/?next={_url(location)}"
        assert location.device_key not in response.content.decode()


# Guidance copy and the settings panel


@pytest.mark.django_db
def test_setup_guidance_copy(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(
        name="Dacha", period_s=45, grace_s=20, language="ru", chat_id=-1009876543210
    )

    page = parse(admin.get(_url(location)))

    assert title(page) == "Dacha · Device setup · Power Monitor"
    assert [_step_title(step) for step in _steps(page)] == [name for _, name in STEPS]
    before = text(_step(page, "before"))
    for paragraph in (BEFORE_1, BEFORE_2, BEFORE_3.format(period=45, off_after=65)):
        assert paragraph in before
    assert URL_INTRO in text(_step(page, "url"))
    examples = _step(page, "examples")
    assert [text(h3) for h3 in examples.find_all("h3")] == list(H3_CAPTIONS.values())
    assert CRON_HELP.format(period=45) in text(examples)
    assert BUSYBOX_HELP in text(examples)
    # The literal fragments are code.
    assert [code.get_text() for code in examples.find_all("code") if not code.get("id")] == [
        "crontab -e",
        "--max-redirect",
    ]
    assert text(by_testid(examples, "previewer-warning")) == PREVIEWER_WARNING
    assert RESPONSES in text(_step(page, "first-heartbeat"))
    assert definitions(page, "settings-panel") == [
        ("Language", "Russian"),
        ("Chart update period", "15 min"),
        ("Heartbeat period", "45 s"),
        ("Grace period", "20 s"),
        ("Reported OFF after", "65 s without a heartbeat"),
        ("Channel chat ID", "-1009876543210"),
        ("Bot token", "123456789, the rest is hidden"),
    ]
    # "Edit location" comes after the settings list in the rail card.
    rail = _rail(page)
    elements = list(rail.find_all(True))
    edit = by_testid(rail, "edit-location")
    assert elements.index(by_testid(rail, "settings-panel")) < elements.index(edit)
    assert "Settings cannot be changed yet" not in text(page)


@pytest.mark.django_db
def test_setup_meta_line_shows_status_and_last_heartbeat(admin: Client, location: Any) -> None:
    waiting = parse(admin.get(_url(location)))
    _beat(location)
    on = parse(admin.get(_url(location)))

    pill = by_testid(_header(waiting), "status-pill")
    assert (pill["data-status"], text(pill)) == ("waiting", STATUS_LABELS["waiting"])
    assert _meta_items(waiting) == ["Last heartbeat Never"]
    assert _header(waiting).find("time") is None
    pill = by_testid(_header(on), "status-pill")
    assert (pill["data-status"], text(pill)) == ("on", STATUS_LABELS["on"])
    assert _meta_items(on) == [f"Last heartbeat {BEAT_TEXT} (12 s ago)"]
    # The time is a <time datetime> of the stored instant beside one [data-relative], live.
    [stamp] = _header(on).find_all("time")
    assert datetime.fromisoformat(str(stamp["datetime"])) == BEAT
    [relative] = _header(on).select("[data-relative]")
    assert relative["data-relative"] == stamp["datetime"]
    live = stamp.find_parent(attrs={"data-live": "last-heartbeat"})
    assert isinstance(live, Tag)
    assert live["data-location-id"] == str(location.pk)


@pytest.mark.django_db
def test_setup_meta_line_shows_maintenance(admin: Client, location: Any) -> None:
    # The Phase 4 vocabulary (D-13): "Maintenance" whenever the flag is on, whatever the
    # stored status underneath.
    _beat(location)
    Location.objects.filter(pk=location.pk).update(maintenance=True)

    page = parse(admin.get(_url(location)))

    pill = by_testid(_header(page), "status-pill")
    assert (pill["data-status"], text(pill)) == ("maintenance", STATUS_LABELS["maintenance"])
    assert _meta_items(page) == [f"Last heartbeat {BEAT_TEXT} (12 s ago)"]


# The setup page's halves of tests on the list and location pages (06-09): the other half
# of each keeps its name in tests/web/test_templates.py (the first two) or
# tests/web/test_location_page.py (the last three).


@pytest.mark.django_db
def test_xss_name_is_escaped_everywhere_on_setup(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name=XSS_NAME)

    response = admin.get(_url(location))
    html = response.content.decode()
    page = parse(response)

    # The parser decodes the entities, so each place reads as the typed name.
    assert text(h1(page)) == XSS_NAME
    assert title(page) == f"{XSS_NAME} · Device setup · Power Monitor"
    assert breadcrumbs(page)[1] == (XSS_NAME, f"/locations/{location.pk}/")
    assert ESCAPED_XSS_NAME in html
    assert_no_injected_script(html, "setup page")


@pytest.mark.django_db
def test_long_name_shows_in_full_on_setup(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "x" * 100
    location = location_factory(name=name)

    page = parse(admin.get(_url(location)))

    # The 100-character name in full (E10 long-text): the h1, the title and both trails.
    assert text(h1(page)) == name
    assert title(page) == f"{name} · Device setup · Power Monitor"
    assert breadcrumbs(page)[1] == (name, f"/locations/{location.pk}/")
    assert breadcrumbs(page, "breadcrumbs-compact")[1] == (name, f"/locations/{location.pk}/")


@pytest.mark.django_db
def test_location_page_settings_and_setup_sections_on_setup(
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

    page = parse(admin.get(_url(location)))

    # The location page's settings card shows these same rows (test_location_page.py).
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
    assert definitions(_rail(page), "settings-panel") == expected
    mask = by_testid(page, "masked-token").find("code")
    assert isinstance(mask, Tag)
    assert (mask.get_text(), mask.get("aria-hidden")) == (MASKED_TOKEN, "true")


@pytest.mark.django_db
def test_location_page_escapes_the_name_on_setup(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name=XSS_NAME)
    detail = f"/locations/{location.pk}/"

    response = admin.get(_url(location))
    page = parse(response)

    # The name is text in the h1, the title, both trails and the sidebar row's title.
    assert text(h1(page)) == XSS_NAME
    assert title(page) == f"{XSS_NAME} · Device setup · Power Monitor"
    assert breadcrumbs(page)[1] == (XSS_NAME, detail)
    assert breadcrumbs(page, "breadcrumbs-compact")[1] == (XSS_NAME, detail)
    sidebar = [
        link
        for link in all_by_testid(page, "sidebar-location")
        if link["data-location-id"] == str(location.pk)
    ]
    assert [link["title"] for link in sidebar] == [XSS_NAME]
    # E3 loading / E10 loading: plain forms and static links, no injected or inline script.
    assert_no_injected_script(response.content.decode(), "setup page")


@pytest.mark.django_db
def test_breadcrumbs_on_location_and_setup_pages_on_setup(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    detail = f"/locations/{location.pk}/"

    page = parse(admin.get(f"{detail}setup/"))

    trail = [("Locations", "/"), ("Office", detail), ("Device setup", None)]
    assert breadcrumbs(page) == trail
    assert breadcrumbs(page, "breadcrumbs-compact") == trail
    # The 06-UI-SPEC order: the top-bar trail, then in main the h1, the compact trail
    # (below md) and the meta line.
    elements = list(page.find_all(True))
    meta = _header(page).find("ul")
    assert isinstance(meta, Tag)
    order = [
        elements.index(by_testid(page, "breadcrumbs")),
        elements.index(h1(page)),
        elements.index(by_testid(page, "breadcrumbs-compact")),
        elements.index(meta),
    ]
    assert order == sorted(order)
    assert by_testid(page, "breadcrumbs").find_parent("main") is None


# Copy (UI-08), the key's placement (R4, INV-23 #2), tabs, the reveal guard, the live
# first-heartbeat step (UI-05) and the regenerate entry (UI-07, R7)


def _first_text(element: Tag) -> str:
    """The first non-blank text of ``element``: the copy component's visible label."""
    for string in element.find_all(string=True):
        if str(string).strip():
            return str(string).strip()
    raise AssertionError("the element has no text")


def _assert_copy_button(page: Tag, button: Tag) -> str:
    """One copy button keeps the 06-11 binding; returns its target id."""
    target = str(button["data-copy-target"])
    assert len(page.find_all(id=target)) == 1, f"copy target {target!r} is not one element"
    name, copied = COPY_BUTTONS[target]
    assert (button.name, button.get("type"), text(button), button.get("data-copied-msg")) == (
        "button",
        "button",
        name,
        copied,
    )
    assert button.get("x-data") == "copy"
    assert button.has_attr("data-js-only")
    assert button.has_attr("hidden")
    # "Copy" is the first text, the sr-only suffix comes after it.
    assert _first_text(button) == "Copy"
    return target


@pytest.mark.django_db
def test_UI08_masked_copy_only_url(admin: Client, location: Any) -> None:
    key = location.device_key

    page = parse(admin.get(_url(location)))

    # Expected: exactly one copy button, for the heartbeat URL.
    buttons = all_by_testid(page, "copy")
    assert [_assert_copy_button(page, button) for button in buttons] == ["heartbeat-url"]
    assert code_block(page, "heartbeat-url") == heartbeat_url(BASE_URL)
    # The key field is masked: the mask hidden from screen readers, which hear the tail.
    field = by_testid(page, "device-key")
    assert field["data-state"] == "masked"
    value = field.find(id="device-key")
    assert isinstance(value, Tag)
    assert (value.get_text(), value.get("aria-hidden")) == (_masked(key), "true")
    assert f"Hidden key ending in {key[-4:]}" in text(field)
    # The Reveal form posts to this page with a filled CSRF token.
    reveal = by_testid(field, "reveal-form")
    assert reveal is post_form(page, _url(location))
    assert hidden_value(reveal, "csrfmiddlewaretoken") != ""
    # Edge: the examples hold the mask, the note says how to fill them, none is copyable.
    assert text(by_testid(page, "examples-masked-note")) == REVEAL_NOTE
    assert by_testid(page, "examples-masked-note")["data-tone"] == "info"
    for block in EXAMPLE_BLOCKS:
        assert _masked(key) in code_block(page, block)
    assert key not in str(page)


@pytest.mark.django_db
@pytest.mark.parametrize(("period", "lines"), [(60, 1), (30, 2), (10, 6)])
def test_UI08_revealed_copy_buttons(
    admin: Client, location_factory: Callable[..., Any], period: int, lines: int
) -> None:
    location = location_factory(name="Office", period_s=period)
    key = location.device_key
    url = heartbeat_url(BASE_URL)

    page = parse(admin.post(_url(location)))

    # Six copy buttons in DOM order, each aimed at one existing element, named uniquely.
    buttons = all_by_testid(page, "copy")
    assert [_assert_copy_button(page, button) for button in buttons] == list(COPY_BUTTONS)
    assert len({text(button) for button in buttons}) == len(COPY_BUTTONS)
    # Each target's text is its generator's output exactly: nothing added around it.
    expected = {
        "heartbeat-url": url,
        "device-key": key,
        "example-curl": curl_cmd(url, key, multiline=True),
        "example-cron": "\n".join(cron_lines(url, key, period)),
        "example-wget-gnu": wget_gnu(url, key),
        "example-wget-busybox": wget_busybox(url, key),
    }
    for target, value in expected.items():
        assert code_block(page, target) == value, target
    # One cron line per offset below a minute (examples.cron_lines), one line from a minute.
    assert len(code_block(page, "example-cron").split("\n")) == lines
    # The examples scroll inside their own block and keep their line breaks.
    for block in EXAMPLE_BLOCKS:
        code = page.find(id=block)
        assert isinstance(code, Tag)
        assert code.name == "code"
        assert code.parent is not None
        assert code.parent.name == "pre"


@pytest.mark.django_db
def test_UI08_key_only_in_its_elements(admin: Client, location_factory: Callable[..., Any]) -> None:
    # A tail with letters no hex hash or other page text holds.
    location = location_factory(
        name="Office", period_s=10, device_key=generate_device_key()[:28] + "QZXK"
    )
    key = location.device_key
    cron = len(cron_lines(heartbeat_url(BASE_URL), key, 10))
    assert cron == 6

    response = admin.post(_url(location))
    html = response.content.decode()

    # The key once in #device-key, once in curl, GNU and BusyBox wget, once per cron line.
    assert html.count(key) == 4 + cron
    # Removing the text of those five elements leaves no key: no attribute, no title, no
    # data-*, no Location header (R4).
    assert_no_secrets(
        html,
        [key],
        label="revealed setup page",
        allow=[(key, f"#{target}") for target in KEY_TARGETS],
        headers=[str(response.get("Location", ""))],
    )
    soup = parse(response)
    for element in soup.find_all(True):
        for attribute, value in element.attrs.items():
            assert key not in str(value), f"<{element.name} {attribute}> holds the key"
    assert key not in str(by_testid(soup, "sidebar"))
    for live in soup.select("[data-live]"):
        assert key not in str(live)

    # Masked: the mask only in #device-key and the examples, the tail only in the sr text.
    masked = admin.get(_url(location))
    page = parse(masked)
    mask = _masked(key)
    for token in page.find_all("input", attrs={"name": "csrfmiddlewaretoken"}):
        token["value"] = ""
    assert_no_secrets(
        str(page),
        [mask],
        label="masked setup page",
        allow=[(mask, f"#{target}") for target in KEY_TARGETS],
    )
    for target in KEY_TARGETS:
        element = page.find(id=target)
        assert isinstance(element, Tag)
        element.clear()
    rest = str(page)
    assert rest.count(key[-4:]) == 1
    assert f"Hidden key ending in {key[-4:]}" in text(page)
    assert key not in masked.content.decode()


@pytest.mark.django_db
def test_tabs_and_warning(admin: Client, location: Any) -> None:
    for response in (admin.get(_url(location)), admin.post(_url(location))):
        page = parse(response)
        examples = _step(page, "examples")
        tablist = by_testid(examples, "example-tabs")
        # The tablist is JS only: rendered hidden, revealed by the tabs component.
        assert (tablist.get("role"), tablist.get("aria-label")) == ("tablist", "Device examples")
        assert tablist.has_attr("data-js-only")
        assert tablist.has_attr("hidden")
        wrapper = tablist.find_parent(attrs={"x-data": "tabs"})
        assert isinstance(wrapper, Tag)
        assert wrapper.find_parent(attrs={"data-step": "examples"}) is examples
        tabs = [
            found for found in tablist.find_all(attrs={"role": "tab"}) if isinstance(found, Tag)
        ]
        assert [text(tab) for tab in tabs] == [label for label, _ in TABS]
        assert [tab.get("aria-selected") for tab in tabs] == ["true", "false", "false", "false"]
        assert [tab.get("tabindex") for tab in tabs] == ["0", "-1", "-1", "-1"]
        panels = [
            found
            for found in wrapper.find_all(attrs={"role": "tabpanel"})
            if isinstance(found, Tag)
        ]
        assert len(panels) == len(TABS)
        for tab, panel, (_, block) in zip(tabs, panels, TABS, strict=True):
            assert (tab.name, tab.get("type")) == ("button", "button")
            # aria-controls names the panel; every panel is in the server HTML, visible.
            assert page.find(id=str(tab["aria-controls"])) is panel
            assert not panel.has_attr("hidden")
            # The panel starts with its full h3 caption, which names it, then its code.
            heading = next(child for child in panel.children if isinstance(child, Tag))
            assert heading.name == "h3"
            assert text(heading) == H3_CAPTIONS[block]
            assert page.find(id=str(panel["aria-labelledby"])) is heading
            assert panel.find(id=block) is not None
        assert CRON_HELP.format(period=60) in text(panels[1])
        assert BUSYBOX_HELP in text(panels[3])
        # The previewer warning: always visible, after the panels, outside every tabpanel.
        warning = by_testid(examples, "previewer-warning")
        assert warning["data-tone"] == "warning"
        assert text(warning) == PREVIEWER_WARNING
        assert warning.find_parent(attrs={"role": "tabpanel"}) is None
        assert not warning.has_attr("hidden")
        elements = list(examples.find_all(True))
        assert elements.index(panels[-1]) < elements.index(warning)


@pytest.mark.django_db
def test_reveal_guard_hooks(admin: Client, location: Any) -> None:
    key = location.device_key
    setup = _url(location)

    revealed = parse(admin.post(setup))
    masked = parse(admin.get(setup))

    # The revealed key region starts the reveal guard with the masked URL, which holds no key.
    region = by_testid(revealed, "device-key")
    assert (region["data-state"], region.get("x-data"), region.get("data-masked-url")) == (
        "revealed",
        "revealGuard",
        setup,
    )
    assert key not in str(region["data-masked-url"])
    assert region.find(id="device-key") is not None
    assert len(revealed.select('[x-data="revealGuard"]')) == 1
    # "Hide key" is a plain GET link to the setup page, which answers masked.
    hide = by_testid(region, "hide-key")
    assert (hide.name, hide["href"], text(hide)) == ("a", setup, "Hide key")
    assert key not in admin.get(str(hide["href"])).content.decode()
    # Failure guard: the masked page has neither the guard nor the hide link.
    assert masked.select('[x-data="revealGuard"]') == []
    assert all_by_testid(masked, "hide-key") == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("power", "maintenance", "received"),
    [
        ("waiting", False, False),
        ("on", False, True),
        ("off", False, True),
        ("on", True, True),
        ("waiting", True, False),
    ],
)
def test_UI05_first_heartbeat_step(
    admin: Client, location: Any, power: str, maintenance: bool, received: bool
) -> None:
    if power != "waiting":
        LocationState.objects.filter(location=location).update(
            status=power,
            last_heartbeat_at=BEAT,
            on_since=BEAT if power == "on" else None,
            outage_started_at=BEAT if power == "off" else None,
        )
    Location.objects.filter(pk=location.pk).update(maintenance=maintenance)

    page = parse(admin.get(_url(location)))

    # Step 5 holds the live element: polite, with this location's id.
    step = by_testid(page, "first-heartbeat")
    assert step.find_parent(attrs={"data-step": "first-heartbeat"}) is _step(
        page, "first-heartbeat"
    )
    assert (step["data-live"], step["data-location-id"], step["aria-live"]) == (
        "first-heartbeat",
        str(location.pk),
        "polite",
    )
    [waiting] = step.select('[data-fh="waiting"]')
    [heard] = step.select('[data-fh="received"]')
    # The waiting line is JS only (it promises an update): hidden until the poll starts.
    assert waiting.has_attr("data-js-only")
    assert waiting.has_attr("hidden")
    assert text(waiting) == WAITING_LINE
    if received:
        assert not heard.has_attr("hidden")
        assert text(heard) == f"{RECEIVED_LINE} Last heartbeat {BEAT_TEXT} (12 s ago)"
        [stamp] = heard.find_all("time")
        assert datetime.fromisoformat(str(stamp["datetime"])) == BEAT
        [relative] = heard.select("[data-relative]")
        assert relative["data-relative"] == stamp["datetime"]
    else:
        # Edge: never heard from, so no time to show; the poll flips the variants.
        assert heard.has_attr("hidden")
        assert text(heard) == RECEIVED_LINE
        assert heard.find("time") is None
    # Step 5 never wraps the key region, and no live element holds the key field; every
    # live element of the page content is about this location.
    assert step.find(id="device-key") is None
    for live in page.select("[data-live]"):
        assert live.find(id="device-key") is None
    for live in main(page).select("[data-live]"):
        assert live.get("data-location-id") == str(location.pk)
    # main polls the status JSON as the setup page and reloads to the masked setup URL.
    shell = main(page)
    assert (shell.get("x-data"), shell.get("data-poll-page")) == ("poll", "setup")
    assert shell["data-poll-url"] == reverse("location-status-json")
    assert shell["data-reload-url"] == _url(location)
    indicator = by_testid(by_testid(page, "topbar"), "live-status")
    assert indicator.has_attr("data-js-only")
    assert indicator.has_attr("hidden")
    chip = by_testid(_header(page), "live-chip")
    assert [link["href"] for link in chip.find_all("a")] == [_url(location), _url(location)]


@pytest.mark.django_db
def test_W6A4_step5_announces_the_flip_not_the_time(admin: Client, location: Any) -> None:
    waiting = by_testid(parse(admin.get(_url(location))), "first-heartbeat")
    _beat(location)

    page = parse(admin.get(_url(location)))

    # Expected: the received line's time (absolute and relative), which every poll refills
    # and the relative component rewrites every 15 s, sits in one aria-live="off" element
    # inside the polite step 5 region, so only the waiting -> received flip is announced.
    step = by_testid(page, "first-heartbeat")
    assert step["aria-live"] == "polite"
    [heard] = step.select('[data-fh="received"]')
    quiet = step.select('[aria-live="off"]')
    assert len(quiet) == 1, "the polite step 5 region announces the ticking time"
    assert any(parent is heard for parent in quiet[0].parents)
    for timed in (*heard.find_all("time"), *heard.select("[data-relative]")):
        assert any(parent is quiet[0] for parent in timed.parents), timed
    # The flip itself stays announced: both lines sit outside the quiet element, and the
    # received sentence is not in it.
    for line in step.select("[data-fh]"):
        assert line.find_parent(attrs={"aria-live": "off"}) is None
    assert RECEIVED_LINE not in text(quiet[0])
    assert text(heard).startswith(RECEIVED_LINE)
    # Edge: rendered while waiting, the received line holds no time, so nothing is silenced.
    assert waiting.select('[aria-live="off"]') == []
    assert waiting.select('[data-fh="received"]')[0].find("time") is None


@pytest.mark.django_db
def test_UI07_setup_regenerate_entry(admin: Client, location: Any) -> None:
    regenerate = _regenerate(location)

    for response in (admin.get(_url(location)), admin.post(_url(location))):
        page = parse(response)
        link = by_testid(page, "regenerate-key")
        assert (link.name, link["href"], text(link)) == ("a", regenerate, "Regenerate key…")
        assert link.has_attr("data-confirm")
        # One dialog shell, named by the fragment's title, after main (the dialog block).
        dialogs = [found for found in page.find_all("dialog") if isinstance(found, Tag)]
        assert [(d.get("data-testid"), d.get("aria-labelledby")) for d in dialogs] == [
            ("confirm-dialog", "confirm-title")
        ]
        assert dialogs[0].find_parent("main") is None
        elements = list(page.find_all(True))
        assert elements.index(main(page)) < elements.index(dialogs[0])
        # One [data-confirm-scope] in main, starting confirmDialog, around the entry.
        scopes = page.select("[data-confirm-scope]")
        assert [scope.get("x-data") for scope in scopes] == ["confirmDialog"]
        assert scopes[0].find_parent("main") is main(page)
        assert link.find_parent(attrs={"data-confirm-scope": True}) is scopes[0]
        # Every confirmation entry is that same-origin link (R7).
        assert page.select("[data-confirm]") == [link]

    # The link's GET answers the shared confirmation partial as a fragment for the modal.
    fragment = admin.get(regenerate, HTTP_X_PM_FRAGMENT="1")
    assert fragment.status_code == 200
    assert fragment["X-PM-Fragment"] == "1"
    assert text(by_testid(parse(fragment), "confirm-title")) == "Regenerate the device key?"
