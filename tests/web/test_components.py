"""The shared component partials of the page plans (06-13; UI-01, UI-05, UI-07, UI-08,
UI-10, UI-11, UI-12, R3, R16).

Each partial is rendered on its own with ``render_to_string`` and read through
tests/web/pages.py and the 06-UI-SPEC hooks (data-testid, data-* vocabularies, ARIA, tag
names), never through classes or raw markup. "now" comes from a FakeClock instant put into
the context, as a view puts its clock's reading there. Python-owned copy (STATUS_LABELS,
the delivery text, "Never", the SwitchRow fields, the language labels) is imported, never
repeated; template-owned copy is the 06-UI-SPEC copy table's. Partials with a CSRF form
render with a RequestFactory request of a signed-in (unsaved) user; locations are unsaved
model instances, so no test here touches the database.

- partials/_status_pill.html: ``[data-testid=status-pill][data-status]``, the label in
  ``[data-label]`` and all four status icons, hidden from assistive technology, so a live
  update only sets ``data-status`` and the label; live adds ``data-live="status"`` and
  ``data-location-id``.
- partials/_time.html: ``<time datetime>`` with the unchanged ``display_time`` text and
  exactly one ``[data-relative]`` sibling with the same ISO value; "Never" without a time.
- partials/_delivery.html: OK or the failing pill with the Python text in ``[data-label]``;
  live renders both variants and hides the one that does not apply.
- partials/_tag.html and partials/_empty.html: fixed copy, icons, the optional CTA.
- partials/_switch.html: one POST form per switch posting the target value (R16), with
  ``role=switch``, ``aria-checked`` = the current state and the Python action as its name.
- partials/_settings_dl.html: the settings rows with units; the bot token only as its mask
  (R3).
- partials/_copy_field.html: the JS-only copy button and the exact value with ``<wbr>``
  break points that add no text (UI-08).
- partials/_confirm_dialog.html and partials/_menu.html: the dialog shell the confirmation
  fragments load into, and the kebab's plain links (UI-07, R7).

Which status icon is visible is decided by CSS from ``data-status``; that is a visual check
(TEST-STRATEGY §11), not a class assertion here.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock
from django.contrib.auth.models import User
from django.template import TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test.html import Element, parse_html
from django.urls import NoReverseMatch, reverse
from pages import (
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    by_testid,
    code_block,
    definitions,
    form_values,
    hidden_value,
    parse,
    post_form,
    text,
)
from secret_fixtures import MASKED, SECRET, SECRETS, TOKEN

from powermon.alerts.delivery import Failing
from powermon.engine.rules import ROUTER_GRACE
from powermon.locations import examples
from powermon.locations.models import CHART_REFRESH_CHOICES, LANGUAGE_CHOICES, Location
from powermon.locations.validators import mask_token
from powermon.web.live import DELIVERY_OK
from powermon.web.location_views import SwitchRow, settings_context, switch_rows
from powermon.web.status import STATUS_LABELS, LocationStatus
from powermon.web.templatetags.display_time import NEVER, display_time, display_time_compact
from powermon.web.templatetags.icons import ICONS
from powermon.web.templatetags.timefmt import relative_text
from powermon.web.views import delivery_text

PILL = "partials/_status_pill.html"
TIME = "partials/_time.html"
DELIVERY = "partials/_delivery.html"
TAG = "partials/_tag.html"
EMPTY = "partials/_empty.html"
SWITCH = "partials/_switch.html"
SETTINGS = "partials/_settings_dl.html"
COPY_FIELD = "partials/_copy_field.html"
DIALOG = "partials/_confirm_dialog.html"
MENU = "partials/_menu.html"

# The view's clock: 2026-10-01 08:00 UTC (11:00 EEST in Europe/Kyiv).
CLOCK = FakeClock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
NOW = CLOCK.now()
# 5 min 59 s before now: the relative text floors to "5 min ago".
SEEN = NOW - timedelta(minutes=5, seconds=59)

STATUSES = ("on", "off", "maintenance", "waiting")
# 06-UI-SPEC Iconography fixed meanings, in the order the pill renders them.
STATUS_ICONS = ("zap", "zap-off", "wrench", "hourglass")
# Copy rows list.ok and list.tags (06-UI-SPEC copy table).
LIST_OK = "OK"
TAGS = {"alerts-off": ("Alerts off", "bell-off"), "router-grace": ("Router grace", "router")}
HOSTILE = '<script>alert(1)</script>" onmouseover="x'

# The three switch routes and their data-switch values (06-UI-SPEC data-switch vocabulary).
SWITCH_NAMES = {
    "location-maintenance": "maintenance",
    "location-alerts": "alerts",
    "location-router-grace": "router-grace",
}
LANGUAGE_LABELS = dict(LANGUAGE_CHOICES)
# The router-reconnect grace extension, in whole seconds.
ROUTER_GRACE_S = int(ROUTER_GRACE.total_seconds())
HEARTBEAT_URL = examples.heartbeat_url("https://power.example.com")
# Copy rows shell.copy, shell.copy_sr, shell.copied_msg, loc.url_label, shell.more_actions,
# loc.menu, shell.modal_close and shell.modal_loading (06-UI-SPEC copy table).
COPY_NAME = "Copy heartbeat URL"
COPIED_MSG = "Heartbeat URL copied."
URL_LABEL = "Heartbeat URL"
MORE_ACTIONS = "More actions"
MENU_ITEMS = {
    "menu-device-setup": "Device setup",
    "menu-reset-history": "Reset history…",
    "menu-delete-location": "Delete location…",
}
CLOSE = "Close"
LOADING = "Loading…"


def render(template: str, **context: Any) -> Tag:
    """The partial rendered with ``context``, parsed."""
    return parse(render_to_string(template, context))


def _shapes(markup: str) -> list[Element | str]:
    """An icon's inner markup as Django's HTML tree (attribute order and syntax ignored)."""
    wrapper = parse_html(f"<g>{markup}</g>")
    assert isinstance(wrapper, Element)
    return wrapper.children


ICON_SHAPES = {name: _shapes(markup) for name, markup in ICONS.items()}


def icon_names(element: Tag) -> list[str]:
    """The vendored icon each inline svg inside ``element`` draws, in document order.

    Every icon must be hidden from assistive technology (aria-hidden, not focusable).
    """
    names: list[str] = []
    for svg in element.find_all("svg"):
        assert isinstance(svg, Tag)
        assert (svg.get("aria-hidden"), svg.get("focusable")) == ("true", "false")
        drawn = _shapes(svg.decode_contents())
        found = [name for name, shapes in ICON_SHAPES.items() if shapes == drawn]
        assert len(found) == 1, found
        names.append(found[0])
    return names


def only(element: Tag, selector: str) -> Tag:
    """The one element inside ``element`` matching an attribute ``selector``."""
    found = element.select(selector)
    assert len(found) == 1, f"{selector}: expected exactly one element, found {len(found)}"
    return found[0]


def shown_text(element: Tag) -> str:
    """``text()`` of what is displayed: every element with the hidden attribute left out."""
    copy = parse(str(element))
    for hidden in copy.find_all(attrs={"hidden": True}):
        if isinstance(hidden, Tag):
            hidden.decompose()
    return text(copy)


# Status pill (UI-01, UI-05, UI-12)


@pytest.mark.parametrize("status", STATUSES)
def test_status_pill_per_status(status: str) -> None:
    pill = by_testid(render(PILL, status=status, label=STATUS_LABELS[status]), "status-pill")

    assert pill.get("data-status") == status
    assert text(only(pill, "[data-label]")) == STATUS_LABELS[status]
    # The icons are hidden from assistive technology: the label is the pill's whole text.
    assert text(pill) == STATUS_LABELS[status]
    # All four status icons are always there, so data-status alone picks the visible one
    # and a live update never adds or removes an icon (06-11 Live update targets).
    assert icon_names(pill) == list(STATUS_ICONS)
    # Not live: no poll hooks.
    assert not pill.has_attr("data-live")
    assert not pill.has_attr("data-location-id")


def test_status_pill_live_hooks() -> None:
    page = render(PILL, status="off", label=STATUS_LABELS["off"], live=True, location_id=7)
    pill = by_testid(page, "status-pill")

    assert (pill.get("data-live"), pill.get("data-location-id")) == ("status", "7")
    assert (pill.get("data-status"), text(pill)) == ("off", STATUS_LABELS["off"])


def test_status_pill_escapes_its_values() -> None:
    html = render_to_string(PILL, {"status": HOSTILE, "label": HOSTILE})
    pill = by_testid(parse(html), "status-pill")

    # Failure input stays data (R1): one attribute value and one text node, no handler.
    assert_no_injected_script(html, "status pill")
    assert pill.get("data-status") == HOSTILE
    assert text(only(pill, "[data-label]")) == HOSTILE


# Absolute time with its relative time (UI-11, UI-05)


def _the_time(element: Tag) -> Tag:
    times = element.find_all("time")
    assert len(times) == 1, f"expected exactly one <time>, found {len(times)}"
    found = times[0]
    assert isinstance(found, Tag)
    return found


def _relative(element: Tag) -> Tag:
    return only(element, "[data-relative]")


def test_time_partial_full() -> None:
    wrapper = by_testid(render(TIME, value=SEEN, variant="full", testid="t", now=NOW), "t")
    time = _the_time(wrapper)
    relative = _relative(wrapper)

    # The datetime parses back to the same instant; the relative sibling repeats it.
    assert datetime.fromisoformat(str(time["datetime"])) == SEEN
    assert relative.get("data-relative") == time["datetime"]
    # The unchanged absolute text, and the relative text floored at the context's now.
    assert text(only(time, '[data-part="full"]')) == display_time(SEEN)
    assert text(relative) == relative_text(SEEN, NOW) == "5 min ago"
    assert text(wrapper) == f"{display_time(SEEN)} (5 min ago)"
    assert not wrapper.has_attr("data-live")


def test_time_partial_table_and_card() -> None:
    table = by_testid(render(TIME, value=SEEN, variant="table", testid="t", now=NOW), "t")
    card = by_testid(render(TIME, value=SEEN, variant="card", testid="t", now=NOW), "t")

    # Table: the compact text plus the tail is the unchanged full text.
    time = _the_time(table)
    compact = text(only(time, '[data-part="compact"]'))
    tail = only(time, '[data-part="tail"]').get_text()
    assert compact == display_time_compact(SEEN)
    assert compact + tail == text(time) == display_time(SEEN)
    assert _relative(table).get("data-relative") == time["datetime"]
    assert text(_relative(table)) == "5 min ago"
    # Card: the relative text first, then the absolute one.
    assert text(card) == f"5 min ago · {display_time(SEEN)}"
    assert _relative(card).get("data-relative") == _the_time(card)["datetime"]


def test_time_partial_live_and_label() -> None:
    since = by_testid(
        render(
            TIME,
            value=SEEN,
            variant="full",
            label="On since",
            live="since",
            location_id=3,
            testid="since",
            now=NOW,
        ),
        "since",
    )
    heartbeat = by_testid(
        render(
            TIME,
            value=SEEN,
            variant="table",
            live="last-heartbeat",
            location_id=3,
            testid="last-heartbeat",
            now=NOW,
        ),
        "last-heartbeat",
    )

    assert (since.get("data-live"), since.get("data-location-id")) == ("since", "3")
    assert text(only(since, "[data-since-label]")) == "On since"
    assert text(since) == f"On since {display_time(SEEN)} (5 min ago)"
    assert (heartbeat.get("data-live"), heartbeat.get("data-location-id")) == (
        "last-heartbeat",
        "3",
    )
    # A label without live="since" is plain text, not the poll's since label.
    plain = by_testid(render(TIME, value=None, label="Last heartbeat", testid="t"), "t")
    assert plain.select("[data-since-label]") == []
    assert text(plain) == f"Last heartbeat {NEVER}"


def test_time_partial_dst_fall_back_hour() -> None:
    # 03:30 happens twice in Kyiv on 2026-10-25: EEST (+03:00), then EET (+02:00).
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    later = datetime(2026, 10, 25, 2, 30, tzinfo=UTC)
    rendered = [
        by_testid(render(TIME, value=value, variant="full", testid="t", now=later), "t")
        for value in (first, second)
    ]

    stamps = [str(_the_time(wrapper)["datetime"]) for wrapper in rendered]
    assert stamps == ["2026-10-25T03:30:00+03:00", "2026-10-25T03:30:00+02:00"]
    assert [datetime.fromisoformat(stamp) for stamp in stamps] == [first, second]
    assert [text(wrapper) for wrapper in rendered] == [
        "2026-10-25 03:30:00 EEST (2 h ago)",
        "2026-10-25 03:30:00 EET (1 h ago)",
    ]


@pytest.mark.parametrize("variant", ["full", "table", "card"])
def test_time_partial_never_and_naive(variant: str) -> None:
    # The one deliberate naive datetime: it is the bad input under test.
    wall = datetime(2026, 10, 1, 11, 0)  # noqa: DTZ001
    never = by_testid(render(TIME, value=None, variant=variant, testid="t", now=NOW), "t")
    naive = by_testid(render(TIME, value=wall, variant=variant, testid="t", now=NOW), "t")

    # Edge: no heartbeat yet reads "Never", with no time and no relative time.
    assert text(never) == NEVER
    assert never.find_all("time") == []
    assert never.select("[data-relative]") == []
    # Failure: a naive datetime is no instant, so it renders no time and no text.
    assert naive.find_all("time") == []
    assert naive.select("[data-relative]") == []
    assert text(naive) == ""


# Delivery (UI-01, UI-05)


def _failing_text() -> str:
    failing = Failing(
        started_at=NOW - timedelta(hours=1, minutes=18), http_status=403, migrate_to_chat_id=None
    )
    found = delivery_text(failing, NOW)
    assert found is not None
    return found


def test_delivery_partial() -> None:
    failing_text = _failing_text()
    failing = by_testid(render(DELIVERY, failing=True, text=failing_text), "delivery")
    ok = by_testid(render(DELIVERY, failing=False), "delivery")

    # Failing: the orange pill with the Python text in [data-label] and the warning icon.
    assert failing.get("data-delivery") == "failing"
    assert text(only(failing, "[data-label]")) == failing_text
    assert text(failing) == failing_text
    assert icon_names(failing) == ["triangle-alert"]
    # OK: the list.ok text with its check icon, no pill label.
    assert ok.get("data-delivery") == "ok"
    assert text(ok) == LIST_OK == DELIVERY_OK
    assert icon_names(ok) == ["check"]
    assert ok.select("[data-label]") == []
    # Without live only the variant that applies is rendered, and no poll hooks.
    for element in (failing, ok):
        assert element.select("[hidden]") == []
        assert not element.has_attr("data-live")


@pytest.mark.parametrize("failing", [True, False])
def test_delivery_partial_live_renders_both_variants(failing: bool) -> None:
    failing_text = _failing_text() if failing else None
    cell = by_testid(
        render(DELIVERY, failing=failing, text=failing_text, live=True, location_id=9),
        "delivery",
    )
    variants = {
        str(found["data-delivery-variant"]): found
        for found in cell.select("[data-delivery-variant]")
    }

    assert (cell.get("data-live"), cell.get("data-location-id")) == ("delivery", "9")
    assert cell.get("data-delivery") == ("failing" if failing else "ok")
    # Both variants are in the DOM; exactly the one that does not apply is hidden.
    assert sorted(variants) == ["failing", "ok"]
    assert variants["ok"].has_attr("hidden") is failing
    assert variants["failing"].has_attr("hidden") is not failing
    assert shown_text(cell) == (failing_text if failing else LIST_OK)
    # Edge: while OK the hidden pill's label is empty, never the text "None".
    if not failing:
        assert text(only(cell, "[data-label]")) == ""


def test_delivery_partial_escapes_its_text() -> None:
    html = render_to_string(DELIVERY, {"failing": True, "text": HOSTILE})

    assert_no_injected_script(html, "delivery")
    assert text(only(by_testid(parse(html), "delivery"), "[data-label]")) == HOSTILE


# Tags (UI-01)


@pytest.mark.parametrize("tag", sorted(TAGS))
def test_tag_partial(tag: str) -> None:
    label, icon = TAGS[tag]
    element = by_testid(render(TAG, tag=tag), "tag")

    assert element.get("data-tag") == tag
    assert text(element) == label
    assert icon_names(element) == [icon]


@pytest.mark.parametrize("tag", ["", "maintenance", HOSTILE])
def test_tag_partial_unknown_renders_nothing(tag: str) -> None:
    html = render_to_string(TAG, {"tag": tag})

    # Failure: only the two fixed tags exist.
    assert all_by_testid(parse(html), "tag") == []
    assert html.strip() == ""


# Empty state (UI-01, UI-12)


def test_empty_partial() -> None:
    page = render(
        EMPTY,
        icon="map-pin",
        title="No locations yet",
        body="Add a location to get its heartbeat URL, device key and setup examples.",
        testid="empty-state",
        accent=True,
        cta_label="Add location",
        cta_href="/locations/new/",
        cta_testid="add-location",
        cta_icon="plus",
    )
    empty = by_testid(page, "empty-state")
    cta = by_testid(empty, "add-location")

    # The circle is decorative (its icon and the CTA's are hidden from assistive technology);
    # the title, the body and the CTA carry the text.
    assert icon_names(empty) == ["map-pin", "plus"]
    assert [text(paragraph) for paragraph in empty.find_all("p")] == [
        "No locations yet",
        "Add a location to get its heartbeat URL, device key and setup examples.",
    ]
    assert (cta.name, cta.get("href"), cta.get("data-variant")) == (
        "a",
        "/locations/new/",
        "primary",
    )
    assert text(cta) == "Add location"
    # It stands alone: no table, list or count.
    assert empty.find_all(["table", "ul", "ol", "dl"]) == []


def test_empty_partial_without_cta() -> None:
    empty = by_testid(
        render(EMPTY, icon="calendar-days", title="No chart yet", body="Later.", testid="e"), "e"
    )

    # Edge: no CTA, so no link and no button; the neutral circle draws the icon.
    assert empty.find_all(["a", "button"]) == []
    assert icon_names(empty) == ["calendar-days"]
    assert [text(paragraph) for paragraph in empty.find_all("p")] == ["No chart yet", "Later."]


def test_empty_partial_escapes_and_needs_an_icon() -> None:
    context = {"icon": "info", "title": HOSTILE, "body": HOSTILE, "testid": "e"}
    html = render_to_string(EMPTY, context)

    assert_no_injected_script(html, "empty state")
    assert [text(p) for p in by_testid(parse(html), "e").find_all("p")] == [HOSTILE, HOSTILE]
    # Failure: an empty state without an icon name is a template error, never a blank circle.
    with pytest.raises(TemplateSyntaxError):
        render_to_string(EMPTY, {"title": "No chart yet", "testid": "e"})


# Shared helpers of the action components


def _location(**fields: Any) -> Location:
    """An unsaved location (pk 42) with the fixture bot token; nothing is saved."""
    values: dict[str, Any] = {
        "pk": 42,
        "name": "Kyiv office",
        "period_s": 60,
        "grace_s": 30,
        "router_grace": False,
        "maintenance": False,
        "alerts_enabled": True,
        "language": "uk",
        "bot_token": TOKEN,
        "chat_id": -1001234567890,
        "device_key": "k" * 32,
        "created_at": NOW,
        **fields,
    }
    return Location(**values)


def _signed_in_request() -> Any:
    """A GET of the location page by a signed-in admin (an unsaved user), for the CSRF token."""
    request = RequestFactory().get("/locations/42/")
    request.user = User(username="admin")
    return request


# Switch (UI-10, R2, R16)


def _switch_html(row: SwitchRow, location: Location) -> str:
    context = {"row": row, "location": location}
    return render_to_string(SWITCH, context, request=_signed_in_request())


@pytest.mark.parametrize("on", [False, True], ids=["all-off", "all-on"])
def test_UI10_switch_partial(on: bool) -> None:
    location = _location(maintenance=on, alerts_enabled=on, router_grace=on)
    current = "on" if on else "off"

    for row in switch_rows(location):
        page = parse(_switch_html(row, location))
        form = by_testid(page, "switch")
        button = only(form, 'button[role="switch"]')
        described = page.find_all(id=button.get("aria-describedby"))

        # Its own switch route, a POST with the CSRF token (R2).
        assert post_form(page, reverse(row.url_name, args=[location.pk])) is form
        assert hidden_value(form, "csrfmiddlewaretoken")
        assert form.get("data-switch") == SWITCH_NAMES[row.url_name]
        assert form.get("data-state") == current
        # R16: it posts the target state, the opposite of the current one, never a toggle,
        # so a stale form can only repeat the change it shows.
        assert row.target != current
        assert form_values(page, "switch") == {"value": row.target}
        # role=switch with aria-checked = the current state; the Python action is its name.
        assert button.get("type") == "submit"
        assert button.get("aria-checked") == ("true" if on else "false")
        assert text(button) == row.button
        assert text(by_testid(form, "switch-state")) == row.heading
        # aria-describedby resolves to the one help element inside the form.
        assert len(described) == 1
        assert text(described[0]) == row.help
        assert described[0].find_parent("form") is form
        # The knob's pending spinner is the only icon, hidden from assistive technology.
        assert icon_names(button) == ["loader-circle"]


def test_UI10_three_switches_on_one_page() -> None:
    location = _location(router_grace=True)
    page = parse("".join(_switch_html(row, location) for row in switch_rows(location)))
    forms = all_by_testid(page, "switch")
    ids = [str(element["id"]) for element in page.find_all(id=True)]

    # Maintenance, Alerts, Router grace, each with its own current state.
    assert [form.get("data-switch") for form in forms] == list(SWITCH_NAMES.values())
    assert [form.get("data-state") for form in forms] == ["off", "on", "on"]
    # Edge: three switches on one page keep their ids unique and their help their own.
    assert len(ids) == len(set(ids)) == 3
    for form in forms:
        button = only(form, 'button[role="switch"]')
        assert only(form, f'[id="{button["aria-describedby"]}"]').find_parent("form") is form


def test_UI10_switch_needs_a_known_route() -> None:
    row = SwitchRow(url_name="location-nowhere", heading="h", help="x", button="b", target="on")

    # Failure: a row without a switch route is a template error, never a form without action.
    with pytest.raises(NoReverseMatch):
        _switch_html(row, _location())


# Settings list (R3)


def _settings_html(location: Location) -> str:
    return render_to_string(SETTINGS, {**settings_context(location), "location": location})


def test_settings_partial() -> None:
    html = _settings_html(_location())
    token = by_testid(html, "masked-token")
    mask = only(token, "code")

    assert definitions(html, "settings-panel") == [
        ("Language", LANGUAGE_LABELS["uk"]),
        ("Chart update period", "15 min"),
        ("Heartbeat period", "60 s"),
        ("Grace period", "30 s"),
        ("Reported OFF after", "90 s without a heartbeat"),
        ("Channel chat ID", "-1001234567890"),
        ("Bot token", "987654321, the rest is hidden"),
    ]
    # The mask is shown but hidden from assistive technology, which reads the sr sentence.
    assert mask.get("aria-hidden") == "true"
    assert mask.get_text() == MASKED == mask_token(TOKEN)
    assert icon_names(token) == ["lock"]
    # R3: neither the token nor its secret part is anywhere in the markup.
    assert_no_secrets(html, SECRETS, label="settings panel")


@pytest.mark.parametrize(("period", "grace"), [(60, 30), (10, 10), (3600, 3600)])
def test_settings_partial_router_grace(period: int, grace: int) -> None:
    total = period + grace
    rows = {
        grace_on: dict(
            definitions(
                _settings_html(_location(period_s=period, grace_s=grace, router_grace=grace_on)),
                "settings-panel",
            )
        )
        for grace_on in (False, True)
    }

    assert rows[False]["Heartbeat period"] == f"{period} s"
    assert rows[False]["Grace period"] == f"{grace} s"
    # Router grace off: no extension; on: the 180 s extension right after power returns.
    assert rows[False]["Reported OFF after"] == f"{total} s without a heartbeat"
    assert rows[True]["Reported OFF after"] == (
        f"{total} s without a heartbeat "
        f"({total + ROUTER_GRACE_S} s right after power returns, router grace on)"
    )


@pytest.mark.parametrize("language", sorted(LANGUAGE_LABELS))
def test_settings_partial_language_and_long_chat_id(language: str) -> None:
    rows = dict(
        definitions(
            _settings_html(_location(language=language, chat_id=-1009999999999999)),
            "settings-panel",
        )
    )

    assert rows["Language"] == LANGUAGE_LABELS[language]
    assert rows["Channel chat ID"] == "-1009999999999999"


@pytest.mark.parametrize(("minutes", "label"), CHART_REFRESH_CHOICES)
def test_settings_partial_chart_update_period(minutes: int, label: str) -> None:
    # CHRT-02 (261006-of9): the option label, right after Language; the token row stays last.
    rows = definitions(_settings_html(_location(chart_refresh_min=minutes)), "settings-panel")

    assert rows[1] == ("Chart update period", label)
    assert rows[0][0] == "Language"
    assert rows[-1][0] == "Bot token"


def test_settings_partial_never_shows_a_colon_less_token() -> None:
    # Failure input: a token without a colon has no public part, so all of it is secret.
    html = _settings_html(_location(bot_token=SECRET))

    assert only(by_testid(html, "masked-token"), "code").get_text() == mask_token(SECRET)
    assert_no_secrets(html, [SECRET], label="settings panel, colon-less token")


# Copy field (UI-08)


def _copy_field(value: str) -> Tag:
    return render(
        COPY_FIELD,
        label=URL_LABEL,
        value=value,
        element_id="heartbeat-url",
        copy_sr="heartbeat URL",
        copied_msg=COPIED_MSG,
    )


def _break_parts(code: Tag) -> list[str]:
    """The text between the ``<wbr>`` break points of ``code``; each one is empty."""
    parts: list[str] = []
    current = ""
    for child in code.children:
        if isinstance(child, Tag):
            assert (child.name, child.contents) == ("wbr", [])
            parts.append(current)
            current = ""
        else:
            current += str(child)
    return [*parts, current]


def test_UI08_copy_field_partial() -> None:
    page = _copy_field(HEARTBEAT_URL)
    button = by_testid(page, "copy")
    code = only(page, "#heartbeat-url")

    # JS only: rendered hidden, revealed by the copy component it binds (06-11).
    assert (button.name, button.get("type")) == ("button", "button")
    assert button.has_attr("data-js-only")
    assert button.has_attr("hidden")
    assert button.get("x-data") == "copy"
    assert button.get("data-copy-target") == code.get("id") == "heartbeat-url"
    assert button.get("data-copied-msg") == COPIED_MSG
    # Visible "Copy" plus the sr suffix: a unique accessible name per copy target.
    assert text(button) == COPY_NAME
    assert icon_names(button) == ["copy", "check"]
    # The button never holds the value; the code element's text is the value exactly.
    assert HEARTBEAT_URL not in str(button)
    assert code_block(page, "heartbeat-url") == HEARTBEAT_URL
    # Break points after the scheme, after the host and before /hb add no text.
    assert _break_parts(code) == ["https://", "power.example.com", "/hb"]
    assert text(only(page, "#heartbeat-url-label")) == URL_LABEL


@pytest.mark.parametrize(
    ("base", "parts"),
    [
        ("https://example.com/pm/", ["https://", "example.com", "/pm", "/hb"]),
        ("http://192.168.1.10:8000", ["http://", "192.168.1.10:8000", "/hb"]),
    ],
    ids=["base-path", "http-port"],
)
def test_UI08_copy_field_break_points(base: str, parts: list[str]) -> None:
    value = examples.heartbeat_url(base)
    page = _copy_field(value)

    # Edge: a base path and a port keep the exact value and break at each path segment.
    assert code_block(page, "heartbeat-url") == value
    assert _break_parts(only(page, "#heartbeat-url")) == parts


def test_UI08_copy_field_copies_any_value_exactly() -> None:
    page = _copy_field(HOSTILE)

    # Failure input: not a URL, so no break point; markup in it stays text (R1).
    assert code_block(page, "heartbeat-url") == HOSTILE
    assert only(page, "#heartbeat-url").find_all(True) == []
    assert_no_injected_script(str(page), "copy field")


# Dialog shell and kebab menu (UI-07, D6-05, R7)


def test_UI07_dialog_shell_and_menu() -> None:
    dialog_page = render(DIALOG)
    dialog = by_testid(dialog_page, "confirm-dialog")
    close = only(dialog, "[data-dialog-close]")
    loading = only(dialog, "[data-dialog-loading]")
    body = only(dialog, "[data-dialog-body]")

    # Exactly one closed native dialog, named by the fragment's h1#confirm-title.
    assert dialog_page.find_all("dialog") == [dialog]
    assert dialog.get("aria-labelledby") == "confirm-title"
    assert not dialog.has_attr("open")
    assert (close.name, close.get("type"), text(close)) == ("button", "button", CLOSE)
    assert icon_names(close) == ["x"]
    # The loading region: a spinner (hidden from assistive technology) and its sr text.
    assert text(loading) == LOADING
    assert icon_names(loading) == ["loader-circle"]
    # The body is empty until the confirmation fragment is moved in.
    assert body.contents == []
    # Data hooks only: no Alpine directive and no form in the shell (06-11).
    for element in [dialog, *dialog.find_all(True)]:
        assert [name for name in element.attrs if name.startswith(("x-", "@", ":"))] == []
    assert dialog.find_all("form") == []

    location = _location()
    page = render(MENU, location=location, has_history=True, outage_in_progress=False)
    button = only(page, "button[popovertarget]")
    popover = only(page, "[popover]")
    links = popover.find_all("a")
    by_name = {str(link.get("data-testid")): link for link in links}

    # The kebab opens its popover; it is named "More actions".
    assert button.get("type") == "button"
    assert button.get("popovertarget") == popover.get("id") == "location-menu"
    assert text(button) == MORE_ACTIONS
    assert icon_names(button) == ["ellipsis-vertical"]
    # Only plain links, in the fixed order; nothing in the menu acts (R7).
    assert [text(link) for link in links] == list(MENU_ITEMS.values())
    assert list(by_name) == list(MENU_ITEMS)
    assert popover.find_all(["form", "button", "input"]) == []
    assert icon_names(popover) == ["cpu", "rotate-ccw", "trash-2"]
    assert by_name["menu-device-setup"].get("href") == reverse("location-setup", args=[42])
    assert not by_name["menu-device-setup"].has_attr("data-confirm")
    # Reset is possible: the confirmation entry point, like delete.
    reset = by_name["menu-reset-history"]
    assert reset.get("href") == reverse("location-reset", args=[42])
    assert reset.has_attr("data-confirm")
    assert not reset.has_attr("data-reason")
    delete = by_name["menu-delete-location"]
    assert delete.get("href") == reverse("location-delete", args=[42])
    assert delete.has_attr("data-confirm")


# The stored power state is off under maintenance: the reset view refuses it all the same.
OFF_UNDER_MAINTENANCE = LocationStatus(
    key="maintenance",
    label=STATUS_LABELS["maintenance"],
    power_key="off",
    power_label=STATUS_LABELS["off"],
    last_heartbeat_at=NOW,
    on_since=None,
    outage_started_at=NOW,
)


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        ({"has_history": True, "outage_in_progress": True}, "in-progress"),
        (
            {"has_history": True, "outage_in_progress": False, "status": OFF_UNDER_MAINTENANCE},
            "in-progress",
        ),
        ({"has_history": False, "outage_in_progress": False}, "no-history"),
    ],
    ids=["outage-listed", "power-off", "no-history"],
)
def test_UI07_menu_reset_unavailable(context: dict[str, Any], reason: str) -> None:
    page = render(MENU, location=_location(), **context)
    reset = by_testid(page, "menu-reset-history")

    # Unavailable: a plain link to the danger-zone row, described by its refusal line;
    # never a dead control and never the confirmation entry point.
    assert (reset.name, reset.get("href")) == ("a", "#reset-history")
    assert not reset.has_attr("data-confirm")
    assert reset.get("data-reason") == reason
    assert reset.get("aria-describedby") == "reset-unavailable"
    assert text(reset) == MENU_ITEMS["menu-reset-history"]
    # Delete stays available in every state.
    assert by_testid(page, "menu-delete-location").has_attr("data-confirm")
