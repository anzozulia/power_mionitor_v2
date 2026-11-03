"""The admin's Compare page S14 (quick task 261103-lfm; D-1, D-2, D-5): the frame.

Task 2a's part: the route and its access rules, the sidebar item and the breadcrumb, the
GET search form (location chips with the status LED, the period radios, the dates and their
validation), the seven section wrappers in D-5 order with the jump bar, the KPI tiles and
the outage scoreboard, the escaping of user-typed names, the secret scan and the query
budget. The page is read only through tests/web/pages.py and its test hooks.

The fixture is the plan's "Page test fixture": now is Thu 2026-10-01 16:00 UTC (19:00 in
Kyiv); the default 7-day period is 25.09..01.10. All times below are UTC; every location is
on from 2026-09-24 00:00.

- Home: off 09-28 06:00-07:00, not monitored 07:00-07:30, off 07:30-08:00 (the SAME outage,
  INV-11), off 10-01 09:00-10:00; on since 10:00.
- Office: off 10-01 09:00-10:00; on since 10:00.
- Shop: off 10-01 09:30-11:00, off since 14:00 (in progress).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from pages import (
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    assert_page,
    breadcrumbs,
    by_testid,
    field,
    field_error,
    main,
    parse,
    table,
    text,
)
from secret_fixtures import SECRETS, TOKEN

from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import keys, validators
from powermon.locations.models import Location
from powermon.web import context_processors
from powermon.web.compare_views import CompareView
from powermon.web.templatetags import timefmt

User = get_user_model()

NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
URL = "/compare/"
SECTIONS = [
    "compare-filter",
    "compare-kpis",
    "compare-scoreboard",
    "compare-timeline",
    "compare-overlap",
    "compare-hours",
    "compare-weeks",
]
JUMP = [f"#{section}" for section in SECTIONS[1:]]
SHELL = {"relative", "sidebar", "theme", "toasts"}
MARKUP_NAME = '<b>x</b> & "q"'


def _t(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


START = _t(9, 24, 0)


@pytest.fixture
def admin(client: Client, db: None, settings: Any, monkeypatch: pytest.MonkeyPatch) -> Client:
    """The signed-in admin, the display TZ and every clock at NOW."""
    settings.TIME_ZONE = "Europe/Kyiv"
    _clock(monkeypatch, NOW)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    clock = FakeClock(now)
    monkeypatch.setattr(CompareView, "clock", clock)
    monkeypatch.setattr(timefmt, "CLOCK", clock)
    monkeypatch.setattr(context_processors, "CLOCK", clock)


def _store(location: Any, *pieces: tuple[str, datetime, datetime | None, datetime | None]) -> None:
    for state, start, end, outage in pieces:
        PowerInterval.objects.create(
            location=location, state=state, start_at=start, end_at=end, outage_start_at=outage
        )


def _live(location: Any, status: str, **fields: Any) -> None:
    LocationState.objects.filter(pk=location.pk).update(status=status, **fields)


def _home(factory: Callable[..., Any], name: str = "Home") -> Any:
    home = factory(name=name, bot_token=TOKEN)
    o1, o2 = _t(9, 28, 6), _t(10, 1, 9)
    _store(
        home,
        ("on", START, o1, None),
        ("off", o1, _t(9, 28, 7), o1),
        ("not_monitored", _t(9, 28, 7), _t(9, 28, 7, 30), None),
        ("off", _t(9, 28, 7, 30), _t(9, 28, 8), o1),
        ("on", _t(9, 28, 8), o2, None),
        ("off", o2, _t(10, 1, 10), o2),
        ("on", _t(10, 1, 10), None, None),
    )
    _live(home, "on", last_heartbeat_at=_t(10, 1, 15, 59), on_since=_t(10, 1, 10))
    return home


def _office(factory: Callable[..., Any], name: str = "Office") -> Any:
    office = factory(name=name, bot_token=TOKEN)
    o = _t(10, 1, 9)
    _store(
        office,
        ("on", START, o, None),
        ("off", o, _t(10, 1, 10), o),
        ("on", _t(10, 1, 10), None, None),
    )
    _live(office, "on", last_heartbeat_at=_t(10, 1, 15, 59), on_since=_t(10, 1, 10))
    return office


def _shop(factory: Callable[..., Any], name: str = "Shop") -> Any:
    shop = factory(name=name, bot_token=TOKEN)
    o1, o2 = _t(10, 1, 9, 30), _t(10, 1, 14)
    _store(
        shop,
        ("on", START, o1, None),
        ("off", o1, _t(10, 1, 11), o1),
        ("on", _t(10, 1, 11), o2, None),
        ("off", o2, None, o2),
    )
    _live(
        shop,
        "off",
        last_heartbeat_at=_t(10, 1, 14),
        on_since=_t(10, 1, 11),
        outage_started_at=o2,
    )
    return shop


def _fixture(factory: Callable[..., Any]) -> tuple[Any, Any, Any]:
    return _home(factory), _office(factory), _shop(factory)


def _page(admin: Client, query: str = "", **kwargs: Any) -> BeautifulSoup:
    response = admin.get(f"{URL}{query}")
    return assert_page(response, title="Compare locations", app=True, **kwargs)


def _x_data(page: Tag) -> set[str]:
    return {str(element["x-data"]) for element in page.find_all(attrs={"x-data": True})}


def _kpis(page: Tag) -> dict[str, Tag]:
    return {str(tile["data-metric"]): tile for tile in all_by_testid(page, "compare-kpi")}


def _value(tile: Tag, hook: str = "compare-kpi-value") -> str:
    return text(by_testid(tile, hook))


def _input(page: Tag, name: str, value: str) -> Tag:
    found = page.find("input", attrs={"name": name, "value": value})
    assert isinstance(found, Tag), (name, value)
    return found


# Defaults


@pytest.mark.django_db
def test_compare_page(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin)

    # The seven cards in D-5 order, each a section whose id is its testid, focusable as a
    # jump target, named by its h2; the jump bar links to the six after the filters.
    sections = [str(found["id"]) for found in page.find_all("section", id=True)]
    assert sections == SECTIONS
    for name in SECTIONS:
        card = by_testid(page, name)
        assert (card.name, card["id"], card["tabindex"]) == ("section", name, "-1")
        assert page.find(id=str(card["aria-labelledby"])).name == "h2"  # type: ignore[union-attr]
    jump = by_testid(page, "compare-jump")
    assert jump.get("aria-label") == "Sections"
    assert [str(link["href"]) for link in jump.find_all("a")] == JUMP
    # The form: a GET search form to /compare/; every chip checked, with its status LED.
    form = by_testid(page, "compare-form")
    assert (form["method"], form["role"], form["action"]) == ("get", "search", URL)
    chips = all_by_testid(form, "compare-loc-chip")
    assert [text(chip) for chip in chips] == ["Home", "Office", "Shop"]
    assert [chip["data-location-id"] for chip in chips] == [
        str(home.pk),
        str(office.pk),
        str(shop.pk),
    ]
    assert [chip["data-status"] for chip in chips] == ["on", "on", "off"]
    for chip in chips:
        box = chip.find("input", attrs={"type": "checkbox", "name": "loc"})
        assert isinstance(box, Tag) and box.has_attr("checked")
        assert chip.find("span", attrs={"aria-hidden": "true"}) is not None
    # The sentinel, the period and the dates prefilled with the effective period.
    assert _input(form, "loc", "").get("type") == "hidden"
    assert _input(form, "range", "7d").has_attr("checked")
    assert not _input(form, "range", "custom").has_attr("checked")
    assert (field(page, "from")["value"], field(page, "to")["value"]) == (
        "2026-09-25",
        "2026-10-01",
    )
    assert text(by_testid(form, "compare-submit")) == "Compare"
    reset = by_testid(form, "compare-reset")
    assert (reset["href"], text(reset)) == (URL, "Reset")
    assert text(by_testid(page, "compare-summary")) == (
        "3 locations · 25 Sep – 1 Oct 2026 (7 days)"
    )
    # The trail and the nav item.
    assert breadcrumbs(page) == [("Compare", None)]
    nav = by_testid(page, "nav-compare")
    assert (nav["href"], nav["title"], nav.get("aria-current")) == (URL, "Compare", "page")
    assert not all_by_testid(page, "compare-invalid")


@pytest.mark.django_db
def test_compare_page_frame_numbers(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin)

    headers, rows = table(page, "compare-scoreboard-table")
    assert headers == [
        "Rank",
        "Location",
        "Off time",
        "Off share",
        "Outages",
        "Avg outage",
        "Longest",
        "Days with outages",
        "Monitored",
    ]
    assert rows == [
        ["1", "Shop", "3h 30m", "2%", "2", "1h 45m", "2h · 01.10", "1 of 7", "100%"],
        ["2", "Home", "2h 30m", "2%", "2", "1h 15m", "1h 30m · 28.09", "2 of 7", "100%"],
        ["3", "Office", "1h", "1%", "1", "1h", "1h · 01.10", "1 of 7", "100%"],
    ]
    board = by_testid(page, "compare-scoreboard-table")
    trs = all_by_testid(board, "compare-row")
    assert [(tr["data-rank"], tr["data-location-id"]) for tr in trs] == [
        ("1", str(shop.pk)),
        ("2", str(home.pk)),
        ("3", str(office.pk)),
    ]
    links = all_by_testid(board, "compare-location-link")
    assert [link["href"] for link in links] == [
        f"/locations/{shop.pk}/",
        f"/locations/{home.pk}/",
        f"/locations/{office.pk}/",
    ]
    bars = all_by_testid(board, "off-bar")
    assert [bar["data-off-us"] for bar in bars] == [
        str(210 * 60_000_000),
        str(150 * 60_000_000),
        str(60 * 60_000_000),
    ]
    assert all((bar["aria-hidden"], bar["focusable"]) == ("true", "false") for bar in bars)
    assert not all_by_testid(page, "partial-data")
    # The phone cards: the same rows, the same order.
    cards = all_by_testid(page, "compare-card")
    assert [card["data-location-id"] for card in cards] == [
        str(shop.pk),
        str(home.pk),
        str(office.pk),
    ]
    # The KPI tiles.
    tiles = _kpis(page)
    assert list(tiles) == ["off-most", "outages-most", "longest", "all-off", "power-somewhere"]
    assert (_value(tiles["off-most"]), _value(tiles["off-most"], "compare-kpi-name")) == (
        "3h 30m",
        "Shop",
    )
    assert (_value(tiles["outages-most"]), _value(tiles["outages-most"], "compare-kpi-name")) == (
        "2",
        "Home",
    )
    assert (_value(tiles["longest"]), _value(tiles["longest"], "compare-kpi-name")) == (
        "2h",
        "Shop",
    )
    assert "01.10" in text(tiles["longest"])
    assert _value(tiles["all-off"]) == "30m"
    assert "<1%" in text(tiles["all-off"])
    assert _value(tiles["power-somewhere"]) == "100%"


# Custom range, one location, no locations


@pytest.mark.django_db
def test_compare_page_custom_range(admin: Client, location_factory: Callable[..., Any]) -> None:
    _fixture(location_factory)

    page = _page(admin, "?range=custom&from=2026-09-01&to=2026-10-01")

    assert text(by_testid(page, "compare-summary")) == (
        "3 locations · 1 Sep – 1 Oct 2026 (31 days)"
    )
    assert _input(page, "range", "custom").has_attr("checked")
    # Edge: 7 days of data in 31 is partial data on every row.
    assert len(all_by_testid(page, "partial-data")) == 3
    # Edge: no loc in the query at all selects every location, as the bare URL does.
    bare = _page(admin, "?range=14d")
    assert len(all_by_testid(by_testid(bare, "compare-scoreboard-table"), "compare-row")) == 3
    assert text(by_testid(bare, "compare-summary")).endswith("(14 days)")


@pytest.mark.django_db
def test_compare_page_one_location(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin, f"?loc={home.pk}&range=7d")

    _headers, rows = table(page, "compare-scoreboard-table")
    assert [row[1] for row in rows] == ["Home"]
    assert list(_kpis(page)) == ["off-most", "outages-most", "longest"]
    checked = {
        str(chip["data-location-id"]): _input(chip, "loc", str(chip["data-location-id"])).has_attr(
            "checked"
        )
        for chip in all_by_testid(page, "compare-loc-chip")
    }
    assert checked == {str(home.pk): True, str(office.pk): False, str(shop.pk): False}
    assert text(by_testid(page, "compare-summary")).startswith("1 location · ")


@pytest.mark.django_db
def test_compare_page_without_locations(admin: Client) -> None:
    page = _page(admin)

    empty = by_testid(page, "empty-state")
    assert "No locations yet" in text(empty)
    assert by_testid(empty, "add-location")["href"] == "/locations/new/"
    assert not all_by_testid(page, "compare-form")
    assert not all_by_testid(page, "compare-jump")


# Invalid filters: a field error on a 200 page, never a 500


INVALID = [
    ("deleted", "loc", "Pick locations from the list."),
    ("?range=year", "range", "Choose a period from the list."),
    (
        "?range=custom&from=2026-09-30&to=2026-09-20",
        "to",
        "The end date must not be before the start date.",
    ),
    ("?range=custom&from=2026-09-25&to=2026-10-02", "to", "The end date cannot be in the future."),
    (
        "?range=custom&from=2025-12-31&to=2026-01-05",
        "from",
        "Pick a start date on or after 01.01.2026.",
    ),
    ("?range=custom&from=garbage&to=2026-10-01", "from", "Enter a valid start date."),
    ("?range=custom&to=2026-10-01", "from", "Enter a start date."),
    ("?range=custom&from=2026-09-25", "to", "Enter an end date."),
    ("?loc=&range=7d", "loc", "Pick at least one location."),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("query", "name", "error"), INVALID)
def test_compare_page_invalid_filters(
    admin: Client, location_factory: Callable[..., Any], query: str, name: str, error: str
) -> None:
    home, _office_, _shop_ = _fixture(location_factory)
    if query == "deleted":
        gone = location_factory(name="Gone")
        Location.objects.filter(pk=gone.pk).update(deleted_at=NOW)
        query = f"?loc={home.pk}&loc={gone.pk}"

    page = _page(admin, query)

    assert field_error(page, name) == error
    summary = by_testid(page, "error-summary")
    assert "Check the filters marked below." in text(summary)
    assert [str(link["href"]) for link in summary.find_all("a")] == [f"#id_{name}"]
    assert text(by_testid(page, "compare-invalid")) == "Fix the filters to see the comparison."
    assert not all_by_testid(page, "compare-scoreboard-table")
    assert not all_by_testid(page, "compare-kpis")
    assert not all_by_testid(page, "compare-jump")
    assert not all_by_testid(page, "compare-summary")
    # The form is still there, with the submitted values, so the admin can fix them.
    by_testid(page, "compare-form")
    assert _x_data(page) == SHELL | {"errorSummary"}


@pytest.mark.django_db
def test_compare_page_invalid_dates_come_back_as_typed(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    _fixture(location_factory)

    page = _page(admin, "?range=custom&from=garbage&to=2026-10-01")

    assert (field(page, "from")["value"], field(page, "to")["value"]) == ("garbage", "2026-10-01")
    assert field(page, "from")["aria-invalid"] == "true"
    assert "id_from_error" in str(field(page, "from")["aria-describedby"])
    assert not field(page, "to").has_attr("aria-invalid")


@pytest.mark.django_db
def test_compare_page_span_rule(
    admin: Client, location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clock(monkeypatch, datetime(2027, 3, 1, 12, 0, tzinfo=UTC))
    _fixture(location_factory)

    # Failure: 367 days.
    refused = _page(admin, "?range=custom&from=2026-02-01&to=2027-02-02")
    assert field_error(refused, "from") == "Pick at most 366 days."
    assert field_error(refused, "to") is None
    assert not all_by_testid(refused, "compare-scoreboard-table")
    # Edge: exactly 366 days is accepted.
    accepted = _page(admin, "?range=custom&from=2026-02-01&to=2027-02-01")
    assert field_error(accepted, "from") is None
    assert not all_by_testid(accepted, "error-summary")
    assert text(by_testid(accepted, "compare-summary")).endswith("(366 days)")
    by_testid(accepted, "compare-scoreboard-table")


# Security


@pytest.mark.django_db
def test_compare_page_escapes_names(admin: Client, location_factory: Callable[..., Any]) -> None:
    _home(location_factory, MARKUP_NAME)
    _shop(location_factory)

    response = admin.get(URL)
    page = assert_page(response, title="Compare locations", app=True)
    body = response.content.decode()

    assert "<b>x</b>" not in body
    assert "&lt;b&gt;x&lt;/b&gt; &amp; &quot;q&quot;" in body
    assert_no_injected_script(body, "compare")
    chips = all_by_testid(page, "compare-loc-chip")
    assert text(chips[0]) == MARKUP_NAME
    board = by_testid(page, "compare-scoreboard-table")
    assert MARKUP_NAME in [text(link) for link in all_by_testid(board, "compare-location-link")]
    names = [text(found) for found in all_by_testid(page, "compare-kpi-name")]
    assert MARKUP_NAME in names


@pytest.mark.django_db
def test_compare_page_shows_no_secret(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)
    secrets: list[str] = [*SECRETS]
    for location in (home, office, shop):
        current = Location.objects.get(pk=location.pk)
        secrets += [
            current.bot_token,
            validators.mask_token(current.bot_token),
            current.device_key,
            keys.mask_key(current.device_key),
        ]

    for query in ("", f"?loc={home.pk}&range=7d", "?range=year"):
        response = admin.get(f"{URL}{query}")
        assert response.status_code == 200, query
        assert_no_secrets(response.content.decode(), secrets, label=f"compare{query}")
        assert "•" not in response.content.decode(), query


@pytest.mark.django_db
def test_compare_page_access(admin: Client, location_factory: Callable[..., Any]) -> None:
    _fixture(location_factory)
    anonymous = Client()

    # Anonymous GET and POST go to the sign-in page; the page needs a session.
    for response in (anonymous.get(URL), anonymous.post(URL)):
        assert response.status_code == 302
        assert response["Location"] == "/login/?next=/compare/"
    # Failure: the signed-in admin's POST is not allowed (the page is GET only).
    assert admin.post(URL).status_code == 405
    # Expected: the signed-in GET is the page; it writes nothing.
    before = PowerInterval.objects.count(), LocationState.objects.count()
    assert admin.get(URL).status_code == 200
    assert (PowerInterval.objects.count(), LocationState.objects.count()) == before


# Budget, nav, no JS


def _count(admin: Client, query: str = "") -> int:
    with CaptureQueriesContext(connection) as captured:
        assert admin.get(f"{URL}{query}").status_code == 200
    return len(captured.captured_queries)


@pytest.mark.django_db
def test_compare_page_query_count_does_not_grow(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    _home(location_factory)
    _shop(location_factory)
    admin.get(URL)

    two = _count(admin)
    for i in range(4):
        _office(location_factory, f"Office {i}")
    six = _count(admin)

    # Expected: no N+1 in the view, the templates or the sidebar.
    assert two == six
    # Edge: an invalid page reads less (no timeline), never more.
    assert _count(admin, "?range=year") < six


@pytest.mark.django_db
def test_compare_nav_item(admin: Client, location_factory: Callable[..., Any]) -> None:
    _home(location_factory)
    hooks = ("nav-locations", "nav-add-location", "nav-compare")

    listing = parse(admin.get("/"))
    compare_page = parse(admin.get(URL))

    assert by_testid(listing, "nav-compare").get("aria-current") is None
    current = [hook for hook in hooks if by_testid(compare_page, hook).has_attr("aria-current")]
    assert current == ["nav-compare"]
    # The item comes after Add location in the Main nav.
    nav = compare_page.find("nav", attrs={"aria-label": "Main"})
    assert isinstance(nav, Tag)
    order = [str(link["data-testid"]) for link in nav.find_all("a")]
    assert order == list(hooks)


@pytest.mark.django_db
def test_compare_page_needs_no_js(admin: Client, location_factory: Callable[..., Any]) -> None:
    _fixture(location_factory)

    page = _page(admin)

    # Exactly the shell's components: the page binds no Alpine component of its own.
    assert _x_data(page) == SHELL
    assert main(page).find_all(attrs={"data-js-only": True}) == []
    # Failure: an invalid page adds only the error summary's focus component.
    assert _x_data(_page(admin, "?range=year")) == SHELL | {"errorSummary"}
