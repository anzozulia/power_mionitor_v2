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
from pages import (
    all_by_testid,
    assert_no_injected_script,
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
    detail = f"/locations/{location.pk}/"
    trail = [("Locations", "/"), ("Office", detail), ("Device setup", None)]
    assert breadcrumbs(soup) == trail
    assert breadcrumbs(soup, "breadcrumbs-compact") == trail
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
    assert breadcrumbs(soup) == [
        ("Locations", "/"),
        ("Office", f"/locations/{location.pk}/"),
        ("Device setup", None),
    ]
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
        # The trail is resolved from the URL name: it starts as the setup page's does.
        assert [label for label, _ in breadcrumbs(page)][:3] == [
            "Locations",
            "Office",
            "Device setup",
        ]
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
def test_long_name_has_the_wrapping_class_on_setup(
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
