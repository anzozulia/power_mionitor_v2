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
- Phase 5 (05-UI-SPEC security rule 3): the location page with outages listed, with the
  in-progress row and in both empty states, the removal and reset confirmation pages, and
  the location page after each of the seven Phase 5 flashes (removed, removal refused,
  already gone, removal deferred, history reset, reset refused, nothing to reset) carry
  neither the bot token nor the device key, put neither in a ``Location`` header and have
  no script. The confirmation pages have no settings panel, so not even the mask shows.

The scans run through the signed-in test client, Telegram faked at the HTTP boundary
(``fake_telegram``); the 429 page comes from five failed sign-ins in a separate client.
"""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, FakeClock, FakeTelegram
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client

from powermon.alerts import ops, outbox
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import keys
from powermon.locations.models import Location
from powermon.web.history_views import (
    HISTORY_RESET_MESSAGE,
    NOTHING_TO_RESET_MESSAGE,
    OUTAGE_GONE_MESSAGE,
    OUTAGE_REMOVED_MESSAGE,
    REMOVAL_DEFERRED_MESSAGE,
    REMOVAL_REFUSED_MESSAGE,
    RESET_REFUSED_MESSAGE,
    HistoryResetView,
    OutageRemoveView,
)
from powermon.web.location_views import LocationDetailView, local_minute

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
        "reset": f"/locations/{location.pk}/reset/",
    }


def _remove_url(location: Any, start: datetime) -> str:
    """The removal URL of the location's outage that started at ``start``."""
    return f"/locations/{location.pk}/outages/{ops.instant_us(start)}/remove/"


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


# Phase 5: the history pages and their flashes (05-UI-SPEC security rule 3)

# 2026-10-01 16:00 UTC, the pages' "now": every seeded outage below is within 14 days,
# except the quiet location's, 30 days earlier.
HISTORY_NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
IN_PROGRESS_NOTE = "The outage in progress can be removed after power returns."
NO_OUTAGES = "No outages in the last 14 days."
NO_HISTORY = "No power history yet."


def _at(hour: int, minute: int = 0) -> datetime:
    """An aware UTC instant on the history pages' day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, tzinfo=UTC)


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


def _live(location: Any, status: str, **fields: Any) -> None:
    """The live state the engine would have left with that timeline."""
    LocationState.objects.filter(pk=location.pk).update(status=status, **fields)


def _flash_texts(html: str) -> list[str]:
    return [unescape(t) for t in re.findall(r'role="(?:status|alert)">([^<]*)<', html)]


@pytest.mark.django_db
def test_INV23_2_history_pages_and_flashes_never_show_a_token_or_the_key(
    admin: Client,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(HISTORY_NOW)
    for view in (LocationDetailView, OutageRemoveView, HistoryResetView):
        monkeypatch.setattr(view, "clock", clock)
    # Two ended outages (09:00-10:00, 11:00-12:00), on since 12:00.
    office = location_factory(name="Office", bot_token=TOKEN)
    _timeline(
        office,
        ("on", _at(8), _at(9)),
        ("off", _at(9), _at(10)),
        ("on", _at(10), _at(11)),
        ("off", _at(11), _at(12)),
        ("on", _at(12), None),
    )
    _live(office, "on", last_heartbeat_at=_at(15, 59), on_since=_at(12))
    # The 11:00 outage's OFF alert is being sent right now (W1-A1).
    off_alert = outbox.enqueue(
        outbox.KIND_POWER_OFF,
        office.pk,
        event_at=_at(11),
        recorded_at=_at(11, 2),
        payload={"was_on_us": 3_600_000_000},
    )
    assert outbox.claim(off_alert.pk) is True
    # An ended outage and the one in progress since 15:00.
    shop = location_factory(name="Shop", bot_token=TOKEN)
    _timeline(
        shop,
        ("on", _at(8), _at(9)),
        ("off", _at(9), _at(10)),
        ("on", _at(10), _at(15)),
        ("off", _at(15), None),
    )
    _live(shop, "off", last_heartbeat_at=_at(15), on_since=_at(10), outage_started_at=_at(15))
    # History, but its only outage is 30 days old.
    quiet = location_factory(name="Garage", bot_token=TOKEN)
    month_ago = HISTORY_NOW - timedelta(days=30)
    _timeline(
        quiet,
        ("on", month_ago, month_ago + timedelta(hours=1)),
        ("off", month_ago + timedelta(hours=1), month_ago + timedelta(hours=2)),
        ("on", month_ago + timedelta(hours=2), None),
    )
    _live(quiet, "on", last_heartbeat_at=_at(15, 59), on_since=month_ago + timedelta(hours=2))
    # No history at all: waiting for its first heartbeat.
    fresh = location_factory(name="New", bot_token=TOKEN)
    device_keys = [loc.device_key for loc in (office, shop, quiet, fresh)]

    # (label, response, True where the settings panel shows the masked token)
    seen: list[tuple[str, Any, bool]] = []
    pages = {
        "ended outages": admin.get(_urls(office)["detail"]),
        "in-progress row": admin.get(_urls(shop)["detail"]),
        "no outage in the window": admin.get(_urls(quiet)["detail"]),
        "no history": admin.get(_urls(fresh)["detail"]),
    }
    # Each page is in the state its label names.
    assert _remove_url(office, _at(9)) in pages["ended outages"].content.decode()
    assert IN_PROGRESS_NOTE in pages["in-progress row"].content.decode()
    assert NO_OUTAGES in pages["no outage in the window"].content.decode()
    assert NO_HISTORY in pages["no history"].content.decode()
    seen += [(f"location page, {label}", page, True) for label, page in pages.items()]
    seen.append(("removal confirmation", admin.get(_remove_url(office, _at(9))), False))
    seen.append(("reset confirmation", admin.get(_urls(office)["reset"]), False))

    removed_at = " ".join(local_minute(_at(9), settings.TIME_ZONE))
    actions = [
        ("removed", _remove_url(office, _at(9)), OUTAGE_REMOVED_MESSAGE.format(start=removed_at)),
        ("already gone", _remove_url(office, _at(9)), OUTAGE_GONE_MESSAGE),
        ("removal refused", _remove_url(shop, _at(15)), REMOVAL_REFUSED_MESSAGE),
        ("removal deferred", _remove_url(office, _at(11)), REMOVAL_DEFERRED_MESSAGE),
        ("history reset", _urls(office)["reset"], HISTORY_RESET_MESSAGE),
        ("nothing to reset", _urls(office)["reset"], NOTHING_TO_RESET_MESSAGE),
        ("reset refused", _urls(shop)["reset"], RESET_REFUSED_MESSAGE),
    ]
    for label, url, flash in actions:
        response = admin.post(url, follow=True)
        # Each action reached its own flash on the location page it redirects to.
        assert _flash_texts(response.content.decode()) == [flash], label
        assert response.redirect_chain, label
        seen.append((f"location page after the {label} flash", response, True))

    assert len(seen) == 13
    for label, response, masked in seen:
        # A scan of an error page could pass silently: every page answered 200.
        assert response.status_code == 200, label
        html = response.content.decode()
        for value in (TOKEN, SECRET, *device_keys, *map(keys.mask_key, device_keys)):
            assert value not in html, label
            for url in _headers_and_urls(response):
                assert value not in url, label
        if masked:
            assert MASKED in html, label
        else:
            # No settings panel on a confirmation page: not even the masked token.
            assert MASKED not in html, label
            assert "•" not in html[html.index("<main") :], label
        _assert_no_script(label, html)
    # No action called Telegram (KD2).
    assert len(fake_telegram.calls) == 0
