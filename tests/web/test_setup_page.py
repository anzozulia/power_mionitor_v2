"""The device setup page (LOC-05, HB-01, D-06, D-11; INV-23 UI part, INV-24 #3 link).

- The key is masked on GET and shown in full only in the reveal POST response (200, no
  redirect) and, with the new key, in the Regenerate POST response (Phase 4 D-14). Every
  response of the view is ``Cache-Control: no-store``. Both key states offer "Regenerate
  key", a link to the confirmation page (UI-D5).
- The heartbeat URL comes from ``PUBLIC_BASE_URL``, never from the request's Host header.
- Every example block is exactly the string the 01-09 generator returns, the same string
  ``test_examples_verbatim`` runs against ``/hb``.
- The bot token is shown only as ``{bot_id}:••••••••``.
"""

# class-guard: pending migration

import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from pages import hidden_value, post_form

from powermon.engine.models import LocationState
from powermon.locations.examples import (
    cron_lines,
    curl_cmd,
    heartbeat_url,
    wget_busybox,
    wget_gnu,
)
from powermon.locations.models import Location

User = get_user_model()

BASE_URL = "https://power.example.org"
HB_URL = f"{BASE_URL}/hb"
EXAMPLE_BLOCKS = ("example-curl", "example-cron", "example-wget-gnu", "example-wget-busybox")
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
MASKED_TOKEN = "987654321:••••••••"
REVEAL_NOTE = "Reveal the key above to fill it into these examples."
HIDDEN_AGAIN = "The key is hidden again the next time you open this page."
KEY_NOTE = "Anyone with this key can send heartbeats for this location. Keep it private."
REGENERATE_NOTE = "If the key has leaked, regenerate it. The old key stops working at once."


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


@pytest.fixture
def location(location_factory: Callable[..., Any]) -> Any:
    return location_factory(name="Office", bot_token=TOKEN, period_s=60, grace_s=30)


def _url(location: Any) -> str:
    return f"/locations/{location.pk}/setup/"


def _block(page: str, block_id: str) -> str:
    """The unescaped text of the code block with this id."""
    match = re.search(rf'<pre class="copy" id="{block_id}"><code>(.*?)</code></pre>', page, re.S)
    assert match is not None, f"no code block {block_id!r}"
    return unescape(match.group(1))


def _masked(key: str) -> str:
    return "•" * 12 + key[-4:]


def _h1_text(page: str) -> str:
    """The page's first h1 as text: tags dropped, whitespace collapsed, entities decoded.

    So an h1 with attributes or an aria-hidden icon inside still reads as its copy.
    """
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S)
    assert match is not None, "no h1 on the page"
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())


def _settings_rows(page: str) -> list[tuple[str, str]]:
    panel = re.search(r'<dl class="panel settings name">(.*?)</dl>', page, re.S)
    assert panel is not None
    pairs = re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", panel.group(1), re.S)
    return [(unescape(term), unescape(value)) for term, value in pairs]


# Masked by default, revealed only by POST


@pytest.mark.django_db
def test_LOC05_setup_masked_by_default(admin: Client, location: Any) -> None:
    key = location.device_key

    response = admin.get(_url(location))

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    page = response.content.decode()
    assert key not in page
    masked = _masked(key)
    assert len(masked) == 16
    assert f'<code id="device-key" aria-hidden="true">{masked}</code>' in page
    assert f'<span class="visually-hidden">Hidden key ending in {key[-4:]}</span>' in page
    for block in EXAMPLE_BLOCKS:
        assert masked in _block(page, block)
    assert f"<strong>Note:</strong> {REVEAL_NOTE}" in page
    assert _block(page, "heartbeat-url") == HB_URL
    reveal = re.search(
        r'<form method="post" action="([^"]+)">(.*?)</form>',
        page[page.index("<h2>Device key") :],
        re.S,
    )
    assert reveal is not None
    assert reveal.group(1) == _url(location)
    assert 'name="csrfmiddlewaretoken"' in reveal.group(2)
    assert '<button class="btn btn--primary" type="submit">Reveal key</button>' in reveal.group(2)
    assert page.count("btn--primary") == 1
    assert "Hide key" not in page
    assert HIDDEN_AGAIN not in page
    assert KEY_NOTE in page


@pytest.mark.django_db
def test_LOC05_reveal_shows_full_key_only_in_post_response(admin: Client, location: Any) -> None:
    key = location.device_key

    response = admin.post(_url(location))

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    page = response.content.decode()
    assert _block(page, "device-key") == key
    for block in EXAMPLE_BLOCKS:
        example = _block(page, block)
        assert key in example
        assert "•" not in example
    assert f'<a class="btn btn--secondary" href="{_url(location)}">Hide key</a>' in page
    assert HIDDEN_AGAIN in page
    assert KEY_NOTE in page
    assert REVEAL_NOTE not in page
    assert "Reveal key" not in page
    # No accent button in the revealed state.
    assert "btn--primary" not in page

    again = admin.get(_url(location))

    assert key not in again.content.decode()
    assert _masked(key) in again.content.decode()


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
    masked = admin.get(_url(location)).content.decode()
    revealed = admin.post(_url(location)).content.decode()

    link = (
        f'<a class="btn btn--secondary" href="/locations/{location.pk}/setup/regenerate/">'
        "Regenerate key</a>"
    )
    for page, last_note in ((masked, KEY_NOTE), (revealed, HIDDEN_AGAIN)):
        section = page[page.index("<h2>Device key") : page.index("<h2>Examples")]
        assert f"<p>{REGENERATE_NOTE}</p>" in section
        assert link in section
        # After the key note (and the "hidden again" line when revealed), UI-SPEC screen E.
        assert section.index(last_note) < section.index(REGENERATE_NOTE) < section.index(link)
        # The entry point only opens the confirmation page (UI-D5): a link, not a form.
        assert "btn--danger" not in page
    # The masked state keeps "Reveal key" as its only primary button.
    assert masked.count("btn--primary") == 1


# The examples are the generator's strings


@pytest.mark.django_db
@pytest.mark.parametrize("period", [10, 60, 120, 3600])
def test_setup_examples_match_the_generators(
    admin: Client, location_factory: Callable[..., Any], period: int
) -> None:
    location = location_factory(period_s=period)
    key = location.device_key
    url = heartbeat_url(BASE_URL)

    revealed = admin.post(_url(location)).content.decode()
    masked = admin.get(_url(location)).content.decode()

    for page, shown in ((revealed, key), (masked, _masked(key))):
        assert _block(page, "heartbeat-url") == url
        assert _block(page, "example-curl") == curl_cmd(url, shown, multiline=True)
        assert _block(page, "example-cron") == "\n".join(cron_lines(url, shown, period))
        assert _block(page, "example-wget-gnu") == wget_gnu(url, shown)
        assert _block(page, "example-wget-busybox") == wget_busybox(url, shown)


@pytest.mark.django_db
def test_setup_cron_follows_the_period(admin: Client, location_factory: Callable[..., Any]) -> None:
    fast = location_factory(name="fast", period_s=10)
    slow = location_factory(name="slow", period_s=120)
    hourly = location_factory(name="hourly", period_s=3600)

    fast_lines = _block(admin.get(_url(fast)).content.decode(), "example-cron").split("\n")
    slow_lines = _block(admin.get(_url(slow)).content.decode(), "example-cron").split("\n")
    hourly_lines = _block(admin.get(_url(hourly)).content.decode(), "example-cron").split("\n")

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
        page = response.content.decode()
        assert "evil.example" not in page
        assert _block(page, "heartbeat-url") == HB_URL
        for block in EXAMPLE_BLOCKS:
            assert HB_URL in _block(page, block)


# The bot token


@pytest.mark.django_db
def test_token_shown_only_as_bot_id(admin: Client, location: Any) -> None:
    for response in (admin.get(_url(location)), admin.post(_url(location))):
        page = response.content.decode()
        assert MASKED_TOKEN in page
        assert SECRET not in page
        assert TOKEN not in page
        assert ("Bot token", MASKED_TOKEN) in _settings_rows(page)


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
        page = response.content.decode()
        assert _h1_text(page) == "Page not found"
        assert deleted.device_key not in page


@pytest.mark.django_db
def test_reveal_requires_csrf(location: Any) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    response = client.post(_url(location))

    assert response.status_code == 403
    page = response.content.decode()
    assert _h1_text(page) == "Form expired"
    assert location.device_key not in page


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

    page = admin.get(_url(location)).content.decode()
    text = unescape(page)

    assert "<title>Dacha · Device setup · Power Monitor</title>" in page
    for heading in (
        "Before you start",
        "Heartbeat URL",
        "Device key",
        "Examples",
        "Location settings",
    ):
        assert f"<h2>{heading}</h2>" in page
    assert (
        "Use a device that loses power in a blackout: plugged into mains, with no UPS, power "
        "bank or battery. A device on backup power keeps reporting and hides the outage."
    ) in text
    assert (
        "A heartbeat needs both power and internet at the device. If the router or the "
        "provider's network is down, this location is reported OFF too."
    ) in text
    assert (
        "The device must send a heartbeat at least every 45 s. "
        "Power is reported OFF after 65 s without one."
    ) in text
    assert (
        "Send a GET or POST request to this URL. Put the key in the Authorization header; "
        "use ?key= in the URL only for devices that cannot set headers."
    ) in text
    for title in (
        "curl, key in a header (recommended)",
        "Cron (router or Linux)",
        "wget, key in the URL (GNU wget)",
        "BusyBox wget or OpenWrt uclient-fetch, key in the URL",
    ):
        assert f"<h3>{title}</h3>" in page
    assert (
        "Each line is one crontab entry; add them with <code>crontab -e</code>. Together they "
        "send a heartbeat at least every 45 s. No curl on the device? Put one of the wget "
        "commands below in place of the curl command."
    ) in page
    assert (
        "These do not accept <code>--max-redirect</code>. The heartbeat URL never redirects."
    ) in page
    assert (
        "<strong>Warning:</strong> Do not paste a URL that contains the key into Telegram or "
        "any other chat. Chat apps open links to build previews, and every opening counts as "
        "a heartbeat: it can mark this location ON while the power is off."
    ) in page
    assert (
        "A working heartbeat gets HTTP 200 with the body ok. HTTP 401 means the key is missing "
        "or wrong, and nothing is recorded. After the first heartbeat this location shows On; "
        "the first heartbeat sends no alert."
    ) in page
    assert _settings_rows(page) == [
        ("Language", "Russian"),
        ("Heartbeat period", "45 s"),
        ("Grace period", "20 s"),
        ("Reported OFF after", "65 s without a heartbeat"),
        ("Channel chat ID", "-1009876543210"),
        ("Bot token", "123456789:••••••••"),
    ]
    # UI-D10: the Phase 1 note is replaced by the "Edit location" link-button under the panel.
    assert "Settings cannot be changed yet" not in page
    settings_section = page[page.index("<h2>Location settings</h2>") :]
    assert settings_section.index("</dl>") < settings_section.index("Edit location")
    assert (
        f'<p><a class="btn btn--secondary" href="/locations/{location.pk}/edit/">'
        "Edit location</a></p>"
    ) in settings_section


@pytest.mark.django_db
def test_setup_meta_line_shows_status_and_last_heartbeat(admin: Client, location: Any) -> None:
    waiting = admin.get(_url(location)).content.decode()
    beat = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    LocationState.objects.filter(location=location).update(
        status="on", last_heartbeat_at=beat, on_since=beat
    )
    on = admin.get(_url(location)).content.decode()

    assert re.search(
        r'<p class="meta"><span class="status status--waiting">Waiting for first heartbeat'
        r'</span> · Last heartbeat: <span class="num">Never</span></p>',
        waiting,
    )
    # 00:30 UTC on the fall-back day is the first 03:30 in Kyiv, still summer time.
    assert re.search(
        r'<p class="meta"><span class="status status--on">On</span> · Last heartbeat: '
        r'<span class="num">2026-10-25 03:30:00 EEST</span></p>',
        on,
    )


@pytest.mark.django_db
def test_setup_meta_line_shows_maintenance(admin: Client, location: Any) -> None:
    # The Phase 4 vocabulary (D-13, UI-SPEC screen E): "Maintenance" whenever the flag is
    # on, whatever the stored status underneath.
    beat = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    LocationState.objects.filter(location=location).update(
        status="on", last_heartbeat_at=beat, on_since=beat
    )
    Location.objects.filter(pk=location.pk).update(maintenance=True)

    page = admin.get(_url(location)).content.decode()

    assert re.search(
        r'<p class="meta"><span class="status status--maintenance">Maintenance</span> · '
        r'Last heartbeat: <span class="num">2026-10-25 03:30:00 EEST</span></p>',
        page,
    )
