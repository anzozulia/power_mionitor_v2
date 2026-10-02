"""INV-23 #2 across every Phase 4 page and action response (SEC-04; D-07, D-14; UI-SPEC
security rules 3, 4 and 8).

- The bot token never enters an HTML response or a ``Location`` header: not on the list,
  the location page, the edit form (GET, an invalid POST that typed a second token, the
  page after a valid save), either confirmation page, the setup page (masked, revealed,
  regenerated), the throttled sign-in page or after any test-message result. Where the
  settings panel or the edit help shows it, it is ``{bot_id}:••••••••`` only (D-07,
  Phase 1 D-11). Telegram's answer may carry the token in its description or an
  exception's text; the flash never shows either (D-11, OPS-08).
- The full device key appears in exactly two responses: the setup page's Reveal POST and
  the Regenerate POST (D-14), both ``Cache-Control: no-store``. After the regenerate the
  new key follows the same rule, and the old key appears nowhere.
- No scanned page has a script element (UI-SPEC rule 8).

The scans run through the signed-in test client, Telegram faked at the HTTP boundary
(``fake_telegram``); the 429 page comes from five failed sign-ins in a separate client.
"""

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, FakeTelegram
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client

from powermon.locations.models import Location

User = get_user_model()

SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
MASKED = "987654321:••••••••"
# Typed into an edit form that is invalid for another reason: never saved, never shown.
SECRET_2 = "Zq-8_Lp4Rt" * 4
TOKEN_2 = f"123123123:{SECRET_2}"
MASKED_2 = "123123123:••••••••"
# Saved by the last valid edit: from then on only its mask shows.
SECRET_3 = "Hy7_-Kd2Wv" * 4
TOKEN_3 = f"456456456:{SECRET_3}"
MASKED_3 = "456456456:••••••••"
SECRETS = (TOKEN, SECRET, TOKEN_2, SECRET_2, TOKEN_3, SECRET_3)
THROTTLE_MESSAGE = "Too many failed sign-ins. Try again in 5 minutes."


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _urls(location: Any) -> dict[str, str]:
    detail = f"/locations/{location.pk}/"
    return {
        "detail": detail,
        "edit": f"{detail}edit/",
        "delete": f"{detail}delete/",
        "setup": f"{detail}setup/",
        "regenerate": f"{detail}setup/regenerate/",
        "test": f"{detail}test-message/",
    }


def _form(location: Any, **overrides: str) -> dict[str, str]:
    """The edit POST of the location's stored values, with ``overrides``."""
    return {
        "name": location.name,
        "period_s": str(location.period_s),
        "grace_s": str(location.grace_s),
        "bot_token": "",
        "chat_id": str(location.chat_id),
        "language": location.language,
        **overrides,
    }


def _marker(page: str) -> str:
    """The regenerate confirmation's hidden marker (UI-D7)."""
    found = re.search(r'<input type="hidden" name="marker" value="([^"]*)">', page)
    assert found is not None, "no marker in the regenerate confirmation"
    return found.group(1)


def _error(status: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


def _flash_count(html: str) -> int:
    return len(re.findall(r'role="(?:status|alert)">', html))


def _headers_and_urls(response: HttpResponse) -> Iterator[str]:
    """The ``Location`` header and every URL the client followed for this response."""
    yield response.get("Location", "")
    for url, _status in getattr(response, "redirect_chain", []):
        yield url


def _assert_no_script(label: str, html: str) -> None:
    # UI-SPEC rule 8: no script, no inline handler, no external URL on any admin page.
    assert "<script" not in html, label
    assert not re.search(r"\son[a-z]+=", html), label


def _throttled_sign_in() -> HttpResponse:
    """The sign-in page answered 429 (INV-21 #2), from a client of its own.

    The wrong password typed each time is a bot token: the page never sends it back.
    """
    client = Client()
    for _ in range(5):
        client.post("/login/", {"username": "admin", "password": TOKEN})
    response = client.post("/login/", {"username": "admin", "password": TOKEN})
    assert response.status_code == 429
    assert THROTTLE_MESSAGE in response.content.decode()
    return response


def _test_message_results(admin: Client, fake_telegram: FakeTelegram, url: str) -> list[Any]:
    """The page after each test-message result: ok, 403, 401, 502, read timeout, 429.

    Telegram's description and an exception's text carry the token, as a hostile or a
    misbehaving answer could: neither may reach the flash.
    """
    fake_telegram.accept(TOKEN)
    fake_telegram.fail(TOKEN, status=403, json_body=_error(403, f"Forbidden: bot {TOKEN} kicked"))
    fake_telegram.fail(TOKEN, status=401, json_body=_error(401, f"Unauthorized {TOKEN}"))
    fake_telegram.fail(TOKEN, status=502, json_body=_error(502, f"Bad Gateway /bot{TOKEN}/"))
    fake_telegram.fail(TOKEN, exc=requests.ReadTimeout(f"read timed out: /bot{TOKEN}/sendMessage"))
    fake_telegram.fail(
        TOKEN, status=429, json_body=_error(429, f"Too Many Requests {TOKEN}", retry_after=7)
    )
    pages = [admin.post(url, follow=True) for _ in range(6)]
    assert len(fake_telegram.calls) == 6
    for page in pages:
        # Each result has its own flash on the location page it redirects to.
        assert page.status_code == 200
        assert _flash_count(page.content.decode()) == 1
    return pages


# The bot token (D-07, UI-SPEC rule 3)


@pytest.mark.django_db
def test_INV23_2_token_never_in_any_page(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    urls = _urls(location)
    # (label, response, the mask that must show there or None)
    seen: list[tuple[str, Any, str | None]] = []

    for n, page in enumerate(_test_message_results(admin, fake_telegram, urls["test"])):
        seen.append((f"test message result {n}", page, MASKED))
    seen.append(("list", admin.get("/"), None))
    seen.append(("location page", admin.get(urls["detail"]), MASKED))
    seen.append(("edit GET", admin.get(urls["edit"]), MASKED))
    # A second token typed into a form that fails on its period: re-rendered, never echoed.
    invalid = admin.post(urls["edit"], _form(location, bot_token=TOKEN_2, period_s="5"))
    assert invalid.status_code == 200
    seen.append(("edit invalid POST", invalid, MASKED))
    seen.append(("delete confirmation", admin.get(urls["delete"]), None))
    confirm = admin.get(urls["regenerate"])
    seen.append(("regenerate confirmation", confirm, None))
    seen.append(("setup masked", admin.get(urls["setup"]), MASKED))
    seen.append(("setup revealed", admin.post(urls["setup"]), MASKED))
    regenerated = admin.post(urls["regenerate"], {"marker": _marker(confirm.content.decode())})
    seen.append(("regenerate POST", regenerated, MASKED))
    saved = admin.post(urls["edit"], _form(location, name="Renamed"), follow=True)
    seen.append(("page after a valid save", saved, MASKED))
    seen.append(("sign-in 429", _throttled_sign_in(), None))
    # Last: a new token is saved; the page after it shows only the new mask.
    changed = admin.post(urls["edit"], _form(location, bot_token=TOKEN_3), follow=True)
    assert Location.objects.get(pk=location.pk).bot_token == TOKEN_3
    seen.append(("page after a token change", changed, MASKED_3))
    seen.append(("edit GET after a token change", admin.get(urls["edit"]), MASKED_3))

    for label, response, mask in seen:
        html = response.content.decode()
        for secret in SECRETS:
            assert secret not in html, label
            for url in _headers_and_urls(response):
                assert secret not in url, label
        # The typed second token was never saved, so not even its mask shows.
        assert MASKED_2 not in html, label
        if mask is not None:
            assert mask in html, label
        _assert_no_script(label, html)


# The device key (D-14, UI-SPEC rule 4)


def _keyless_responses(
    admin: Client, location: Any, fake_telegram: FakeTelegram
) -> list[tuple[str, Any]]:
    """Every Phase 4 response that must not carry the device key, in its current state."""
    urls = _urls(location)
    stored = Location.objects.get(pk=location.pk)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    return [
        ("list", admin.get("/")),
        ("location page", admin.get(urls["detail"])),
        ("edit GET", admin.get(urls["edit"])),
        ("edit invalid POST", admin.post(urls["edit"], _form(stored, period_s="5"))),
        ("page after a valid save", admin.post(urls["edit"], _form(stored), follow=True)),
        ("delete confirmation", admin.get(urls["delete"])),
        ("regenerate confirmation", admin.get(urls["regenerate"])),
        ("setup masked", admin.get(urls["setup"])),
        ("test message", admin.post(urls["test"], follow=True)),
        (
            "switch",
            admin.post(f"{urls['detail']}maintenance/", {"value": "on"}, follow=True),
        ),
        ("sign-in 429", _throttled_sign_in()),
    ]


@pytest.mark.django_db
def test_INV23_2_key_only_in_reveal_and_regenerate(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office")
    urls = _urls(location)
    key = location.device_key

    for label, response in _keyless_responses(admin, location, fake_telegram):
        html = response.content.decode()
        assert key not in html, label
        for url in _headers_and_urls(response):
            assert key not in url, label
        _assert_no_script(label, html)

    revealed = admin.post(urls["setup"])
    confirm = admin.get(urls["regenerate"]).content.decode()
    regenerated = admin.post(urls["regenerate"], {"marker": _marker(confirm)})
    new_key = Location.objects.get(pk=location.pk).device_key

    # Exactly the Reveal POST and the Regenerate POST carry a full key, never cached.
    assert new_key != key
    assert key in revealed.content.decode()
    assert new_key in regenerated.content.decode()
    assert key not in regenerated.content.decode()
    for response in (revealed, regenerated):
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        _assert_no_script("reveal/regenerate", response.content.decode())

    # After the rotation neither key shows anywhere else; a new Reveal shows the new one.
    Location.objects.filter(pk=location.pk).update(maintenance=False)
    for label, response in _keyless_responses(admin, location, fake_telegram):
        html = response.content.decode()
        for value in (key, new_key):
            assert value not in html, label
            for url in _headers_and_urls(response):
                assert value not in url, label
    again = admin.post(urls["setup"]).content.decode()
    assert new_key in again
    assert key not in again
