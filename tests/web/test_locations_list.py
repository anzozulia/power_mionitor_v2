"""The Locations page S3: Fleet health, its filter cells and the phone cards (06-UI-SPEC Page
Contracts > S3; UI-01, UI-04, UI-05, UI-12, D6-04, polish N13).

- With at least one location the page shows the Fleet health card ``fleet-summary``: the
  description ``fleet-showing`` ("Showing all {M} locations", "Showing 1 location") with
  ``data-total`` = M, the cell "All" (``fleet-total``) and one ``fleet-tile`` per
  ``data-metric`` on, off, maintenance, waiting and failing. Each cell's value
  ``fleet-count`` equals its ``data-count``. The counts are ``live.fleet_counts`` of the
  page's rows, so they equal the table: maintenance counts as maintenance and never as on
  or off, and a location that is Off with failing delivery counts in off and in failing. A
  zero renders "0" with ``data-count="0"``. The status JSON's counts are the same numbers.
- The filter (N13) is client-side only: six overlay buttons ``filter-chip`` with
  ``data-filter`` all, on, off, maintenance, waiting and failing, ``aria-pressed`` (All
  pressed), named by ``aria-labelledby`` (the cell's label and value), rendered hidden and
  ``data-js-only``; the proportional bar ``fleet-bar`` is aria-hidden and JS only, with the
  segments on, off, maintenance and waiting and their ``data-count``. The page has no GET
  form and no link carries a filter. The card and the table sit inside ``[data-fleet]``
  bound to ``fleetFilter``; the table's last body row is the hidden ``no-match`` line with
  its "show all" reset button, which ``pages.table`` skips.
- With no location there is no fleet card, no wrapper and no filter.
- Below md the same rows render as phone cards (UI-01, UI-12): under the heading "All
  locations", one ``li[data-status][data-delivery]`` per row in the table's order, each
  holding one link ``a[data-testid=location-card][data-location-id]`` to the location's
  page with the name, the status pill, the tags (Alerts off, then Router grace), "Last
  heartbeat {relative} · {absolute}" or "Last heartbeat Never", and "Delivery" with OK or
  the failing pill. The card's pill, time and delivery carry the same ``data-live`` and
  ``data-location-id`` as the table's (UI-05); the cards hold no element id. The list ends
  with its own hidden ``no-match`` item. Names render whole and escaped in the card, the
  row link and the sidebar link's title (only the sidebar truncates, visually).
- The meta count says "1 location" at one and "{N} locations" otherwise; the page keeps
  every ``pages.assert_page`` invariant with 0, 1 and 6 locations, the ops chat set or not,
  and delivery failing since today or since an earlier day.

Rows are read inside ``tr[data-testid=location-row]`` of the locations table, cards inside
each ``location-card`` and cells inside each fleet cell, never page-wide: the sidebar lists
the same locations. ``_shown`` drops every element carrying the ``hidden`` attribute (the
inactive delivery variant, the no-match lines) before a text read.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http.response import HttpResponseBase
from django.test import Client
from django.urls import reverse
from pages import (
    all_by_testid,
    assert_no_injected_script,
    assert_page,
    by_testid,
    main,
    parse,
    table,
    text,
)

from powermon.alerts import delivery
from powermon.engine.models import LocationState
from powermon.web import live
from powermon.web.templatetags.display_time import display_time
from powermon.web.views import LocationListView

User = get_user_model()

# The fleet tiles' data-metric values, in order (= the status JSON's counts keys).
METRICS = ["on", "off", "maintenance", "waiting", "failing"]
# The filter chips' data-filter values, in order, and each cell's label (copy row list.cells).
FILTERS = ["all", *METRICS]
CELL_LABELS = {
    "all": "All",
    "on": "On",
    "off": "Off",
    "maintenance": "Maintenance",
    "waiting": "Waiting",
    "failing": "Delivery failing",
}
# The bar's segments: the four statuses, never delivery failing.
SEGMENTS = ["on", "off", "maintenance", "waiting"]
NO_MATCH_TEXT = "No locations match — show all"
# The live elements of a row and of a card (06-UI-SPEC Test hooks > Attribute vocabularies).
CARD_LIVE = {"status-pill": "status", "last-heartbeat": "last-heartbeat", "delivery": "delivery"}
XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"
LONG_NAME = "x" * 100
OPS_TOKEN = "555555555:" + "C" * 35


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


@pytest.fixture
def list_clock(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> FakeClock:
    """The list view's clock, at ``fixed_now``: the relative times do not depend on today."""
    clock = FakeClock(fixed_now)
    monkeypatch.setattr(LocationListView, "clock", clock)
    return clock


def _set_state(location: Any, **fields: Any) -> None:
    LocationState.objects.filter(location=location).update(**fields)


def _power(location: Any, status: str, at: datetime) -> None:
    """The stored power state: on since ``at``, or off since ``at`` (its last heartbeat)."""
    if status == "on":
        _set_state(location, status="on", last_heartbeat_at=at, on_since=at)
    else:
        _set_state(location, status="off", last_heartbeat_at=at, outage_started_at=at)


def _fail(location: Any, started_at: datetime, status: int = 403) -> None:
    """Open the location's delivery_failing incident as the relay does (D-10)."""
    with transaction.atomic():
        delivery.open_failing(location.pk, started_at, status)


def _page(response: HttpResponseBase) -> BeautifulSoup:
    assert response.status_code == 200
    return parse(response)


def _inside(element: Tag, ancestor: Tag) -> bool:
    """``element`` is a descendant of this very ``ancestor`` (identity, not bs4 equality)."""
    return any(parent is ancestor for parent in element.parents)


def _fleet(soup: Tag) -> Tag:
    """The one ``[data-fleet]`` wrapper of the page."""
    found = soup.find_all(attrs={"data-fleet": True})
    assert len(found) == 1, f"{len(found)} [data-fleet] wrappers"
    return found[0]


def _rows(soup: Tag) -> list[Tag]:
    """The ``location-row`` elements of the locations table, in order."""
    return all_by_testid(by_testid(soup, "locations-table"), "location-row")


def _tiles(soup: Tag) -> dict[str, Tag]:
    """The fleet tiles of the fleet card by ``data-metric``, in document order."""
    tiles = all_by_testid(by_testid(soup, "fleet-summary"), "fleet-tile")
    return {str(tile["data-metric"]): tile for tile in tiles}


def _value(cell: Tag) -> int:
    """The cell's number: the text of its one ``fleet-count``, which equals its data-count."""
    shown = text(by_testid(cell, "fleet-count"))
    assert shown == cell.get("data-count"), (shown, cell.get("data-count"))
    return int(shown)


def _counts(soup: Tag) -> dict[str, int]:
    """Each fleet tile's number by ``data-metric``."""
    return {metric: _value(tile) for metric, tile in _tiles(soup).items()}


def _shown(page: HttpResponseBase | str) -> BeautifulSoup:
    """The page parsed, without the elements that carry the ``hidden`` attribute."""
    soup = parse(page)
    for element in [found for found in soup.find_all(True) if found.has_attr("hidden")]:
        element.extract()
    return soup


def _items(soup: Tag) -> list[Tag]:
    """The phone list's location items, ``li[data-status][data-delivery]``, in order."""
    return list(_fleet(soup).select("li[data-status][data-delivery]"))


def _card(item: Tag) -> Tag:
    """The item's one card link; it is the item's only link."""
    [card] = all_by_testid(item, "location-card")
    assert item.find_all("a") == [card]
    return card


def _cards(soup: Tag) -> list[Tag]:
    """The ``location-card`` links of the page, in order."""
    return all_by_testid(soup, "location-card")


def _card_of(soup: Tag, location: Any) -> Tag:
    """The one card of ``location``."""
    found = [card for card in _cards(soup) if card["data-location-id"] == str(location.pk)]
    assert len(found) == 1, f"{len(found)} cards for location {location.pk}"
    return found[0]


def _meta_count(soup: Tag) -> str:
    """The text of the meta count item ("3 locations")."""
    [value] = main(soup).find_all(attrs={"data-count-value": True})
    item = value.find_parent("li")
    assert isinstance(item, Tag), "the meta count is not inside a meta line item"
    return text(item)


# The counts (UI-04)


@pytest.mark.django_db
def test_UI04_fleet_counts_match_rows(
    admin: Client,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    earlier = fixed_now - timedelta(minutes=5)
    _power(location_factory(name="A on"), "on", fixed_now)
    _power(location_factory(name="B off"), "off", earlier)
    # Maintenance whatever the power underneath: never counted as on or off.
    _power(location_factory(name="C maintenance on", maintenance=True), "on", fixed_now)
    _power(location_factory(name="D maintenance off", maintenance=True), "off", earlier)
    location_factory(name="E waiting")
    off_failing = location_factory(name="F off failing")
    _power(off_failing, "off", earlier)
    _fail(off_failing, fixed_now - timedelta(minutes=1))

    soup = _page(admin.get("/"))

    tiles = _tiles(soup)
    assert list(tiles) == METRICS
    counts = _counts(soup)
    assert counts == {"on": 1, "off": 2, "maintenance": 2, "waiting": 1, "failing": 1}
    # Each status tile equals the table's rows with that data-status; failing equals the
    # rows with failing delivery, so the Off location with failing delivery counts twice.
    rows = _rows(soup)
    statuses = Counter(str(row["data-status"]) for row in rows)
    for metric in SEGMENTS:
        assert counts[metric] == statuses[metric], metric
    assert counts["failing"] == sum(row["data-delivery"] == "failing" for row in rows)
    [failing_row] = [row for row in rows if row["data-delivery"] == "failing"]
    assert failing_row["data-status"] == "off"
    # All counts every listed location once.
    total = by_testid(soup, "fleet-total")
    assert _value(total) == len(rows) == 6
    # The same numbers as fleet_counts of the live rows and as the status JSON's counts.
    assert counts == live.fleet_counts(live.live_rows(fixed_now))
    assert counts == admin.get(reverse("location-status-json")).json()["counts"]


@pytest.mark.django_db
def test_UI04_zero_metrics_render(
    admin: Client,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    _power(location_factory(name="Office"), "on", fixed_now)

    soup = _page(admin.get("/"))

    # A metric with no location renders the number 0 with data-count "0", never a blank.
    assert _counts(soup) == {"on": 1, "off": 0, "maintenance": 0, "waiting": 0, "failing": 0}
    for metric in ("off", "maintenance", "waiting", "failing"):
        tile = _tiles(soup)[metric]
        assert tile["data-count"] == "0"
        assert text(by_testid(tile, "fleet-count")) == "0"
    bar = by_testid(soup, "fleet-bar")
    segments = bar.find_all(attrs={"data-count": True})
    assert [(s["data-status"], s["data-count"]) for s in segments] == [
        ("on", "1"),
        ("off", "0"),
        ("maintenance", "0"),
        ("waiting", "0"),
    ]


@pytest.mark.django_db
def test_UI04_no_fleet_card_when_empty(
    admin: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    # Nothing to count: a deleted location is not listed.
    location_factory(name="tombstoned", deleted_at=fixed_now)

    soup = _page(admin.get("/"))

    by_testid(soup, "empty-state")
    for hook in (
        "fleet-summary",
        "fleet-showing",
        "fleet-bar",
        "fleet-total",
        "fleet-tile",
        "fleet-count",
        "filter-chip",
        "no-match",
        "location-card",
    ):
        assert all_by_testid(soup, hook) == [], hook
    assert soup.find_all(attrs={"data-fleet": True}) == []
    assert soup.find_all(attrs={"x-data": "fleetFilter"}) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("count", "showing"),
    [
        (1, "Showing 1 location"),
        (2, "Showing all 2 locations"),
        (6, "Showing all 6 locations"),
    ],
)
def test_UI04_fleet_showing_copy(
    admin: Client, location_factory: Callable[..., Any], count: int, showing: str
) -> None:
    for n in range(count):
        location_factory(name=f"Location {n}")

    soup = _page(admin.get("/"))

    card = by_testid(soup, "fleet-summary")
    heading = card.find("h2")
    assert isinstance(heading, Tag)
    assert text(heading) == "Fleet health"
    assert card.get("aria-labelledby") == heading.get("id")
    description = by_testid(card, "fleet-showing")
    assert text(description) == showing
    assert description.get("data-total") == str(count)
    assert description.get("aria-live") == "polite"
    assert _value(by_testid(card, "fleet-total")) == count


# The filter cells (N13)


@pytest.mark.django_db
def test_N13_filter_hooks(
    admin: Client,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    _power(location_factory(name="A on"), "on", fixed_now)
    off = location_factory(name="B off")
    _power(off, "off", fixed_now - timedelta(minutes=3))
    _fail(off, fixed_now - timedelta(minutes=1))
    location_factory(name="C waiting")

    response = admin.get("/")

    soup = _page(response)
    card = by_testid(soup, "fleet-summary")
    # The six cells form one group with its label, All first.
    groups = card.find_all(attrs={"role": "group"})
    assert len(groups) == 1
    group = groups[0]
    assert group.get("aria-label") == "Filter the list by status"
    cells = [by_testid(group, "fleet-total"), *all_by_testid(group, "fleet-tile")]
    assert [cell.get("data-metric", "all") for cell in cells] == FILTERS
    # One overlay button per cell, in order: JS only and hidden, All pressed.
    chips = all_by_testid(card, "filter-chip")
    assert [chip["data-filter"] for chip in chips] == FILTERS
    for cell, chip in zip(cells, chips, strict=True):
        filter_value = str(chip["data-filter"])
        assert all_by_testid(cell, "filter-chip") == [chip], filter_value
        assert chip.name == "button"
        assert chip.get("type") == "button"
        assert chip.has_attr("data-js-only")
        assert chip.has_attr("hidden")
        assert chip.get("aria-pressed") == ("true" if filter_value == "all" else "false")
        # Named by the cell's label and value, both existing ids inside the cell.
        ids = str(chip["aria-labelledby"]).split()
        assert len(ids) == 2, filter_value
        parts = []
        for element_id in ids:
            [target] = soup.find_all(id=element_id)
            assert _inside(target, cell), element_id
            parts.append(text(target))
        assert parts == [CELL_LABELS[filter_value], text(by_testid(cell, "fleet-count"))]
    # The bar: aria-hidden, JS only, the four status segments with their counts.
    bar = by_testid(card, "fleet-bar")
    assert bar.get("aria-hidden") == "true"
    assert bar.has_attr("data-js-only")
    assert bar.has_attr("hidden")
    segments = bar.find_all(attrs={"data-count": True})
    assert [segment["data-status"] for segment in segments] == SEGMENTS
    counts = _counts(soup)
    assert [int(segment["data-count"]) for segment in segments] == [
        counts[status] for status in SEGMENTS
    ]
    # Client-side only: no GET form, and no link carries a filter.
    forms = soup.find_all("form")
    assert [form for form in forms if str(form.get("method", "")).lower() != "post"] == []
    assert [link for link in soup.find_all("a", href=True) if "filter" in link["href"]] == []
    # The card and the table sit inside the one fleetFilter wrapper.
    fleet = _fleet(soup)
    assert fleet.get("x-data") == "fleetFilter"
    assert _inside(card, fleet)
    assert _inside(by_testid(soup, "locations-table"), fleet)
    # The table's no-match line: the last body row, hidden, with its reset button.
    tbody = by_testid(soup, "locations-table").find("tbody")
    assert isinstance(tbody, Tag)
    body_rows = tbody.find_all("tr", recursive=False)
    no_match = body_rows[-1]
    assert no_match.get("data-testid") == "no-match"
    assert no_match.has_attr("hidden")
    assert text(no_match) == NO_MATCH_TEXT
    [reset] = no_match.find_all(attrs={"data-filter-reset": True})
    assert reset.name == "button"
    assert reset.get("type") == "button"
    assert text(reset) == "show all"
    # pages.table skips the hidden line: one row per location.
    _, rows = table(soup, "locations-table")
    assert len(rows) == len(_rows(soup)) == 3


# The phone cards (UI-01, UI-12, UI-05)


@pytest.mark.django_db
def test_UI01_phone_cards(
    admin: Client,
    kyiv: Any,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    alpha = location_factory(name="Alpha", router_grace=True)
    beta = location_factory(name="beta", alerts_enabled=False)
    gamma = location_factory(name="Gamma")
    delta = location_factory(
        name="delta", maintenance=True, alerts_enabled=False, router_grace=True
    )
    alpha_beat = fixed_now - timedelta(seconds=90)
    beta_beat = fixed_now - timedelta(hours=3, minutes=5)
    _power(alpha, "on", alpha_beat)
    _power(beta, "off", beta_beat)
    _power(delta, "on", fixed_now)
    _fail(beta, fixed_now - timedelta(hours=1))

    response = admin.get("/")

    soup = _page(response)
    rows = _rows(soup)
    items = _items(soup)
    # One item per row, in the table's order (case-insensitive name), with the row's state.
    ordered = [alpha, beta, delta, gamma]
    assert [str(row["data-location-id"]) for row in rows] == [str(loc.pk) for loc in ordered]
    assert len(items) == len(rows) == 4
    for item, row, location in zip(items, rows, ordered, strict=True):
        card = _card(item)
        assert card["data-location-id"] == str(location.pk)
        assert card["href"] == f"/locations/{location.pk}/"
        assert item["data-status"] == row["data-status"]
        assert item["data-delivery"] == row["data-delivery"]
        # The pill shows the item's status; the tags follow in order.
        [pill] = all_by_testid(card, "status-pill")
        assert pill["data-status"] == item["data-status"]
        tags = [str(tag["data-tag"]) for tag in all_by_testid(card, "tag")]
        assert tags == [str(tag["data-tag"]) for tag in all_by_testid(row, "tag")]
        # The live elements carry the same data-live and location id as the row's.
        for testid, kind in CARD_LIVE.items():
            [element] = all_by_testid(card, testid)
            [twin] = all_by_testid(row, testid)
            assert element.get("data-live") == twin.get("data-live") == kind, testid
            assert element.get("data-location-id") == str(location.pk), testid
        [cell] = all_by_testid(card, "delivery")
        assert cell["data-delivery"] == item["data-delivery"]
        # A second copy repeats no element id.
        assert card.find_all(id=True) == []
    # Every card of the page sits in the fleetFilter wrapper.
    fleet = _fleet(soup)
    assert all(_inside(card, fleet) for card in _cards(soup))
    assert len(_cards(soup)) == 4
    # The list's heading: "All locations", naming the region that holds it.
    region = items[0].find_parent("section")
    assert isinstance(region, Tag)
    [heading] = soup.find_all(id=region["aria-labelledby"])
    assert text(heading) == "All locations"
    # What each card reads, with the inactive delivery variant left out.
    shown = _shown(response)
    assert [text(card) for card in _cards(shown)] == [
        f"Alpha On Router grace Last heartbeat 1 min ago · {display_time(alpha_beat)} Delivery OK",
        f"beta Off Alerts off Last heartbeat 3 h ago · {display_time(beta_beat)} "
        "Delivery Failing since 10:00 (http_403)",
        "delta Maintenance Alerts off Router grace Last heartbeat just now · "
        f"{display_time(fixed_now)} Delivery OK",
        "Gamma Waiting for first heartbeat Last heartbeat Never Delivery OK",
    ]
    # The card time: one <time datetime> with the unchanged text and one relative time.
    [heartbeat] = all_by_testid(_card_of(shown, alpha), "last-heartbeat")
    [time] = heartbeat.find_all("time")
    assert datetime.fromisoformat(str(time["datetime"])) == alpha_beat
    assert text(time) == display_time(alpha_beat) == "2026-10-01 10:58:30 EEST"
    [relative] = heartbeat.find_all(attrs={"data-relative": True})
    assert relative["data-relative"] == time["datetime"]
    assert text(relative) == "1 min ago"
    # Never: no time element and no relative time.
    [never] = all_by_testid(_card_of(shown, gamma), "last-heartbeat")
    assert never.find_all("time") == []
    assert never.find_all(attrs={"data-relative": True}) == []
    # Both delivery variants are in the DOM for the poll; only the inactive one is hidden.
    for location in ordered:
        [cell] = all_by_testid(_card_of(soup, location), "delivery")
        variants = {
            str(variant["data-delivery-variant"]): variant.has_attr("hidden")
            for variant in cell.find_all(attrs={"data-delivery-variant": True})
        }
        failing = cell["data-delivery"] == "failing"
        assert variants == {"ok": failing, "failing": not failing}


@pytest.mark.django_db
def test_UI12_long_and_xss_names_in_cards(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    hostile = location_factory(name=XSS_NAME)
    long = location_factory(name=LONG_NAME)

    html = admin.get("/").content.decode()

    soup = _shown(html)
    rows = {str(row["data-location-id"]): row for row in _rows(soup)}
    sidebar = {
        str(link["data-location-id"]): link
        for link in all_by_testid(by_testid(soup, "sidebar-locations"), "sidebar-location")
    }
    for location in (hostile, long):
        name = location.name
        # The card reads the whole name first; the parser decodes the escaped text.
        card = _card_of(soup, location)
        assert text(card) == f"{name} Waiting for first heartbeat Last heartbeat Never Delivery OK"
        assert card["href"] == f"/locations/{location.pk}/"
        # The row's link and the sidebar link's title carry the whole name too.
        [link] = all_by_testid(rows[str(location.pk)], "location-link")
        assert text(link) == name
        assert link["title"] == name
        assert sidebar[str(location.pk)]["title"] == name
    assert len(LONG_NAME) == 100
    assert XSS_NAME not in html
    assert html.count(ESCAPED_XSS_NAME) >= 3
    assert_no_injected_script(html, "list")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("count", "meta"), [(1, "1 location"), (2, "2 locations"), (5, "5 locations")]
)
def test_UI01_list_count_copy(
    admin: Client, location_factory: Callable[..., Any], count: int, meta: str
) -> None:
    for n in range(count):
        location_factory(name=f"Location {n}")

    soup = _page(admin.get("/"))

    assert _meta_count(soup) == meta
    # Every surface lists each location once.
    _, rows = table(soup, "locations-table")
    assert len(rows) == len(_rows(soup)) == len(_cards(soup)) == len(_items(soup)) == count
    # Two no-match lines, both hidden: the table's last row and the phone list's last item.
    lines = all_by_testid(soup, "no-match")
    assert [line.name for line in lines] == ["tr", "li"]
    tbody = by_testid(soup, "locations-table").find("tbody")
    assert isinstance(tbody, Tag)
    assert tbody.find_all("tr", recursive=False)[-1] is lines[0]
    phone_list = _cards(soup)[0].find_parent("ul")
    assert isinstance(phone_list, Tag)
    assert phone_list.find_all("li", recursive=False)[-1] is lines[1]
    for line in lines:
        assert line.has_attr("hidden")
        # Not a location item: the filter never counts it.
        assert not line.has_attr("data-status")
        assert text(line) == NO_MATCH_TEXT
        [reset] = line.find_all(attrs={"data-filter-reset": True})
        assert (reset.name, reset.get("type"), text(reset)) == ("button", "button", "show all")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("count", "ops_configured", "failing_since"),
    [
        (0, True, None),
        (0, False, None),
        (1, True, "today"),
        (1, False, "earlier"),
        (6, False, "today"),
        (6, True, "earlier"),
    ],
)
def test_UI12_list_page_invariants(
    admin: Client,
    settings: Any,
    kyiv: Any,
    list_clock: FakeClock,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    count: int,
    ops_configured: bool,
    failing_since: str | None,
) -> None:
    if ops_configured:
        settings.CFG = replace(settings.CFG, ops_bot_token=OPS_TOKEN, ops_chat_id=-1005555555555)
    else:
        settings.CFG = replace(settings.CFG, ops_bot_token="", ops_chat_id=None)
    locations = [location_factory(name=f"Location {n}") for n in range(count)]
    # UI-D6: HH:MM when the incident started today (11:00 in Kyiv now), else with its date.
    expected = {
        "today": ("Failing since 10:00 (http_403)", timedelta(hours=1)),
        "earlier": ("Failing since 2026-09-29 11:00 (http_403)", timedelta(days=2)),
    }
    if failing_since is not None:
        _fail(locations[0], fixed_now - expected[failing_since][1])

    soup = assert_page(admin.get("/"), title="Locations", app=True)

    assert len(_cards(soup)) == len(_items(soup) if count else []) == count
    assert len(all_by_testid(soup, "fleet-summary")) == (1 if count else 0)
    assert len(all_by_testid(soup, "ops-chat-banner")) == (0 if ops_configured else 1)
    if failing_since is not None:
        # The table row and the card show the same failing pill.
        shown = _shown(str(soup))
        first = str(locations[0].pk)
        [card] = [card for card in _cards(shown) if card["data-location-id"] == first]
        [row] = [row for row in _rows(shown) if row["data-location-id"] == first]
        for copy in (card, row):
            [cell] = all_by_testid(copy, "delivery")
            assert cell["data-delivery"] == "failing"
            assert text(cell) == expected[failing_since][0]
        assert _counts(soup)["failing"] == 1
