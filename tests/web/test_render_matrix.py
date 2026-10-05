"""The render matrix: every admin route x state in the rebuilt design (UI-01, UI-12, UI-13;
TEST-STRATEGY §5.2, §7.1, §7.5 UI-12 row, §9).

One table, ``CASES``, names each screen and state of ADMIN-INVENTORY by its UI-SPEC id
(S1 sign-in, S3 list, S4 add, S5 location page, S6 edit, S7 delete, S8 device setup, S9
regenerate, S10 remove outage, S11 reset, E1-E3 the error pages) and builds it through the
signed-in test client, the real middleware and every context processor. Every case:

- passes ``pages.assert_page`` (the 15 page invariants) with ``app=True`` on the app layout
  and ``app=False`` on the sign-in and error pages, with its status and page title;
- shows the breadcrumb trail of the page it is (the Regenerate POST answers with the
  setup page, so it shows the setup trail: ``crumbs.POST_TRAILS``);
- renders exactly the Alpine components its page binds (every ``x-data`` value), and only
  the ``data-live`` values the poll knows; ``[data-power]`` only on S5; the fleet-showing
  parts only inside the polite, atomic S3 sentence; ``#recent-outages`` and
  ``#reset-history`` keep ``tabindex="-1"`` (W5-A1);
- gives every POST form a non-empty CSRF token (a partial included with ``only`` drops the
  context processors' token unless its caller passes it on);
- passes the secret scan with its TEST-STRATEGY §9 allowances: the bot token (full or its
  secret part) and the ops token never; the token mask only in ``settings-panel`` (S5, S8)
  or ``masked-token`` (S6); the device key only in ``#device-key`` and the four examples of
  a revealed S8; the key mask there on a masked S8, and its tail also in the visually
  hidden "Hidden key ending in" text; a key replaced by Regenerate never. The scan reads
  the decoded body with the CSRF values blanked (a random token may hold a key tail).

The a11y test re-reads the same cases (UI-12): every id reference (``for``,
``aria-labelledby``, ``aria-describedby``, ``aria-controls``, ``popovertarget``,
``data-copy-target`` and same-page ``#`` links, the error summary's included) points at an
element on the page; ids are unique; tables are named and their header cells scoped; live
regions are polite, assertive or a nested ``off`` silencer (S8 step 5's received line); every
JS-only control ships hidden; and every location name, a 100-character one included, shows
whole in the h1, the list link text and the sidebar link text and ``title`` (01-UAT #7 as
UI-12). A separate test proves a 100-letter Cyrillic name (100 code points, 200 UTF-8
bytes) passes the name limit and shows whole the same way.

``MATRIX_ROUTES`` (the URL names the matrix renders) is exported for the completeness check
in tests/web/test_routes_coverage.py (TEST-STRATEGY §5.6).

Clocks are injected (FakeClock on every view and on the sidebar and timefmt clocks), Telegram
is faked at the HTTP boundary, and the timeline helpers are copied from
tests/web/test_inv23_pages.py (test directories have no ``__init__.py``).
"""

import dataclasses
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from bs4 import BeautifulSoup, Tag
from conftest import DEFAULT_CHAT_ID, OPS_BOT_TOKEN, OPS_CHAT_ID, FakeClock, FakeTelegram
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import Client
from pages import (
    all_by_testid,
    assert_no_secrets,
    assert_page,
    breadcrumbs,
    by_testid,
    h1,
    hidden_value,
    messages,
    parse,
    post_form,
    text,
)
from secret_fixtures import MASKED, MASKED_2, MASKED_3, SECRETS, TOKEN, TOKEN_2, TOKEN_3
from urls_raise import RAISE_PATH

from powermon.alerts import delivery, ops, outbox
from powermon.engine import maintenance
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import keys, validators
from powermon.locations.models import Location
from powermon.web import context_processors
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
    ALERTS_COPY,
    ALREADY_REGENERATED_MESSAGE,
    CHANGES_SAVED_MESSAGE,
    CHANNEL_CHANGED_MESSAGE,
    DELIVERY_BOT_REJECTED_CAUSE,
    DELIVERY_CANNOT_POST_CAUSE,
    DELIVERY_MIGRATE_LINE,
    DELIVERY_NOT_IN_CHAT_CAUSE,
    DELIVERY_OTHER_CAUSE,
    LOCATION_DELETED_MESSAGE,
    MAINTENANCE_COPY,
    REGENERATED_MESSAGE,
    ROUTER_GRACE_COPY,
    TEST_BOT_REJECTED_MESSAGE,
    TEST_MAYBE_SENT_MESSAGE,
    TEST_NOT_IN_CHAT_MESSAGE,
    TEST_RATE_LIMITED_MESSAGE,
    TEST_RECOVERED_MESSAGE,
    TEST_REFUSED_MESSAGE,
    TEST_SENT_MESSAGE,
    TEST_SERVER_ERROR_MESSAGE,
    TEST_UNREACHABLE_MESSAGE,
    LocationDeleteView,
    LocationDetailView,
    LocationEditView,
    SendTestMessageView,
    SwitchView,
    local_minute,
)
from powermon.web.templatetags import crumbs, timefmt
from powermon.web.views import LOCATION_CREATED_MESSAGE, LocationCreateView, LocationListView

User = get_user_model()

# The pages' "now": 2026-10-01 16:00 UTC (19:00 in Kyiv). Every seeded outage below is within
# 14 days of it, except the quiet location's, 30 days earlier.
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
BASE_URL = "https://power.example.org"
# Every key the matrix creates ends in letters no hex hash, CSRF-free page text or ISO time
# holds, so its tail can be scanned on its own (the CSRF values are blanked first).
KEY_TAIL = "QZXK"
EXAMPLES = ("example-curl", "example-cron", "example-wget-gnu", "example-wget-busybox")
KEY_TARGETS = ("device-key", *EXAMPLES)
OPS_SECRET = OPS_BOT_TOKEN.partition(":")[2]
# A 100-character name with spaces: the longest the name field accepts (01-UAT #7, UI-12).
LONG_NAME = ("Kyiv Obolon district office near the river " * 3)[:99] + "X"
ERROR_TITLES = {404: "Page not found", 403: "Form expired", 500: "Server error"}
THEMES = ("light", "dark", "system")

# The Alpine components a page binds (x-data values). Every app page has the shell four.
SHELL = frozenset({"relative", "sidebar", "theme", "toasts"})
S3_LIVE = SHELL | {"poll", "fleetFilter"}
FORM = SHELL | {"offAfterHint"}
FORM_ERRORS = FORM | {"errorSummary"}
S5 = SHELL | {"poll", "confirmDialog", "sectionNav", "copy", "chartImage"}
S5_NO_CHART = S5 - {"chartImage"}
S8 = SHELL | {"poll", "confirmDialog", "tabs", "copy"}
S8_REVEALED = S8 | {"revealGuard"}
SIGN_IN = frozenset({"toasts"})
NONE: frozenset[str] = frozenset()

# The data-live vocabulary (06-UI-SPEC Test hooks, plus 06-12's sidebar-sr, sidebar-fail,
# summary-sr and sidebar-count). The aggregate ones carry no location id.
LIVE_PER_LOCATION = frozenset(
    {
        "status",
        "since",
        "last-heartbeat",
        "delivery",
        "first-heartbeat",
        "sidebar-cell",
        "sidebar-sr",
        "sidebar-fail",
    }
)
LIVE_AGGREGATE = frozenset({"count", "summary", "summary-sr", "sidebar-count"})
LIVE_VALUES = LIVE_PER_LOCATION | LIVE_AGGREGATE
POWER_VALUES = frozenset({"on", "off", "waiting"})
DELIVERY_VARIANTS = frozenset({"ok", "failing"})
SHOWING_PARTS = (
    "data-showing-all",
    "data-showing-shown",
    "data-showing-of",
    "data-showing-total",
    "data-showing-noun",
)
# Same-page anchors that a dialog or menu link jumps to keep tabindex="-1" (W5-A1).
JUMP_TARGETS = ("recent-outages", "reset-history")
ID_REFERENCES = ("aria-labelledby", "aria-describedby", "aria-controls", "popovertarget")
# The confirmation title the confirm dialog shell names before its fragment is loaded.
DIALOG_TITLE = "confirm-title"
CONFIRMATIONS = ("· Delete", "· Regenerate key", "· Remove outage", "· Reset history")
LIVE_POLITENESS = frozenset({"polite", "assertive", "off"})
ADMIN_JS = Path(django_settings.BASE_DIR) / "powermon" / "web" / "static" / "web" / "admin.js"
_REGISTRATION = re.compile(r"\bAlpine\s*\.\s*data\s*\(\s*\"(?P<name>[^\"]+)\"")


# The world a case is built in


@dataclass
class Env:
    """What a case builds its page with: the signed-in admin, Telegram, its locations."""

    admin: Client
    telegram: FakeTelegram
    settings: Any
    factory: Callable[..., Any]
    locations: list[Any] = field(default_factory=list)
    main: Any = None
    old_keys: list[str] = field(default_factory=list)

    def make(self, name: str = "Office", **fields: Any) -> Any:
        """A location with the fixture bot token and a key ending in ``KEY_TAIL``; the first
        one made is the case's main location (its title, trail and allowances)."""
        fields.setdefault("bot_token", TOKEN)
        fields.setdefault("device_key", keys.generate_device_key()[:28] + KEY_TAIL)
        location = self.factory(name=name, **fields)
        self.adopt(location)
        return location

    def adopt(self, location: Any) -> None:
        self.locations.append(location)
        if self.main is None:
            self.main = location

    def ops_off(self) -> None:
        """The ops chat is not configured (the env file default)."""
        self.settings.CFG = dataclasses.replace(
            self.settings.CFG, ops_bot_token="", ops_chat_id=None
        )

    def current(self, location: Any) -> Any:
        return Location.objects.get(pk=location.pk)


def _at(hour: int, minute: int = 0) -> datetime:
    """An aware UTC instant on the pages' day (2026-10-01)."""
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


def _detail(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _remove(location: Any, start: datetime) -> str:
    return f"/locations/{location.pk}/outages/{ops.instant_us(start)}/remove/"


def _office(env: Env, name: str = "Office", **fields: Any) -> Any:
    """Two ended outages (09:00-10:00, 11:00-12:00), on since 12:00."""
    office = env.make(name, **fields)
    _timeline(
        office,
        ("on", _at(8), _at(9)),
        ("off", _at(9), _at(10)),
        ("on", _at(10), _at(11)),
        ("off", _at(11), _at(12)),
        ("on", _at(12), None),
    )
    _live(office, "on", last_heartbeat_at=_at(15, 59), on_since=_at(12))
    return office


def _shop(env: Env, name: str = "Shop") -> Any:
    """An ended outage and the one in progress since 15:00."""
    shop = env.make(name)
    _timeline(
        shop,
        ("on", _at(8), _at(9)),
        ("off", _at(9), _at(10)),
        ("on", _at(10), _at(15)),
        ("off", _at(15), None),
    )
    _live(shop, "off", last_heartbeat_at=_at(15), on_since=_at(10), outage_started_at=_at(15))
    return shop


def _quiet(env: Env) -> Any:
    """History, but its only outage is 30 days old."""
    quiet = env.make("Garage")
    month_ago = NOW - timedelta(days=30)
    _timeline(
        quiet,
        ("on", month_ago, month_ago + timedelta(hours=1)),
        ("off", month_ago + timedelta(hours=1), month_ago + timedelta(hours=2)),
        ("on", month_ago + timedelta(hours=2), None),
    )
    _live(quiet, "on", last_heartbeat_at=_at(15, 59), on_since=month_ago + timedelta(hours=2))
    return quiet


def _on(location: Any) -> Any:
    """On since 07:30 with a heartbeat a minute ago, and that one on piece."""
    _timeline(location, ("on", _at(7, 30), None))
    _live(location, "on", last_heartbeat_at=_at(15, 59), on_since=_at(7, 30))
    return location


def _off(location: Any) -> Any:
    """Off since 14:00 after an on piece."""
    _timeline(location, ("on", _at(7, 30), _at(14)), ("off", _at(14), None))
    _live(
        location,
        "off",
        last_heartbeat_at=_at(13, 59),
        on_since=_at(7, 30),
        outage_started_at=_at(14),
    )
    return location


def _fail(
    location: Any, started_at: datetime, status: int = 403, migrate: int | None = None
) -> None:
    """Open the location's delivery_failing incident as the relay does (D-10)."""
    with transaction.atomic():
        delivery.open_failing(location.pk, started_at, status, migrate)


def _edit_form(location: Any, **overrides: str) -> dict[str, str]:
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


def _add_form(**overrides: str) -> dict[str, str]:
    """A valid add POST, with ``overrides``."""
    return {
        "name": "New site",
        "period_s": "60",
        "grace_s": "30",
        "bot_token": TOKEN,
        "chat_id": str(DEFAULT_CHAT_ID),
        "language": "en",
        **overrides,
    }


def _telegram_error(status: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


def _marker(env: Env, location: Any) -> str:
    """The regenerate confirmation's hidden marker (UI-D7)."""
    url = f"/locations/{location.pk}/setup/regenerate/"
    return hidden_value(post_form(env.admin.get(url), url), "marker")


# One case: a screen in one state


type Build = Callable[[Env], HttpResponse]
type Check = Callable[[Env, BeautifulSoup], None]


@dataclass(frozen=True)
class Case:
    """A screen in one state, and what its page must be.

    ``route`` is the URL name of the page the response is (None for E1-E3); ``trail`` the
    route whose breadcrumbs it shows (default ``route``). ``title`` may name the main
    location as ``{name}``. ``surface`` picks the §9 allowances. ``xdata`` is the exact set
    of Alpine components the page binds. ``check`` proves the page is in the state its id
    names.
    """

    id: str
    route: str | None
    build: Build
    title: str
    xdata: frozenset[str]
    surface: str = "bare"
    app: bool = True
    status: int = 200
    trail: str | None = None
    theme: str | None = None
    check: Check | None = None
    urls: str | None = None


def _flash(level: str, flash: str) -> Check:
    """The page shows exactly this one flash as a toast of this level."""

    def check(env: Env, page: BeautifulSoup) -> None:
        assert [(found.level, found.text) for found in messages(page)] == [(level, flash)]

    return check


def _testid(name: str, **attributes: str) -> Check:
    """The page has exactly one ``name`` hook with these attribute values."""

    def check(env: Env, page: BeautifulSoup) -> None:
        element = by_testid(page, name)
        for attribute, value in attributes.items():
            assert element.get(attribute) == value, (name, attribute, element.get(attribute))

    return check


def _both(*checks: Check) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        for one in checks:
            one(env, page)

    return check


# S1 sign in (auth layout)


def _s1_get(env: Env) -> HttpResponse:
    return Client().get("/login/")


def _s1_wrong(env: Env) -> HttpResponse:
    # The wrong password is a bot token: the page never sends it back.
    return Client().post("/login/", {"username": "admin", "password": TOKEN})


def _s1_throttled(env: Env) -> HttpResponse:
    client = Client()
    for _ in range(5):
        client.post("/login/", {"username": "admin", "password": TOKEN})
    return client.post("/login/", {"username": "admin", "password": TOKEN})


def _s1_theme(theme: str) -> Build:
    def build(env: Env) -> HttpResponse:
        client = Client()
        client.cookies["theme"] = theme
        return client.get("/login/")

    return build


# S3 locations


def _s3_empty(env: Env) -> HttpResponse:
    return env.admin.get("/")


def _s3_every_status(env: Env) -> HttpResponse:
    _on(env.make(LONG_NAME))
    _off(env.make("Lviv home"))
    env.make("New site")
    mnt = _on(env.make("Garage"))
    Location.objects.filter(pk=mnt.pk).update(maintenance=True)
    return env.admin.get("/")


def _s3_tags(env: Env) -> HttpResponse:
    _on(env.make("Office", alerts_enabled=False, router_grace=True))
    return env.admin.get("/")


def _s3_failing(env: Env) -> HttpResponse:
    today = _on(env.make("Office"))
    earlier = _off(env.make("Shop"))
    _fail(today, _at(10), 403)
    _fail(earlier, datetime(2026, 9, 29, 7, 5, tzinfo=UTC), 400)
    return env.admin.get("/")


def _s3_ops_unset(env: Env) -> HttpResponse:
    env.ops_off()
    _on(env.make("Office"))
    return env.admin.get("/")


def _s3_theme(theme: str) -> Build:
    def build(env: Env) -> HttpResponse:
        _on(env.make("Office"))
        env.admin.cookies["theme"] = theme
        return env.admin.get("/")

    return build


def _s3_after_delete(env: Env) -> HttpResponse:
    gone = _on(env.make("Office"))
    _on(env.make("Shop"))
    url = f"/locations/{gone.pk}/delete/"
    env.admin.get(url)
    return env.admin.post(url, follow=True)


# S4 add location


def _s4_get(env: Env) -> HttpResponse:
    return env.admin.get("/locations/new/")


def _s4_invalid(env: Env) -> HttpResponse:
    return env.admin.post("/locations/new/", _add_form(name="", bot_token=""))


def _s4_invalid_token(env: Env) -> HttpResponse:
    # A token typed into a form that fails on its period: re-rendered, never echoed.
    return env.admin.post("/locations/new/", _add_form(bot_token=TOKEN_2, period_s="5"))


# S6 edit location


def _s6_get(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    return env.admin.get(f"/locations/{office.pk}/edit/")


def _s6_invalid(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    return env.admin.post(f"/locations/{office.pk}/edit/", _edit_form(office, period_s="5"))


def _s6_invalid_token(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    form = _edit_form(office, period_s="5", bot_token=TOKEN_2)
    return env.admin.post(f"/locations/{office.pk}/edit/", form)


def _s6_after_rename(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    url = f"/locations/{office.pk}/edit/"
    saved = env.admin.post(url, _edit_form(office, name="Kyiv office"), follow=True)
    assert saved.status_code == 200
    return env.admin.get(url)


# S5 location page


def _s5(build: Callable[[Env], Any]) -> Build:
    """The location page of the location ``build`` makes."""

    def page(env: Env) -> HttpResponse:
        return env.admin.get(_detail(build(env)))

    return page


def _s5_failing(status: int, migrate: int | None = None) -> Build:
    def page(env: Env) -> HttpResponse:
        office = _on(env.make("Office"))
        _fail(office, _at(15, 2), status, migrate)
        return env.admin.get(_detail(office))

    return page


def _s5_maintenance(power: Callable[[Any], Any]) -> Build:
    def page(env: Env) -> HttpResponse:
        office = power(env.make("Office"))
        Location.objects.filter(pk=office.pk).update(maintenance=True)
        return env.admin.get(_detail(office))

    return page


def _s5_long_name(env: Env) -> HttpResponse:
    office = _office(env, LONG_NAME)
    _on(env.make("Shop"))
    return env.admin.get(_detail(office))


# S5 after each Phase 5 flash


def _removed(env: Env) -> HttpResponse:
    office = _office(env)
    return env.admin.post(_remove(office, _at(9)), follow=True)


def _already_gone(env: Env) -> HttpResponse:
    office = _office(env)
    return env.admin.post(_remove(office, _at(8, 30)), follow=True)


def _removal_refused(env: Env) -> HttpResponse:
    shop = _shop(env)
    return env.admin.post(_remove(shop, _at(15)), follow=True)


def _removal_deferred(env: Env) -> HttpResponse:
    office = _office(env)
    # The 11:00 outage's OFF alert is being sent right now (W1-A1).
    alert = outbox.enqueue(
        outbox.KIND_POWER_OFF,
        office.pk,
        event_at=_at(11),
        recorded_at=_at(11, 2),
        payload={"was_on_us": 3_600_000_000},
    )
    assert outbox.claim(alert.pk) is True
    return env.admin.post(_remove(office, _at(11)), follow=True)


def _history_reset(env: Env) -> HttpResponse:
    office = _office(env)
    return env.admin.post(f"/locations/{office.pk}/reset/", follow=True)


def _nothing_to_reset(env: Env) -> HttpResponse:
    fresh = env.make("New site")
    return env.admin.post(f"/locations/{fresh.pk}/reset/", follow=True)


def _reset_refused(env: Env) -> HttpResponse:
    shop = _shop(env)
    return env.admin.post(f"/locations/{shop.pk}/reset/", follow=True)


# S5 after each switch flash: (initial fields, the switch's flag setter, posted value)

SWITCHES: dict[str, dict[str, str]] = {
    "maintenance": MAINTENANCE_COPY,
    "alerts": ALERTS_COPY,
    "router-grace": ROUTER_GRACE_COPY,
}


def _switch(name: str, key: str) -> Build:
    value = key.removeprefix("already_")
    # The flag already has the posted value for "already_*", the other value otherwise.
    start_on = (value == "on") == key.startswith("already_")

    def page(env: Env) -> HttpResponse:
        office = _on(env.make("Office"))
        if name == "maintenance" and start_on:
            assert maintenance.set_maintenance(office.pk, True, _at(15)) is True
        elif name == "alerts":
            Location.objects.filter(pk=office.pk).update(alerts_enabled=start_on)
        elif name == "router-grace":
            Location.objects.filter(pk=office.pk).update(router_grace=start_on)
        return env.admin.post(f"{_detail(office)}{name}/", {"value": value}, follow=True)

    return page


def _switch_flash(name: str, key: str) -> Check:
    level = "info" if key.startswith("already_") else "success"
    copy = SWITCHES[name][key]
    if name == "router-grace":
        copy = copy.format(off_after_s=90)
    return _flash(level, copy)


# S5 after each test-message result

TEST_RESULTS: dict[str, tuple[dict[str, Any], str, str]] = {
    "sent": ({}, "success", TEST_SENT_MESSAGE),
    "recovered": ({}, "success", TEST_RECOVERED_MESSAGE),
    "not-in-chat": (
        {"status": 403, "json_body": _telegram_error(403, f"Forbidden: bot {TOKEN} kicked")},
        "error",
        TEST_NOT_IN_CHAT_MESSAGE.format(code="http_403"),
    ),
    "bot-rejected": (
        {"status": 401, "json_body": _telegram_error(401, f"Unauthorized {TOKEN}")},
        "error",
        TEST_BOT_REJECTED_MESSAGE.format(code="http_401"),
    ),
    "refused": (
        {"status": 409, "json_body": _telegram_error(409, f"Conflict /bot{TOKEN}/")},
        "error",
        TEST_REFUSED_MESSAGE.format(code="http_409"),
    ),
    "maybe-sent": (
        {"exc": requests.ReadTimeout(f"read timed out: /bot{TOKEN}/sendMessage")},
        "warning",
        TEST_MAYBE_SENT_MESSAGE.format(code="read_timeout"),
    ),
    "unreachable": (
        {"exc": requests.ConnectTimeout(f"connect timed out: /bot{TOKEN}/sendMessage")},
        "error",
        TEST_UNREACHABLE_MESSAGE.format(code="connect_timeout"),
    ),
    "server-error": (
        {"status": 502, "json_body": _telegram_error(502, f"Bad Gateway /bot{TOKEN}/")},
        "error",
        TEST_SERVER_ERROR_MESSAGE.format(code="http_502"),
    ),
    "rate-limited": (
        {"status": 429, "json_body": _telegram_error(429, f"Too Many {TOKEN}", retry_after=30)},
        "warning",
        TEST_RATE_LIMITED_MESSAGE.format(wait="30 seconds"),
    ),
}


def _test_message(result: str) -> Build:
    answer, _level, _text = TEST_RESULTS[result]

    def page(env: Env) -> HttpResponse:
        office = _on(env.make("Office"))
        if result == "recovered":
            _fail(office, _at(15), 403)
        if answer:
            env.telegram.fail(TOKEN, **answer)
        else:
            env.telegram.accept(TOKEN)
        return env.admin.post(f"{_detail(office)}test-message/", follow=True)

    return page


def _saved(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    url = f"/locations/{office.pk}/edit/"
    return env.admin.post(url, _edit_form(office, name="Kyiv office"), follow=True)


def _channel_changed(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    url = f"/locations/{office.pk}/edit/"
    return env.admin.post(url, _edit_form(office, bot_token=TOKEN_3), follow=True)


# S7, S9, S10, S11 confirmation pages


def _s7(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    return env.admin.get(f"/locations/{office.pk}/delete/")


def _s9(state: Callable[[Env], Any]) -> Build:
    def page(env: Env) -> HttpResponse:
        location = state(env)
        return env.admin.get(f"/locations/{location.pk}/setup/regenerate/")

    return page


def _s9_maintenance(env: Env) -> Any:
    office = _on(env.make("Office"))
    Location.objects.filter(pk=office.pk).update(maintenance=True)
    return office


def _s10_plain(env: Env) -> HttpResponse:
    office = _office(env)
    return env.admin.get(_remove(office, _at(9)))


def _s10_not_monitored(env: Env) -> HttpResponse:
    # 09:00-11:00 with 10:00-10:10 not monitored inside: the off time is shorter than the span.
    paused = env.make("Paused")
    PowerInterval.objects.create(location=paused, state="on", start_at=_at(8), end_at=_at(9))
    for start, end in ((_at(9), _at(10)), (_at(10, 10), _at(11))):
        PowerInterval.objects.create(
            location=paused, state="off", start_at=start, end_at=end, outage_start_at=_at(9)
        )
    PowerInterval.objects.create(
        location=paused, state="not_monitored", start_at=_at(10), end_at=_at(10, 10)
    )
    PowerInterval.objects.create(location=paused, state="on", start_at=_at(11), end_at=None)
    _live(paused, "on", last_heartbeat_at=_at(15, 59), on_since=_at(11))
    return env.admin.get(_remove(paused, _at(9)))


def _s11(env: Env) -> HttpResponse:
    office = _office(env)
    return env.admin.get(f"/locations/{office.pk}/reset/")


# S8 device setup


def _s8_masked(power: Callable[[Any], Any]) -> Build:
    def page(env: Env) -> HttpResponse:
        office = power(env.make("Office"))
        return env.admin.get(f"/locations/{office.pk}/setup/")

    return page


def _s8_revealed(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    return env.admin.post(f"/locations/{office.pk}/setup/")


def _s8_regenerated(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    env.old_keys.append(office.device_key)
    marker = _marker(env, office)
    return env.admin.post(f"/locations/{office.pk}/setup/regenerate/", {"marker": marker})


def _s8_already_regenerated(env: Env) -> HttpResponse:
    office = _on(env.make("Office"))
    env.old_keys.append(office.device_key)
    marker = _marker(env, office)
    url = f"/locations/{office.pk}/setup/regenerate/"
    first = env.admin.post(url, {"marker": marker})
    assert first.status_code == 200
    # A resubmit of the same confirmation: the key is not replaced a second time.
    return env.admin.post(url, {"marker": marker})


def _s8_after_create(env: Env) -> HttpResponse:
    response = env.admin.post("/locations/new/", _add_form(), follow=True)
    env.adopt(Location.objects.get(name="New site"))
    return response


# E1-E3 error pages (error layout, context-free)


def _browser() -> Client:
    """A signed-in browser with a theme=dark cookie that the error pages ignore (R11)."""
    browser = Client(enforce_csrf_checks=True, raise_request_exception=False)
    browser.force_login(User.objects.get(username="admin"))
    browser.cookies["theme"] = "dark"
    return browser


def _e1(env: Env) -> HttpResponse:
    _on(env.make("Office"))
    return _browser().get("/no-such-page-xyz")


def _e2(env: Env) -> HttpResponse:
    _on(env.make("Office"))
    return _browser().post("/locations/new/", _add_form())


def _e3(env: Env) -> HttpResponse:
    _on(env.make("Office"))
    return _browser().get(RAISE_PATH)


# State checks


def _rows(count: int) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        table = by_testid(page, "locations-table")
        assert len(all_by_testid(table, "location-row")) == count

    return check


def _statuses(*expected: str) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        table = by_testid(page, "locations-table")
        rows = all_by_testid(table, "location-row")
        assert sorted(str(row["data-status"]) for row in rows) == sorted(expected)

    return check


def _error_summary(*fields: str) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        summary = by_testid(page, "error-summary")
        hrefs = [str(link["href"]) for link in summary.find_all("a")]
        assert hrefs == [f"#id_{name}" for name in fields], hrefs

    return check


def _delivery_cause(cause: str) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        banner = by_testid(page, "delivery-banner")
        assert text(by_testid(banner, "delivery-cause")) == cause

    return check


def _power(value: str) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        panel = by_testid(page, "status-panel")
        assert [str(found["data-power"]) for found in panel.select("[data-power]")] == [value]
        by_testid(page, "maintenance-banner")

    return check


def _has_chart(env: Env, page: BeautifulSoup) -> None:
    figure = by_testid(page, "weekly-chart")
    assert len(figure.find_all("img")) == 1
    assert not all_by_testid(page, "weekly-chart-empty")


def _no_chart(env: Env, page: BeautifulSoup) -> None:
    by_testid(page, "weekly-chart-empty")
    assert not page.find_all("img")


def _consequences(count: int) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        assert len(by_testid(page, "consequences").find_all("li")) == count

    return check


def _key_state(state: str) -> Check:
    return _testid("device-key", **{"data-state": state})


def _first_heartbeat(received: bool) -> Check:
    def check(env: Env, page: BeautifulSoup) -> None:
        # Without JS the waiting line stays hidden (data-js-only); the received line shows
        # for every power state but waiting.
        step = by_testid(page, "first-heartbeat")
        shown = [str(found["data-fh"]) for found in step.select("[data-fh]:not([hidden])")]
        assert shown == (["received"] if received else [])
        assert step.select('[data-fh="waiting"][data-js-only][hidden]')

    return check


def _removed_flash(env: Env, page: BeautifulSoup) -> None:
    start = " ".join(local_minute(_at(9), django_settings.TIME_ZONE))
    _flash("success", OUTAGE_REMOVED_MESSAGE.format(start=start))(env, page)


def _tags(env: Env, page: BeautifulSoup) -> None:
    table = by_testid(page, "locations-table")
    assert [str(tag["data-tag"]) for tag in all_by_testid(table, "tag")] == [
        "alerts-off",
        "router-grace",
    ]


def _all_failing(env: Env, page: BeautifulSoup) -> None:
    rows = all_by_testid(by_testid(page, "locations-table"), "location-row")
    assert [str(row["data-delivery"]) for row in rows] == ["failing", "failing"]


def _renamed(env: Env, page: BeautifulSoup) -> None:
    assert env.current(env.main).name == "Kyiv office"
    assert by_testid(page, "location-form").find(id="id_name").get("value") == "Kyiv office"  # type: ignore[union-attr]


# The matrix (TEST-STRATEGY §7.1)

CASES: list[Case] = [
    # S1 sign in: normal, wrong credentials, throttled 429, and each theme cookie value.
    Case("S1-normal", "login", _s1_get, "Sign in", SIGN_IN, app=False),
    Case(
        "S1-wrong-credentials",
        "login",
        _s1_wrong,
        "Sign in",
        SIGN_IN,
        app=False,
        check=_testid("form-error", role="alert"),
    ),
    Case(
        "S1-throttled",
        "login",
        _s1_throttled,
        "Sign in",
        SIGN_IN | {"throttleCountdown"},
        app=False,
        status=429,
        check=_testid("throttle-message", role="alert"),
    ),
    *(
        Case(
            f"S1-theme-{theme}",
            "login",
            _s1_theme(theme),
            "Sign in",
            SIGN_IN,
            app=False,
            theme=theme,
        )
        for theme in THEMES
    ),
    # S3 locations: empty, every status, tags, delivery failing, ops chat unset, themes.
    Case(
        "S3-empty",
        "location-list",
        _s3_empty,
        "Locations",
        SHELL | {"poll"},
        check=_testid("empty-state"),
    ),
    Case(
        "S3-every-status",
        "location-list",
        _s3_every_status,
        "Locations",
        S3_LIVE,
        check=_statuses("on", "off", "waiting", "maintenance"),
    ),
    Case(
        "S3-tags",
        "location-list",
        _s3_tags,
        "Locations",
        S3_LIVE,
        check=_both(_rows(1), _tags),
    ),
    Case(
        "S3-delivery-failing-today-and-earlier",
        "location-list",
        _s3_failing,
        "Locations",
        S3_LIVE,
        check=_both(_rows(2), _all_failing),
    ),
    Case(
        "S3-ops-chat-unset",
        "location-list",
        _s3_ops_unset,
        "Locations",
        S3_LIVE,
        check=_both(_testid("ops-chat-banner"), _testid("ops-chat-warning")),
    ),
    *(
        Case(
            f"S3-theme-{theme}",
            "location-list",
            _s3_theme(theme),
            "Locations",
            S3_LIVE,
            theme=theme,
        )
        for theme in THEMES
    ),
    Case(
        "S3-theme-invalid",
        "location-list",
        _s3_theme("DARK"),
        "Locations",
        S3_LIVE,
        theme="system",
    ),
    Case(
        "S3-after-delete",
        "location-list",
        _s3_after_delete,
        "Locations",
        S3_LIVE,
        check=_both(_rows(1), _flash("success", LOCATION_DELETED_MESSAGE)),
    ),
    # S4 add: GET, invalid POST without and with a typed token.
    Case("S4-get", "location-create", _s4_get, "Add location", FORM),
    Case(
        "S4-invalid",
        "location-create",
        _s4_invalid,
        "Add location",
        FORM_ERRORS,
        check=_error_summary("name", "bot_token"),
    ),
    Case(
        "S4-invalid-typed-token",
        "location-create",
        _s4_invalid_token,
        "Add location",
        FORM_ERRORS,
        check=_error_summary("period_s"),
    ),
    # S6 edit: GET, invalid POST without and with a typed token, after a rename.
    Case("S6-get", "location-edit", _s6_get, "{name} · Edit", FORM, surface="edit"),
    Case(
        "S6-invalid",
        "location-edit",
        _s6_invalid,
        "{name} · Edit",
        FORM_ERRORS,
        surface="edit",
        check=_error_summary("period_s"),
    ),
    Case(
        "S6-invalid-typed-token",
        "location-edit",
        _s6_invalid_token,
        "{name} · Edit",
        FORM_ERRORS,
        surface="edit",
        check=_error_summary("period_s"),
    ),
    Case(
        "S6-after-rename",
        "location-edit",
        _s6_after_rename,
        "{name} · Edit",
        FORM,
        surface="edit",
        check=_renamed,
    ),
    # S5 location page: outages, in progress, both empty states, reset states, chart card.
    Case(
        "S5-outages-listed",
        "location-detail",
        _s5(_office),
        "{name}",
        S5,
        surface="panel",
        check=_both(_has_chart, _testid("reset-history")),
    ),
    Case(
        "S5-in-progress-reset-refused",
        "location-detail",
        _s5(_shop),
        "{name}",
        S5,
        surface="panel",
        check=_both(
            _testid("in-progress-note"),
            _testid("reset-unavailable", **{"data-reason": "in-progress"}),
        ),
    ),
    Case(
        "S5-none-in-14-days",
        "location-detail",
        _s5(_quiet),
        "{name}",
        S5,
        surface="panel",
        check=_testid("outages-empty", **{"data-reason": "none-in-14-days"}),
    ),
    Case(
        "S5-no-history",
        "location-detail",
        _s5(lambda env: env.make("New site")),
        "{name}",
        S5_NO_CHART,
        surface="panel",
        check=_both(
            _no_chart,
            _testid("outages-empty", **{"data-reason": "no-history"}),
            _testid("reset-unavailable", **{"data-reason": "no-history"}),
        ),
    ),
    Case("S5-long-name", "location-detail", _s5_long_name, "{name}", S5, surface="panel"),
    # S5 delivery failing: each cause line and the migrate line.
    *(
        Case(
            f"S5-delivery-failing-{label}",
            "location-detail",
            _s5_failing(status, migrate),
            "{name}",
            S5,
            surface="panel",
            check=_delivery_cause(cause),
        )
        for label, status, migrate, cause in (
            ("http_400", 400, None, DELIVERY_NOT_IN_CHAT_CAUSE),
            ("http_401", 401, None, DELIVERY_BOT_REJECTED_CAUSE),
            ("http_403", 403, None, DELIVERY_CANNOT_POST_CAUSE),
            ("http_409", 409, None, DELIVERY_OTHER_CAUSE),
            (
                "migrate",
                400,
                -1001234567999,
                DELIVERY_MIGRATE_LINE.format(new_chat_id=-1001234567999),
            ),
        )
    ),
    # S5 maintenance on, with power on and with power off.
    Case(
        "S5-maintenance-power-on",
        "location-detail",
        _s5_maintenance(_on),
        "{name}",
        S5,
        surface="panel",
        check=_power("on"),
    ),
    Case(
        "S5-maintenance-power-off",
        "location-detail",
        _s5_maintenance(_off),
        "{name}",
        S5,
        surface="panel",
        check=_power("off"),
    ),
    # S5 after each of the 7 Phase 5 flashes.
    Case(
        "S5-flash-removed",
        "location-detail",
        _removed,
        "{name}",
        S5,
        surface="panel",
        check=_removed_flash,
    ),
    *(
        Case(
            f"S5-flash-{label}",
            "location-detail",
            build,
            "{name}",
            xdata,
            surface="panel",
            check=_flash(level, flash),
        )
        for label, build, xdata, level, flash in (
            ("already-gone", _already_gone, S5, "info", OUTAGE_GONE_MESSAGE),
            ("removal-refused", _removal_refused, S5, "error", REMOVAL_REFUSED_MESSAGE),
            ("removal-deferred", _removal_deferred, S5, "info", REMOVAL_DEFERRED_MESSAGE),
            ("history-reset", _history_reset, S5_NO_CHART, "success", HISTORY_RESET_MESSAGE),
            ("nothing-to-reset", _nothing_to_reset, S5_NO_CHART, "info", NOTHING_TO_RESET_MESSAGE),
            ("reset-refused", _reset_refused, S5, "error", RESET_REFUSED_MESSAGE),
        )
    ),
    # S5 after each of the 12 switch flashes.
    *(
        Case(
            f"S5-flash-{name}-{key.replace('_', '-')}",
            "location-detail",
            _switch(name, key),
            "{name}",
            S5,
            surface="panel",
            check=_switch_flash(name, key),
        )
        for name in SWITCHES
        for key in ("on", "off", "already_on", "already_off")
    ),
    # S5 after each test-message result (9 flashes, the server-error one from A1).
    *(
        Case(
            f"S5-flash-test-{result}",
            "location-detail",
            _test_message(result),
            "{name}",
            S5,
            surface="panel",
            check=_flash(level, flash),
        )
        for result, (_answer, level, flash) in TEST_RESULTS.items()
    ),
    # S5 after an edit save and a channel change (sticky).
    Case(
        "S5-flash-changes-saved",
        "location-detail",
        _saved,
        "{name}",
        S5,
        surface="panel",
        check=_flash("success", CHANGES_SAVED_MESSAGE),
    ),
    Case(
        "S5-flash-channel-changed",
        "location-detail",
        _channel_changed,
        "{name}",
        S5,
        surface="panel",
        check=_flash("success", CHANNEL_CHANGED_MESSAGE),
    ),
    # S7 delete; S9 regenerate with each of its 4 state blocks; S10 with and without
    # consequence 2; S11 reset.
    Case("S7-delete", "location-delete", _s7, "{name} · Delete", SHELL),
    *(
        Case(
            f"S9-{block}",
            "location-regenerate",
            _s9(state),
            "{name} · Regenerate key",
            SHELL,
            check=_testid("state-block", **{"data-state-block": block}),
        )
        for block, state in (
            ("warning-on", lambda env: _on(env.make("Office"))),
            ("power-off", lambda env: _off(env.make("Office"))),
            ("waiting", lambda env: env.make("Office")),
            ("maintenance", _s9_maintenance),
        )
    ),
    Case(
        "S10-without-consequence-2",
        "outage-remove",
        _s10_plain,
        "{name} · Remove outage",
        SHELL,
        check=_consequences(3),
    ),
    Case(
        "S10-with-consequence-2",
        "outage-remove",
        _s10_not_monitored,
        "{name} · Remove outage",
        SHELL,
        check=_consequences(4),
    ),
    Case("S11-reset", "location-reset", _s11, "{name} · Reset history", SHELL),
    # S8 device setup: masked (received and waiting), revealed by Reveal, revealed by
    # Regenerate (both flashes), and after the add form's redirect.
    Case(
        "S8-masked",
        "location-setup",
        _s8_masked(_on),
        "{name} · Device setup",
        S8,
        surface="setup-masked",
        check=_both(_key_state("masked"), _first_heartbeat(received=True)),
    ),
    Case(
        "S8-masked-waiting",
        "location-setup",
        _s8_masked(lambda location: location),
        "{name} · Device setup",
        S8,
        surface="setup-masked",
        check=_both(_key_state("masked"), _first_heartbeat(received=False)),
    ),
    Case(
        "S8-revealed-by-reveal",
        "location-setup",
        _s8_revealed,
        "{name} · Device setup",
        S8_REVEALED,
        surface="setup-revealed",
        check=_key_state("revealed"),
    ),
    Case(
        "S8-revealed-by-regenerate",
        "location-regenerate",
        _s8_regenerated,
        "{name} · Device setup",
        S8_REVEALED,
        surface="setup-revealed",
        trail="location-setup",
        check=_both(_key_state("revealed"), _flash("success", REGENERATED_MESSAGE)),
    ),
    Case(
        "S8-revealed-by-regenerate-again",
        "location-regenerate",
        _s8_already_regenerated,
        "{name} · Device setup",
        S8_REVEALED,
        surface="setup-revealed",
        trail="location-setup",
        check=_both(_key_state("revealed"), _flash("info", ALREADY_REGENERATED_MESSAGE)),
    ),
    Case(
        "S8-after-create",
        "location-setup",
        _s8_after_create,
        "{name} · Device setup",
        S8,
        surface="setup-masked",
        check=_both(_key_state("masked"), _flash("success", LOCATION_CREATED_MESSAGE)),
    ),
    # E1-E3: 404, 403 CSRF, 500 (through the whole middleware stack).
    Case("E1-404", None, _e1, ERROR_TITLES[404], NONE, app=False, status=404, theme="system"),
    Case("E2-403-csrf", None, _e2, ERROR_TITLES[403], NONE, app=False, status=403, theme="system"),
    Case(
        "E3-500",
        None,
        _e3,
        ERROR_TITLES[500],
        NONE,
        app=False,
        status=500,
        theme="system",
        urls="urls_raise",
    ),
]

# The URL names the matrix renders (TEST-STRATEGY §5.6; tests/web/test_routes_coverage.py).
MATRIX_ROUTES = frozenset(case.route for case in CASES if case.route is not None)


def _params() -> list[Any]:
    return [
        pytest.param(case, id=case.id, marks=[pytest.mark.urls(case.urls)] if case.urls else [])
        for case in CASES
    ]


@pytest.fixture
def env(
    client: Client,
    db: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    fake_telegram: FakeTelegram,
    location_factory: Callable[..., Any],
) -> Env:
    """The signed-in admin, the display TZ and base URL, the ops chat on, every clock at NOW."""
    settings.TIME_ZONE = "Europe/Kyiv"
    settings.PUBLIC_BASE_URL = BASE_URL
    settings.CFG = dataclasses.replace(
        settings.CFG, ops_bot_token=OPS_BOT_TOKEN, ops_chat_id=OPS_CHAT_ID
    )
    clock = FakeClock(NOW)
    monkeypatch.setattr(timefmt, "CLOCK", clock)
    monkeypatch.setattr(context_processors, "CLOCK", clock)
    for view in (
        LocationListView,
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
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return Env(admin=client, telegram=fake_telegram, settings=settings, factory=location_factory)


def _render(env: Env, case: Case) -> tuple[HttpResponse, BeautifulSoup]:
    response = case.build(env)
    assert isinstance(response, HttpResponse)
    name = env.current(env.main).name if env.main is not None else ""
    soup = assert_page(
        response, status=case.status, title=case.title.format(name=name), app=case.app
    )
    return response, soup


# The secret scan (TEST-STRATEGY §9)


def _keys_of(env: Env) -> list[str]:
    """Every key the case's locations have had, current first."""
    found: list[str] = []
    for location in env.locations:
        found.append(Location.objects.get(pk=location.pk).device_key)
    return [*found, *env.old_keys]


def _secrets(env: Env) -> list[str]:
    """Everything no page may show outside its allowances, without repeats."""
    values = [*SECRETS, OPS_BOT_TOKEN, OPS_SECRET, MASKED, MASKED_2, MASKED_3]
    for key in _keys_of(env):
        values += [key, keys.mask_key(key)]
        if key.endswith(KEY_TAIL):
            values.append(KEY_TAIL)
    return list(dict.fromkeys(values))


def _allowances(env: Env, surface: str) -> list[tuple[str, str]]:
    """What the surface may show where (TEST-STRATEGY §9), for the case's main location."""
    if surface == "bare":
        return []
    current = env.current(env.main)
    mask = validators.mask_token(current.bot_token)
    if surface == "edit":
        return [(mask, "masked-token")]
    allowed = [(mask, "settings-panel")]
    key = current.device_key
    tail = [KEY_TAIL] if key.endswith(KEY_TAIL) else []
    if surface == "setup-masked":
        allowed += [(keys.mask_key(key), f"#{target}") for target in KEY_TARGETS]
        allowed += [(value, "device-key") for value in tail]
        allowed += [(value, f"#{target}") for value in tail for target in EXAMPLES]
    elif surface == "setup-revealed":
        allowed += [(key, f"#{target}") for target in KEY_TARGETS]
        allowed += [(value, f"#{target}") for value in tail for target in KEY_TARGETS]
    else:
        assert surface == "panel", surface
    return allowed


def _without_csrf(soup: BeautifulSoup) -> str:
    """The page with every CSRF value blanked; each one was non-empty."""
    copy = BeautifulSoup(str(soup), "html.parser")
    for token in copy.find_all("input", attrs={"name": "csrfmiddlewaretoken"}):
        token["value"] = ""
    return str(copy)


def _redirects(response: HttpResponse) -> list[str]:
    return [
        str(response.get("Location", "")),
        *(url for url, _ in getattr(response, "redirect_chain", [])),
    ]


# Hooks the rendered pages share with admin.js (UI-05, UI-07, UI-08, UI-13)


def _x_data(soup: BeautifulSoup) -> set[str]:
    return {str(element["x-data"]) for element in soup.find_all(attrs={"x-data": True})}


def registered_components(source: str | None = None) -> set[str]:
    """The Alpine.data names admin.js (or ``source``) registers, comment lines skipped."""
    if source is None:
        source = ADMIN_JS.read_text(encoding="utf-8")
    lines = source.splitlines()
    code = "\n".join(line for line in lines if not line.strip().startswith(("//", "/*", "*")))
    return {match.group("name") for match in _REGISTRATION.finditer(code)}


def hook_violations(case: Case, soup: BeautifulSoup) -> list[str]:
    """Where the page's live, power, fleet-showing, jump-target and copy hooks break the
    contract admin.js reads them by."""
    found: list[str] = []
    for element in soup.find_all(attrs={"data-live": True}):
        value = str(element["data-live"])
        if value not in LIVE_VALUES:
            found.append(f"data-live {value!r} outside the vocabulary")
        elif (value in LIVE_PER_LOCATION) != element.has_attr("data-location-id"):
            found.append(f"data-live {value!r} with the wrong location id rule")
    for element in soup.find_all(attrs={"data-power": True}):
        if case.route != "location-detail":
            found.append("[data-power] outside S5")
        if str(element["data-power"]) not in POWER_VALUES:
            found.append(f"data-power {element['data-power']!r}")
    for element in soup.find_all(attrs={"data-delivery-variant": True}):
        if str(element["data-delivery-variant"]) not in DELIVERY_VARIANTS:
            found.append(f"data-delivery-variant {element['data-delivery-variant']!r}")
    showing = all_by_testid(soup, "fleet-showing")
    for part in SHOWING_PARTS:
        elements = soup.find_all(attrs={part: True})
        inside = [el for el in elements if el.find_parent(attrs={"data-testid": "fleet-showing"})]
        if len(elements) != (1 if showing else 0) or len(inside) != len(elements):
            found.append(f"[{part}] not exactly once inside fleet-showing")
    for element in showing:
        if (element.get("aria-live"), element.get("aria-atomic")) != ("polite", "true"):
            found.append("fleet-showing is not a polite atomic region")
    if case.route == "location-detail":
        for target in JUMP_TARGETS:
            element = soup.find(id=target)
            if not isinstance(element, Tag) or element.get("tabindex") != "-1":
                found.append(f"#{target} without tabindex -1")
    for button in soup.find_all(attrs={"data-copy-target": True}):
        target = str(button["data-copy-target"])
        if soup.find(id=target) is None:
            found.append(f"copy target #{target} missing")
        if target == "heartbeat-url" and soup.find(id=f"{target}-label") is None:
            found.append(f"copy field #{target}-label missing")
    return found


def _empty_csrf_forms(soup: BeautifulSoup) -> list[str]:
    """POST forms whose CSRF input is missing or empty."""
    empty: list[str] = []
    for form in soup.find_all("form"):
        if str(form.get("method", "")).lower() != "post":
            continue
        tokens = [
            str(found.get("value") or "")
            for found in form.find_all("input", attrs={"name": "csrfmiddlewaretoken"})
        ]
        if not tokens or not all(tokens):
            empty.append(str(form.get("action")))
    return empty


# UI-01: every route x state renders in the new design, with the page invariants


@pytest.mark.django_db
@pytest.mark.parametrize("case", _params())
def test_UI01_render_matrix(env: Env, case: Case) -> None:
    response, soup = _render(env, case)

    # The page is in the state its id names.
    if case.check is not None:
        case.check(env, soup)
    resolved = getattr(response.wsgi_request.resolver_match, "url_name", None)
    if case.route is not None:
        assert resolved == case.route
    # The theme: the cookie's allowlisted value, else system; error pages always system.
    if case.theme is not None:
        assert soup.html is not None and soup.html.get("data-theme") == case.theme
    # The breadcrumb trail of the page it is (the Regenerate POST shows the setup trail).
    if case.app:
        trail = crumbs.trail_for(
            case.trail or case.route, env.current(env.main) if env.main else None
        )
        assert breadcrumbs(soup) == [(crumb.label, crumb.href) for crumb in trail]
    # Exactly the Alpine components this page binds, all registered in admin.js.
    assert _x_data(soup) == set(case.xdata)
    assert set(case.xdata) <= registered_components()
    # The hooks admin.js reads have the shape it reads them by.
    assert hook_violations(case, soup) == []
    # Every POST form carries its CSRF token, never an empty one.
    assert _empty_csrf_forms(soup) == []
    # E1-E3 are context-free: the template alone, with no request (R11).
    if case.status in ERROR_TITLES and not case.app:
        template = {404: "404.html", 403: "403_csrf.html", 500: "500.html"}[case.status]
        assert response.content.decode() == render_to_string(template)
    # The secret scan with the surface's allowances, on the decoded body.
    body = _without_csrf(soup)
    assert_no_secrets(
        body,
        _secrets(env),
        label=case.id,
        allow=_allowances(env, case.surface),
        headers=_redirects(response),
    )
    if case.surface == "bare":
        assert "•" not in body
    if case.surface == "setup-masked":
        tail = env.current(env.main).device_key[-keys.MASK_VISIBLE :]
        assert f"Hidden key ending in {tail}" in text(by_testid(soup, "device-key"))
    # No action called Telegram except the test message.
    if "flash-test" not in case.id:
        assert len(env.telegram.calls) == 0


def test_UI13_matrix_binds_every_component() -> None:
    # Expected: the pages of the matrix bind every registered component between them, and
    # nothing else (each case's set is checked against its render above).
    assert set().union(*(case.xdata for case in CASES)) == registered_components()
    assert len(CASES) >= 30
    # Edge: a registration in a comment is not one. Failure: a name the matrix never binds
    # would leave the union short of the registered set.
    sample = '// Alpine.data("ghost", f);\n * Alpine.data("doc", f)\nwindow.Alpine.data("real", f);'
    assert registered_components(sample) == {"real"}
    assert set().union(*(case.xdata for case in CASES)) != registered_components() | {"ghost"}


@pytest.mark.django_db
def test_UI05_pages_render_the_live_vocabulary(env: Env) -> None:
    office = _on(env.make("Office"))
    _fail(office, _at(15), 403)
    waiting = env.make("New site")
    pages = [
        env.admin.get("/"),
        env.admin.get(_detail(office)),
        env.admin.get(f"/locations/{waiting.pk}/setup/"),
    ]
    seen: set[str] = set()
    for response in pages:
        soup = parse(response)
        seen |= {str(found["data-live"]) for found in soup.find_all(attrs={"data-live": True})}

    # Expected: S3, S5 and S8 render the whole data-live vocabulary between them (06-12's
    # sidebar-sr, sidebar-fail, summary-sr and sidebar-count included), and nothing else.
    assert seen == LIVE_VALUES
    # The poll reads every value by name.
    source = ADMIN_JS.read_text(encoding="utf-8")
    assert [value for value in sorted(LIVE_VALUES) if f'"{value}"' not in source] == []
    # Failure: a page without the sidebar (sign-in) renders none of them.
    assert parse(Client().get("/login/")).find_all(attrs={"data-live": True}) == []


def test_hook_rules_report_each_break() -> None:
    page = parse(
        '<main><p data-live="status"></p><p data-live="nope" data-location-id="1"></p>'
        '<p data-live="count" data-location-id="1"></p><span data-power="maybe"></span>'
        '<span data-delivery-variant="late"></span><button data-copy-target="gone"></button>'
        '<p data-testid="fleet-showing" aria-live="polite"><span data-showing-all></span></p>'
        '<form method="post"><input name="csrfmiddlewaretoken" value=""></form></main>'
    )
    case = Case("sample", "location-list", _s3_empty, "Locations", SHELL)

    assert hook_violations(case, page) == [
        "data-live 'status' with the wrong location id rule",
        "data-live 'nope' outside the vocabulary",
        "data-live 'count' with the wrong location id rule",
        "[data-power] outside S5",
        "data-power 'maybe'",
        "data-delivery-variant 'late'",
        "[data-showing-shown] not exactly once inside fleet-showing",
        "[data-showing-of] not exactly once inside fleet-showing",
        "[data-showing-total] not exactly once inside fleet-showing",
        "[data-showing-noun] not exactly once inside fleet-showing",
        "fleet-showing is not a polite atomic region",
        "copy target #gone missing",
    ]
    assert _empty_csrf_forms(page) == ["None"]
    # Expected: a well-formed page reports nothing.
    good = parse('<form method="post"><input name="csrfmiddlewaretoken" value="x"></form>')
    assert _empty_csrf_forms(good) == []
    assert hook_violations(case, good) == []


# UI-12: the a11y invariants the server can check


def _ids(soup: BeautifulSoup) -> Counter[str]:
    return Counter(str(element["id"]) for element in soup.find_all(id=True))


def _filled_later(element: Tag, target: str) -> bool:
    """The closed confirm dialog names the title its loaded fragment brings (UI-07).

    The shell ships empty and closed, so nothing exposes it before admin.js moves in a
    confirmation's ``h1#confirm-title``; every confirmation page has that id itself.
    """
    return (
        element.name == "dialog"
        and element.get("data-testid") == "confirm-dialog"
        and not element.has_attr("open")
        and target == DIALOG_TITLE
    )


def reference_gaps(soup: BeautifulSoup) -> list[str]:
    """Every id reference on the page that points at no element."""
    ids = _ids(soup)
    gaps: list[str] = []
    for attribute in ID_REFERENCES:
        for element in soup.find_all(attrs={attribute: True}):
            for target in str(element[attribute]).split():
                if target not in ids and not _filled_later(element, target):
                    gaps.append(f"{attribute}={target}")
    for label in soup.find_all("label", attrs={"for": True}):
        if str(label["for"]) not in ids:
            gaps.append(f"for={label['for']}")
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if href.startswith("#") and href[1:] not in ids:
            gaps.append(f"href={href}")
    return gaps


def live_region_violations(soup: BeautifulSoup) -> list[str]:
    """aria-live values outside polite/assertive/off, and an off silencer outside a region."""
    found: list[str] = []
    for element in soup.find_all(attrs={"aria-live": True}):
        value = str(element["aria-live"])
        if value not in LIVE_POLITENESS:
            found.append(f"aria-live={value}")
        elif value == "off":
            region = element.find_parent(attrs={"aria-live": ["polite", "assertive"]})
            if region is None:
                found.append("aria-live=off outside a live region")
    return found


def name_violations(env: Env, soup: BeautifulSoup) -> list[str]:
    """Where a location's name is not whole: S5's h1, the list link, the sidebar link."""
    found: list[str] = []
    for location in env.locations:
        current = Location.objects.get(pk=location.pk)
        if current.deleted_at is not None:
            continue
        name = current.name
        for link in all_by_testid(soup, "sidebar-location"):
            if link.get("data-location-id") == str(current.pk):
                if link.get("title") != name or name not in text(link):
                    found.append(f"sidebar {current.pk}")
        for link in all_by_testid(soup, "location-link"):
            if str(link.get("href")) == _detail(current) and text(link) != name:
                found.append(f"list link {current.pk}")
    return found


@pytest.mark.django_db
@pytest.mark.parametrize("case", _params())
def test_UI12_render_matrix_a11y(env: Env, case: Case) -> None:
    _response, soup = _render(env, case)

    # Every id reference resolves, the error summary's links included. The confirm dialog's
    # title comes with its fragment: every confirmation page has it as its h1.
    assert reference_gaps(soup) == []
    if case.title.endswith(CONFIRMATIONS):
        assert soup.find(id=DIALOG_TITLE) is h1(soup)
    for summary in all_by_testid(soup, "error-summary"):
        links = summary.find_all("a")
        assert links and all(str(link["href"]).startswith("#id_") for link in links)
    # Unique ids (labels and aria-describedby depend on them).
    assert [name for name, count in _ids(soup).items() if count > 1] == []
    # Tables are named and every header cell has a scope.
    for table in soup.find_all("table"):
        assert table.find("caption") is not None or table.has_attr("aria-label")
        assert all(cell.has_attr("scope") for cell in table.find_all("th"))
    # Live regions: polite, assertive, or an off silencer nested in one (S8 step 5).
    assert live_region_violations(soup) == []
    # Every JS-only control ships hidden: a page without JS shows no dead control.
    assert [
        el.name for el in soup.find_all(attrs={"data-js-only": True}) if not el.has_attr("hidden")
    ] == []
    # Every name whole, in the h1 of S5 and in the list and sidebar links and titles.
    assert name_violations(env, soup) == []
    if case.route == "location-detail":
        assert text(h1(soup)) == env.current(env.main).name


@pytest.mark.django_db
def test_UI12_long_name_shows_whole(env: Env) -> None:
    # The 100-character case: the name in full in the S5 h1, the S3 link and the sidebar.
    office = _office(env, LONG_NAME)
    assert len(LONG_NAME) == 100

    detail = parse(env.admin.get(_detail(office)))
    listing = parse(env.admin.get("/"))

    assert text(h1(detail)) == LONG_NAME
    for page in (detail, listing):
        [link] = [
            found
            for found in all_by_testid(page, "sidebar-location")
            if found.get("data-location-id") == str(office.pk)
        ]
        assert link.get("title") == LONG_NAME
        assert LONG_NAME in text(link)
    [row_link] = all_by_testid(by_testid(listing, "locations-table"), "location-link")
    assert text(row_link) == LONG_NAME


def test_a11y_rules_report_each_break() -> None:
    page = parse(
        '<main><label for="id_x">X</label><a href="#nowhere">jump</a>'
        '<button aria-controls="drawer" aria-labelledby="t1 t2">b</button><p id="t1">t</p>'
        '<p aria-live="loud"></p><p aria-live="off">quiet</p>'
        '<div aria-live="polite"><p aria-live="off">silenced</p></div></main>'
    )

    assert reference_gaps(page) == [
        "aria-labelledby=t2",
        "aria-controls=drawer",
        "for=id_x",
        "href=#nowhere",
    ]
    assert live_region_violations(page) == ["aria-live=loud", "aria-live=off outside a live region"]
    assert reference_gaps(parse('<p id="a"></p><a href="#a">a</a>')) == []
    # Edge: the closed confirm dialog may name the title its fragment brings; open, or
    # naming anything else, it may not.
    shell = '<dialog data-testid="confirm-dialog" aria-labelledby="{}"{}></dialog>'
    assert reference_gaps(parse(shell.format(DIALOG_TITLE, ""))) == []
    assert reference_gaps(parse(shell.format(DIALOG_TITLE, " open"))) == [
        f"aria-labelledby={DIALOG_TITLE}"
    ]
    assert reference_gaps(parse(shell.format("other", ""))) == ["aria-labelledby=other"]


CYRILLIC_NAME = ("Житомирщина" * 10)[:100]


@pytest.mark.django_db
def test_UI12_cyrillic_long_name(env: Env) -> None:
    # Expected: 100 Cyrillic letters (200 UTF-8 bytes) pass the 100-character limit, which
    # counts code points, and show whole in the S3 link, the S5 h1 and the sidebar title.
    assert len(CYRILLIC_NAME) == 100
    assert len(CYRILLIC_NAME.encode()) == 200
    created = env.admin.post("/locations/new/", _add_form(name=CYRILLIC_NAME))
    assert created.status_code == 302
    location = Location.objects.get(name=CYRILLIC_NAME)

    listing = assert_page(env.admin.get("/"), title="Locations", app=True)
    detail = assert_page(env.admin.get(_detail(location)), title=CYRILLIC_NAME, app=True)

    [row_link] = all_by_testid(by_testid(listing, "locations-table"), "location-link")
    assert text(row_link) == CYRILLIC_NAME
    assert text(h1(detail)) == CYRILLIC_NAME
    for page in (listing, detail):
        [link] = all_by_testid(page, "sidebar-location")
        assert link.get("title") == CYRILLIC_NAME
        assert CYRILLIC_NAME in text(link)
    # Edge: the name in the edit form's value is whole too.
    edit = parse(env.admin.get(f"/locations/{location.pk}/edit/"))
    assert edit.find(id="id_name").get("value") == CYRILLIC_NAME  # type: ignore[union-attr]
    # Failure: 101 letters are refused with the field error, and nothing is saved.
    refused = env.admin.post("/locations/new/", _add_form(name=CYRILLIC_NAME + "ж"))
    assert refused.status_code == 200
    assert Location.objects.filter(name__startswith=CYRILLIC_NAME).count() == 1
    assert parse(refused).find(id="id_name_error") is not None
