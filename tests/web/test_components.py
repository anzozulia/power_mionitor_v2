"""The shared component partials of the page plans (06-13; UI-01, UI-05, UI-11, UI-12).

Each partial is rendered on its own with ``render_to_string`` and read through
tests/web/pages.py and the 06-UI-SPEC hooks (data-testid, data-* vocabularies, ARIA, tag
names), never through classes or raw markup. "now" comes from a FakeClock instant put into
the context, as a view puts its clock's reading there. Python-owned copy (STATUS_LABELS,
the delivery text, "Never") is imported, never repeated; template-owned copy is the
06-UI-SPEC copy table's.

- partials/_status_pill.html: ``[data-testid=status-pill][data-status]``, the label in
  ``[data-label]`` and all four status icons, hidden from assistive technology, so a live
  update only sets ``data-status`` and the label; live adds ``data-live="status"`` and
  ``data-location-id``.
- partials/_time.html: ``<time datetime>`` with the unchanged ``display_time`` text and
  exactly one ``[data-relative]`` sibling with the same ISO value; "Never" without a time.
- partials/_delivery.html: OK or the failing pill with the Python text in ``[data-label]``;
  live renders both variants and hides the one that does not apply.
- partials/_tag.html and partials/_empty.html: fixed copy, icons, the optional CTA.

Which status icon is visible is decided by CSS from ``data-status``; that is a visual check
(TEST-STRATEGY §11), not a class assertion here.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock
from django.template import TemplateSyntaxError
from django.template.loader import render_to_string
from django.test.html import Element, parse_html
from pages import all_by_testid, assert_no_injected_script, by_testid, parse, text

from powermon.alerts.delivery import Failing
from powermon.web.live import DELIVERY_OK
from powermon.web.status import STATUS_LABELS
from powermon.web.templatetags.display_time import NEVER, display_time, display_time_compact
from powermon.web.templatetags.icons import ICONS
from powermon.web.templatetags.timefmt import relative_text
from powermon.web.views import delivery_text

PILL = "partials/_status_pill.html"
TIME = "partials/_time.html"
DELIVERY = "partials/_delivery.html"
TAG = "partials/_tag.html"
EMPTY = "partials/_empty.html"

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
