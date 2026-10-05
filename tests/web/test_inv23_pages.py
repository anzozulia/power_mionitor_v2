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
- No scanned page carries an injected script, a script with a body or an inline event
  handler; a script may only load from a manifest-hashed same-origin static path
  (``pages.assert_no_injected_script``; R5, UI-13).
- Phase 5 (05-UI-SPEC security rule 3): the location page with outages listed, with the
  in-progress row and in both empty states, the removal and reset confirmation pages, and
  the location page after each of the seven Phase 5 flashes (removed, removal refused,
  already gone, removal deferred, history reset, reset refused, nothing to reset) carry
  neither the bot token nor the device key, put neither in a ``Location`` header and carry
  no injected or inline script. The confirmation pages have no settings panel, so not even
  the mask shows.

Pages are read through ``pages.py``: the flashes with ``messages()``, the regenerate marker
with ``hidden_value(post_form(...), "marker")``.

Phase 6 (TEST-STRATEGY §9) extends the matrix to every new surface:

- the sidebar on every app page of the render matrix (tests/web/test_render_matrix.py
  ``CASES``, each with one more location holding the fixture token): no token, secret part,
  key, key mask or "•" in it;
- the theme POST's redirect (headers, cookies and body, a token typed into ``next`` and a key
  in the Referer included) and the built CSS and admin.js bodies;
- every action's redirect and the toasts of the page it leads to: add, test message
  (Telegram's answer carries the token), edit, remove, reset, the three switches, delete
  and sign-out, each with exactly one toast and no secret in a header;
- ``test_INV23_2_matrix_is_complete`` maps each §9 row, read from the TEST-STRATEGY table
  itself, to the routes whose responses it scans and the test functions that scan them,
  and imports each module to prove the function exists. ``INV23_ROUTES`` is exported for
  tests/web/test_routes_coverage.py: every named admin route is in some row.

The scans run through the signed-in test client, Telegram faked at the HTTP boundary
(``fake_telegram``); the 429 page comes from five failed sign-ins in a separate client.
"""

import importlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
import test_render_matrix as matrix
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, FakeTelegram
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import HttpResponse
from django.http.response import HttpResponseBase
from django.test import Client
from pages import (
    TOAST_REGIONS,
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    by_testid,
    hidden_value,
    message_texts,
    messages,
    parse,
    post_form,
)
from secret_fixtures import MASKED, MASKED_2, MASKED_3, SECRET, SECRETS, TOKEN, TOKEN_2, TOKEN_3

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
from powermon.web.location_views import (
    LocationDeleteView,
    LocationDetailView,
    LocationEditView,
    SendTestMessageView,
    SwitchView,
    local_minute,
)
from powermon.web.views import LocationCreateView

User = get_user_model()

# The render matrix's world (signed-in admin, every clock at its NOW), for the sidebar row.
env = matrix.env

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


def _marker(page: HttpResponse | str, location: Any) -> str:
    """The regenerate confirmation's hidden marker (UI-D7), whatever its attribute order."""
    return hidden_value(post_form(page, _urls(location)["regenerate"]), "marker")


def _error(status: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


def _headers_and_urls(response: HttpResponse) -> Iterator[str]:
    """The ``Location`` header and every URL the client followed for this response."""
    yield response.get("Location", "")
    for url, _status in getattr(response, "redirect_chain", []):
        yield url


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
        assert len(messages(page)) == 1
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
    regenerated = admin.post(urls["regenerate"], {"marker": _marker(confirm, location)})
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
        assert_no_injected_script(html, label)


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
        assert_no_injected_script(html, label)

    revealed = admin.post(urls["setup"])
    confirm = admin.get(urls["regenerate"]).content.decode()
    regenerated = admin.post(urls["regenerate"], {"marker": _marker(confirm, location)})
    new_key = Location.objects.get(pk=location.pk).device_key

    # Exactly the Reveal POST and the Regenerate POST carry a full key, never cached.
    assert new_key != key
    assert key in revealed.content.decode()
    assert new_key in regenerated.content.decode()
    assert key not in regenerated.content.decode()
    for response in (revealed, regenerated):
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert_no_injected_script(response.content.decode(), "reveal/regenerate")

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
        assert message_texts(response) == [flash], label
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
        assert_no_injected_script(html, label)
    # No action called Telegram (KD2).
    assert len(fake_telegram.calls) == 0


# Phase 6: the sidebar, the theme POST, the static assets, every action's redirect


@pytest.mark.django_db
@pytest.mark.parametrize("case", [case for case in matrix.CASES if case.app], ids=lambda c: c.id)
def test_INV23_2_sidebar_on_every_app_page(env: matrix.Env, case: matrix.Case) -> None:
    # Every app page of the render matrix, with one more location that holds the fixture
    # token and a key: the sidebar lists it, and shows none of its secrets, not even masked.
    probe = env.make("Sidebar probe")
    response = case.build(env)
    page = parse(response)

    sidebar = by_testid(page, "sidebar")
    listed = {str(link["title"]) for link in all_by_testid(sidebar, "sidebar-location")}
    assert probe.name in listed
    html = str(sidebar)
    assert_no_secrets(html, matrix.secrets_of(env), label=case.id)
    assert "•" not in html


def _all_keys() -> list[str]:
    """Every location's key and its mask, as they are now."""
    found: list[str] = []
    for key in Location.objects.values_list("device_key", flat=True):
        found += [key, keys.mask_key(key)]
    return found


def _header_lines(response: HttpResponseBase) -> list[str]:
    """Every response header and cookie as text."""
    lines = [f"{name}: {value}" for name, value in response.items()]
    return [*lines, *(morsel.OutputString() for morsel in response.cookies.values())]


def _body(response: HttpResponseBase) -> bytes:
    if getattr(response, "streaming", False):
        return b"".join(response.streaming_content)  # type: ignore[attr-defined]
    return response.content  # type: ignore[attr-defined,no-any-return]


@pytest.mark.django_db
def test_INV23_2_theme_post_and_static_assets(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office", bot_token=TOKEN)
    key = location.device_key
    secrets = [*SECRETS, MASKED, key, keys.mask_key(key)]

    # The theme POST: a redirect to / whose headers, cookies and body hold no secret, with
    # a token typed into next and a key in the Referer (both ignored, R8).
    response = admin.post(
        "/theme/",
        {"theme": "dark", "next": f"/?t={TOKEN}"},
        HTTP_REFERER=f"http://testserver/locations/{location.pk}/setup/?key={key}",
    )
    assert response.status_code == 302
    assert response["Location"] == "/"
    assert response.cookies["theme"].value == "dark"
    assert_no_secrets(
        response.content.decode(), secrets, label="theme POST", headers=_header_lines(response)
    )
    # The built CSS and admin.js, as WhiteNoise serves them: no secret in the bytes.
    for name in ("web/build/app.css", "web/admin.js"):
        static = admin.get("/static/" + staticfiles_storage.stored_name(name))
        assert static.status_code == 200, name
        body = _body(static)
        assert len(body) > 1000, name
        assert_no_secrets(body, secrets, label=name, headers=_header_lines(static))
    # Failure: the scan sees a secret in the bytes and in a header.
    with pytest.raises(AssertionError):
        assert_no_secrets(b"x" + key.encode(), secrets)
    with pytest.raises(AssertionError):
        assert_no_secrets("", secrets, headers=[f"Location: /?k={key}"])


@pytest.mark.django_db
def test_INV23_2_action_redirects_and_toasts(
    admin: Client,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(HISTORY_NOW)
    for view in (
        LocationCreateView,
        LocationDetailView,
        LocationEditView,
        LocationDeleteView,
        SwitchView,
        SendTestMessageView,
        OutageRemoveView,
        HistoryResetView,
    ):
        monkeypatch.setattr(view, "clock", clock)
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
    shop = location_factory(name="Shop", bot_token=TOKEN)
    # Telegram's refusal carries the token in its description: the flash never shows it.
    fake_telegram.fail(TOKEN, status=403, json_body=_error(403, f"Forbidden: bot {TOKEN} kicked"))
    urls = _urls(office)
    add = {
        "name": "New site",
        "period_s": "60",
        "grace_s": "30",
        "bot_token": TOKEN,
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "en",
    }
    actions = [
        ("add", "/locations/new/", add),
        ("test message", urls["test"], {}),
        ("edit", urls["edit"], _form(office, name="Renamed")),
        ("remove outage", _remove_url(office, _at(9)), {}),
        ("reset", urls["reset"], {}),
        ("maintenance", f"{urls['detail']}maintenance/", {"value": "on"}),
        ("alerts", f"{urls['detail']}alerts/", {"value": "off"}),
        ("router grace", f"{urls['detail']}router-grace/", {"value": "on"}),
        ("delete", _urls(shop)["delete"], {}),
        ("sign out", "/logout/", {}),
    ]

    for label, url, data in actions:
        secrets = [*SECRETS, MASKED, MASKED_2, MASKED_3, *_all_keys()]
        response = admin.post(url, data)
        # A redirect with no secret in any header, cookie or its body.
        assert response.status_code == 302, label
        assert_no_secrets(
            response.content.decode(), secrets, label=label, headers=_header_lines(response)
        )
        # The page it leads to shows exactly one toast, and no toast region holds a secret.
        page = admin.get(response["Location"])
        assert page.status_code == 200, label
        assert len(messages(page)) == 1, label
        for region in TOAST_REGIONS:
            html = str(by_testid(parse(page), region))
            assert_no_secrets(html, secrets, label=f"{label} {region}")
            assert "•" not in html, label
    assert len(fake_telegram.calls) == 1


# The §9 rows (TEST-STRATEGY, INV-23 #2): the routes each scans and the tests that scan it

STRATEGY = Path(settings.BASE_DIR) / "docs" / "phase-6" / "TEST-STRATEGY.md"
MATRIX = ("test_render_matrix", "test_UI01_render_matrix")
FRAGMENTS = ("test_fragments", "test_INV23_2_confirmations_have_no_secrets")
TOKEN_PAGES = ("test_inv23_pages", "test_INV23_2_token_never_in_any_page")
KEY_PAGES = ("test_inv23_pages", "test_INV23_2_key_only_in_reveal_and_regenerate")
HISTORY_PAGES = (
    "test_inv23_pages",
    "test_INV23_2_history_pages_and_flashes_never_show_a_token_or_the_key",
)
CONFIRMATIONS = frozenset(
    {"location-delete", "location-regenerate", "outage-remove", "location-reset"}
)
ACTIONS = frozenset(
    {
        "location-create",
        "location-test-message",
        "location-edit",
        "outage-remove",
        "location-reset",
        "location-maintenance",
        "location-alerts",
        "location-router-grace",
        "location-delete",
        "logout",
    }
)
type Proof = tuple[str, str]
INV23_ROWS: dict[str, tuple[frozenset[str], tuple[Proof, ...]]] = {
    "S1 sign-in, incl. wrong credentials and 429": (frozenset({"login"}), (MATRIX, TOKEN_PAGES)),
    "S3 list (with sidebar and fleet tiles)": (frozenset({"location-list"}), (MATRIX, TOKEN_PAGES)),
    "S4 add: GET, invalid POST with a typed token": (
        frozenset({"location-create"}),
        (MATRIX, ("test_locations", "test_R3_token_never_returned")),
    ),
    "S5 location page, every state and every flash": (
        frozenset({"location-detail"}),
        (MATRIX, HISTORY_PAGES, ("test_location_page", "test_location_page_shows_no_secret")),
    ),
    "S6 edit: GET, invalid POST (typed second token), after a valid save": (
        frozenset({"location-edit"}),
        (MATRIX, TOKEN_PAGES, ("test_edit", "test_edit_token_is_write_only")),
    ),
    "S7 delete: page and fragment": (frozenset({"location-delete"}), (MATRIX, FRAGMENTS)),
    "S8 setup GET (masked)": (
        frozenset({"location-setup"}),
        (MATRIX, ("test_setup_page", "test_UI08_key_only_in_its_elements")),
    ),
    "S8 Reveal POST (`no-store`)": (
        frozenset({"location-setup"}),
        (MATRIX, KEY_PAGES, ("test_setup_page", "test_UI08_key_only_in_its_elements")),
    ),
    "S9 regenerate: page and fragment": (frozenset({"location-regenerate"}), (MATRIX, FRAGMENTS)),
    "S9 Regenerate POST (`no-store`)": (frozenset({"location-regenerate"}), (MATRIX, KEY_PAGES)),
    "S10 remove outage, S11 reset: pages, fragments, and every refusal redirect": (
        frozenset({"outage-remove", "location-reset"}),
        (
            MATRIX,
            FRAGMENTS,
            ("test_history_confirm", "test_removal_pages_show_no_secret"),
            ("test_history_confirm", "test_reset_pages_show_no_secret"),
        ),
    ),
    "Every action's redirect and the following page's toasts (switches, test message incl. "
    "Telegram errors carrying the token, edit, delete, remove, reset)": (
        ACTIONS,
        (("test_inv23_pages", "test_INV23_2_action_redirects_and_toasts"), TOKEN_PAGES),
    ),
    "E1 404, E2 403 CSRF, E3 500": (frozenset(), (MATRIX,)),
    "Status JSON": (
        frozenset({"location-status-json"}),
        (("test_status_json", "test_INV23_2_status_json_has_no_secrets"),),
    ),
    "Chart PNG (bytes and PNG text chunks)": (
        frozenset({"location-chart"}),
        (("test_chart_preview", "test_INV23_2_chart_png_has_no_secrets"),),
    ),
    "Modal fragments (all four, both outcomes)": (CONFIRMATIONS, (FRAGMENTS,)),
    "Theme POST response (redirect)": (
        frozenset({"theme"}),
        (("test_inv23_pages", "test_INV23_2_theme_post_and_static_assets"),),
    ),
    "Sidebar (on every app page)": (
        frozenset(),
        (
            ("test_inv23_pages", "test_INV23_2_sidebar_on_every_app_page"),
            ("test_shell", "test_UI05_live_slots_hold_no_secret"),
        ),
    ),
    "Static assets (built CSS, `admin.js`)": (
        frozenset(),
        (("test_inv23_pages", "test_INV23_2_theme_post_and_static_assets"),),
    ),
}
# The named routes whose responses the INV-23 matrix scans (tests/web/test_routes_coverage.py).
INV23_ROUTES = frozenset().union(*(routes for routes, _proofs in INV23_ROWS.values()))


def strategy_rows(document: str) -> list[str]:
    """The first cell of each body row of the §9 table, its bold markers dropped."""
    section = document.split("## 9.", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    return [row.split("|")[1].strip().replace("**", "") for row in rows[2:]]


def inv23_gaps(
    rows: Mapping[str, tuple[frozenset[str], tuple[Proof, ...]]], doc: list[str]
) -> list[str]:
    """Where the mapping and the §9 table disagree, and each proof that does not exist."""
    gaps = [f"row {row!r} has no proof" for row in doc if not rows.get(row, ((), ()))[1]]
    for row, (_routes, proofs) in rows.items():
        if row not in doc:
            gaps.append(f"row {row!r} is not in the §9 table")
        gaps += [
            f"row {row!r}: {module}.{function} does not exist"
            for module, function in proofs
            if not _test_exists(module, function)
        ]
    return gaps


def _test_exists(module: str, function: str) -> bool:
    """The test module imports and defines ``function`` (tests/web is on sys.path)."""
    try:
        found = importlib.import_module(module)
    except ModuleNotFoundError:
        return False
    return callable(getattr(found, function, None))


def test_INV23_2_matrix_is_complete() -> None:
    doc = strategy_rows(STRATEGY.read_text(encoding="utf-8"))

    # Expected: every §9 row maps to the tests that prove it, and each one exists.
    assert len(doc) == 19
    assert inv23_gaps(INV23_ROWS, doc) == []
    assert {"location-status-json", "location-chart", "theme", "logout"} <= INV23_ROUTES
    # Failure: a §9 row with no mapping, a mapped row the table lacks, a missing test.
    broken = dict(INV23_ROWS)
    del broken["Status JSON"]
    broken["Status XML"] = (frozenset(), (("test_inv23_pages", "test_no_such_function"),))
    assert inv23_gaps(broken, doc) == [
        "row 'Status JSON' has no proof",
        "row 'Status XML' is not in the §9 table",
        "row 'Status XML': test_inv23_pages.test_no_such_function does not exist",
    ]
    # Edge: a row with no proof at all is a gap, and so is a module that does not exist.
    empty = {**INV23_ROWS, "Status JSON": (frozenset(), ())}
    assert inv23_gaps(empty, doc) == ["row 'Status JSON' has no proof"]
    ghost = {**INV23_ROWS, "Status JSON": (frozenset(), (("test_ghost", "test_x"),))}
    assert inv23_gaps(ghost, doc) == ["row 'Status JSON': test_ghost.test_x does not exist"]
