"""The Locations page S3: Fleet health and its filter cells (06-UI-SPEC Page Contracts > S3;
UI-04, D6-04, polish N13).

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

Rows are read inside ``tr[data-testid=location-row]`` of the locations table and cells
inside each fleet cell, never page-wide: the sidebar lists the same locations.
"""

from collections import Counter
from collections.abc import Callable
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
from pages import all_by_testid, by_testid, parse, table, text

from powermon.alerts import delivery
from powermon.engine.models import LocationState
from powermon.web import live
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


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


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
