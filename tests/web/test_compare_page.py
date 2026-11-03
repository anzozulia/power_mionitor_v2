"""The admin's Compare page S14 (quick task 261103-lfm; D-1, D-2, D-5): the frame.

Task 2a's part: the route and its access rules, the sidebar item and the breadcrumb, the
GET search form (location chips with the status LED, the period radios, the dates and their
validation), the seven section wrappers in D-5 order with the jump bar, the KPI tiles and
the outage scoreboard, the escaping of user-typed names, the secret scan and the query
budget. Task 2b's part: the timeline (day groups, lanes, segment titles, totals, the now
marker, the window of up to 31 days paged by "Earlier days" / "Later days" through ``tl``,
the daily-totals table), the overlap card
(matrix, pairs, simultaneous outages, the one-location line), the hours chart, the weeks
table, the escaping inside the SVGs and the decorative-SVG rule. The page is read only
through tests/web/pages.py and its test hooks.

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
    # IN-06: the server validates; the date inputs' min/max never block a preset submit.
    assert form.has_attr("novalidate")
    # The form does not carry the timeline's tl: a new comparison resets the window.
    assert form.find(attrs={"name": "tl"}) is None
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
    # IN-02: Home's 30m not monitored is 99.7 % monitored, which reads ">99%", not "100%".
    assert rows == [
        ["1", "Shop", "3h 30m", "2%", "2", "1h 45m", "2h · 01.10", "1 of 7", "100%"],
        ["2", "Home", "2h 30m", "2%", "2", "1h 15m", "1h 30m · 28.09", "2 of 7", ">99%"],
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
    # IN-02: the 30m with every location off is not "power somewhere": ">99%".
    assert _value(tiles["power-somewhere"]) == ">99%"


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


# Verification info 1: with a preset the dates are ignored entirely (no field error).
PRESET_IGNORES_DATES = [
    "?range=7d&from=garbage&to=2099-01-01",
    "?from=garbage&to=nonsense",
    "?range=7d&from=2025-06-01&to=2026-10-02",
    "?range=7d&from=2026-09-30&to=2026-09-20",
]


@pytest.mark.django_db
@pytest.mark.parametrize("query", PRESET_IGNORES_DATES)
def test_compare_page_preset_ignores_the_dates(
    admin: Client, location_factory: Callable[..., Any], query: str
) -> None:
    _fixture(location_factory)

    page = _page(admin, query)

    assert not all_by_testid(page, "error-summary")
    assert (field_error(page, "from"), field_error(page, "to")) == (None, None)
    # The date inputs show the preset's effective dates, not the ignored text.
    assert (field(page, "from")["value"], field(page, "to")["value"]) == (
        "2026-09-25",
        "2026-10-01",
    )
    assert text(by_testid(page, "compare-summary")).endswith("(7 days)")
    by_testid(page, "compare-scoreboard-table")


@pytest.mark.django_db
def test_compare_page_preset_with_another_error_still_shows_its_dates(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # Edge: an invalid page with a preset (no location picked) still shows the preset's
    # dates and no date error; only the location error is listed.
    _fixture(location_factory)

    page = _page(admin, "?loc=&range=14d&from=garbage")

    assert field_error(page, "loc") == "Pick at least one location."
    assert field_error(page, "from") is None
    assert (field(page, "from")["value"], field(page, "to")["value"]) == (
        "2026-09-18",
        "2026-10-01",
    )


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


# Task 2b: the charts


def _lane(page: Tag, day: str, key: str) -> Tag:
    found = [
        lane
        for lane in all_by_testid(page, "tl-lane")
        if (lane["data-day"], lane["data-location-id"]) == (day, key)
    ]
    assert len(found) == 1, (day, key)
    return found[0]


def _total(page: Tag, day: str, key: str) -> str:
    found = [
        total
        for total in all_by_testid(page, "tl-total")
        if (total["data-day"], total["data-location-id"]) == (day, key)
    ]
    assert len(found) == 1, (day, key)
    return text(found[0])


def _direct_title(svg: Tag) -> str:
    found = svg.find("title", recursive=False)
    assert isinstance(found, Tag), "no direct <title>"
    return text(found)


@pytest.mark.django_db
def test_compare_page_timeline(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin)

    chart = by_testid(page, "compare-timeline-chart")
    assert (chart.name, chart["role"]) == ("svg", "img")
    assert _direct_title(chart).startswith("Timeline of power on and off")
    desc = page.find(id=str(chart["aria-describedby"]))
    assert isinstance(desc, Tag) and text(desc)
    # Phones: one muted line says the chart scrolls sideways (hidden from md up).
    hint = by_testid(page, "compare-timeline-hint")
    assert text(hint) == "Scroll sideways to see the whole day."
    assert hint.find_next("svg") is chart
    days = all_by_testid(chart, "tl-day")
    assert [day["data-day"] for day in days] == [
        "2026-09-25",
        "2026-09-26",
        "2026-09-27",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
    ]
    assert [day["data-today"] for day in days] == ["false"] * 6 + ["true"]
    assert text(by_testid(days[-1], "tl-now")) == "19:00"
    assert len(all_by_testid(chart, "tl-now")) == 1
    keys = [str(home.pk), str(office.pk), str(shop.pk), "together"]
    for day in days:
        lanes = all_by_testid(day, "tl-lane")
        assert [lane["data-location-id"] for lane in lanes] == keys
    # The lane title holds the full name; a segment its own hover title.
    home_lane = _lane(page, "2026-10-01", str(home.pk))
    assert _direct_title(home_lane) == "Home"
    offs = [seg for seg in all_by_testid(home_lane, "tl-seg") if seg["data-state"] == "off"]
    assert [_direct_title(seg) for seg in offs] == ["Home · Power off · 12:00–13:00 · 1h"]
    assert (offs[0]["data-start-us"], offs[0]["data-end-us"]) == (
        str(12 * 3_600_000_000),
        str(13 * 3_600_000_000),
    )
    assert _total(page, "2026-10-01", "together") == "all off 30m"
    assert _total(page, "2026-09-25", "together") == "no overlap"
    assert _total(page, "2026-10-01", str(shop.pk)) == "3h 30m · 2"
    assert _total(page, "2026-09-25", str(home.pk)) == "no outages"
    # Monday 28.09 opens a new week: one divider.
    assert len(all_by_testid(chart, "tl-week-divider")) == 1
    # No paging for 7 days.
    assert not all_by_testid(page, "compare-timeline-window")
    assert not all_by_testid(page, "compare-earlier")
    # The daily totals as a table, closed by default.
    daily = by_testid(page, "compare-daily")
    assert daily.name == "details" and not daily.has_attr("open")
    headers, rows = table(daily, "compare-daily-table")
    assert headers == ["Day", "Home", "Office", "Shop", "Together"]
    assert len(rows) == 7
    assert rows[-1] == ["Thu 01.10", "1h · 1", "1h · 1", "3h 30m · 2", "all off 30m"]


def _pager(page: Tag) -> dict[str, str]:
    """The timeline's paging links (top and bottom) by testid, as their hrefs."""
    hooks = ("compare-earlier", "compare-later", "compare-earlier-bottom", "compare-later-bottom")
    return {hook: str(by_testid(page, hook)["href"]) for hook in hooks if all_by_testid(page, hook)}


def _days_shown(page: Tag) -> list[str]:
    return [str(day["data-day"]) for day in all_by_testid(page, "tl-day")]


SAME_SECTIONS = ("compare-kpis", "compare-scoreboard", "compare-overlap", "compare-hours")


@pytest.mark.django_db
def test_compare_page_timeline_window_default(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    _fixture(location_factory)

    page = _page(admin, "?range=90d")

    days = _days_shown(page)
    assert (len(days), days[0], days[-1]) == (31, "2026-09-01", "2026-10-01")
    window = by_testid(page, "compare-timeline-window")
    assert text(window).startswith("Showing 01.09 – 01.10 of the period.")
    # The window line comes before the chart, the bottom pager after it.
    chart = by_testid(page, "compare-timeline-chart")
    assert window.find_next("svg") is chart
    assert by_testid(page, "compare-timeline-pager").find_previous("svg") is chart
    # Expected: only an earlier page at the period's end, top and bottom, same href.
    assert _pager(page) == {
        "compare-earlier": "?range=90d&tl=2026-08-31",
        "compare-earlier-bottom": "?range=90d&tl=2026-08-31",
    }
    assert text(by_testid(page, "compare-earlier")) == "Earlier days"
    # The daily-totals table follows the window.
    _headers, rows = table(page, "compare-daily-table")
    assert (len(rows), rows[0][0], rows[-1][0]) == (31, "Tue 01.09", "Thu 01.10")


@pytest.mark.django_db
def test_compare_page_timeline_pages_back_twice(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    _fixture(location_factory)
    default = _page(admin, "?range=90d")

    second = _page(admin, _pager(default)["compare-earlier"])
    days = _days_shown(second)
    assert (len(days), days[0], days[-1]) == (31, "2026-08-01", "2026-08-31")
    assert text(by_testid(second, "compare-timeline-window")).startswith(
        "Showing 01.08 – 31.08 of the period."
    )
    assert _pager(second) == {
        "compare-earlier": "?range=90d&tl=2026-07-31",
        "compare-later": "?range=90d&tl=2026-10-01",
        "compare-earlier-bottom": "?range=90d&tl=2026-07-31",
        "compare-later-bottom": "?range=90d&tl=2026-10-01",
    }
    assert text(by_testid(second, "compare-later")) == "Later days"
    assert not all_by_testid(second, "tl-now")

    # The first window starts at the period's start: no earlier page.
    first = _page(admin, _pager(second)["compare-earlier"])
    days = _days_shown(first)
    assert (len(days), days[0], days[-1]) == (28, "2026-07-04", "2026-07-31")
    assert _pager(first) == {
        "compare-later": "?range=90d&tl=2026-08-31",
        "compare-later-bottom": "?range=90d&tl=2026-08-31",
    }
    # Later days lead back to the second window, and from there to the default one.
    assert _days_shown(_page(admin, _pager(first)["compare-later"])) == _days_shown(second)
    assert _days_shown(_page(admin, _pager(second)["compare-later"])) == _days_shown(default)

    # Every other section keeps the whole period: the same numbers on every page.
    for name in SAME_SECTIONS:
        assert text(by_testid(first, name)) == text(by_testid(default, name)), name
    assert table(first, "compare-weeks-table") == table(default, "compare-weeks-table")
    assert text(by_testid(first, "compare-summary")) == text(by_testid(default, "compare-summary"))


@pytest.mark.django_db
def test_compare_page_timeline_links_keep_every_other_param(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    home, office, _shop_ = _fixture(location_factory)
    query = f"?loc=&loc={home.pk}&loc={office.pk}&range=custom&from=2026-08-22&to=2026-10-01"

    page = _page(admin, f"{query}&tl=2026-09-15")

    days = _days_shown(page)
    assert (days[0], days[-1]) == ("2026-08-22", "2026-09-15")
    assert _pager(page) == {
        "compare-later": f"{query}&tl=2026-10-01",
        "compare-later-bottom": f"{query}&tl=2026-10-01",
    }
    # The form keeps the custom period and never carries tl.
    assert (field(page, "from")["value"], field(page, "to")["value"]) == (
        "2026-08-22",
        "2026-10-01",
    )
    assert by_testid(page, "compare-form").find(attrs={"name": "tl"}) is None


CLAMPED_TL = [
    # (tl, the first and last day shown): navigation is clamped, never an error.
    ("2020-01-01", ("2026-07-04", "2026-07-04")),
    ("2099-12-31", ("2026-09-01", "2026-10-01")),
    ("garbage", ("2026-09-01", "2026-10-01")),
    ("2026-02-30", ("2026-09-01", "2026-10-01")),
    ("", ("2026-09-01", "2026-10-01")),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("tl", "shown"), CLAMPED_TL)
def test_compare_page_timeline_clamps_tl(
    admin: Client, location_factory: Callable[..., Any], tl: str, shown: tuple[str, str]
) -> None:
    _fixture(location_factory)
    default = _page(admin, "?range=90d")

    page = _page(admin, f"?range=90d&tl={tl}")

    assert not all_by_testid(page, "error-summary")
    days = _days_shown(page)
    assert (days[0], days[-1]) == shown
    for name in SAME_SECTIONS:
        assert text(by_testid(page, name)) == text(by_testid(default, name)), name


@pytest.mark.django_db
def test_compare_page_short_period_has_no_pager(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # A period that fits one window shows it whole, with no paging line or link.
    _fixture(location_factory)

    page = _page(admin, "?range=30d")

    assert len(_days_shown(page)) == 30
    assert not all_by_testid(page, "compare-timeline-window")
    assert not all_by_testid(page, "compare-timeline-pager")
    assert _pager(page) == {}
    # Edge: a hand-made tl inside it still clamps the window's start at the period's start
    # (up to 31 days ending at tl) and offers the way back with "Later days".
    paged = _page(admin, "?range=30d&tl=2026-09-10")
    days = _days_shown(paged)
    assert (days[0], days[-1]) == ("2026-09-02", "2026-09-10")
    assert _pager(paged) == {
        "compare-later": "?range=30d&tl=2026-10-01",
        "compare-later-bottom": "?range=30d&tl=2026-10-01",
    }


def _terms(card: Tag) -> list[tuple[str, str]]:
    """A phone card's labelled values: (dt text, dd text) pairs in order."""
    return [
        (text(term), text(value))
        for term, value in zip(card.find_all("dt"), card.find_all("dd"), strict=True)
    ]


@pytest.mark.django_db
def test_compare_page_overlap(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin)

    matrix = by_testid(page, "compare-matrix")
    cells = {
        (str(cell["data-row-id"]), str(cell["data-col-id"])): cell
        for cell in all_by_testid(matrix, "matrix-cell")
    }
    h, o, s = str(home.pk), str(office.pk), str(shop.pk)
    assert len(cells) == 9
    assert (cells[(h, o)]["data-share"], cells[(h, o)]["data-bucket"]) == ("40", "3")
    assert text(cells[(h, o)]) == "While Home was off, Office was also off 40% of the time (1h)."
    assert (cells[(o, h)]["data-share"], cells[(o, h)]["data-bucket"]) == ("100", "5")
    assert cells[(h, s)]["data-share"] == "20"
    assert cells[(s, h)]["data-share"] == "14"
    assert cells[(o, s)]["data-share"] == "50"
    assert not cells[(h, h)].has_attr("data-share")
    # The pairs, most both-off time first.
    _headers, pairs = table(page, "compare-pairs")
    assert pairs == [
        [
            "A · Home B · Office",
            "1h",
            "40%",
            "100%",
            "A 1 of 2 B 1 of 1",
            "same minute",
            "same minute",
        ],
        [
            "A · Home B · Shop",
            "30m",
            "20%",
            "14%",
            "A 1 of 2 B 1 of 2",
            "B 30m later",
            "B 1h later",
        ],
        [
            "A · Office B · Shop",
            "30m",
            "50%",
            "14%",
            "A 1 of 1 B 1 of 2",
            "B 30m later",
            "B 1h later",
        ],
    ]
    rows = all_by_testid(page, "pair-row")
    assert [(row["data-a"], row["data-b"]) for row in rows] == [(h, o), (h, s), (o, s)]
    # The phone cards: the same pairs in the same order, values labelled; the table hooks
    # are on the table only, so pair-row counts each pair once.
    pair_cards = all_by_testid(page, "pair-card")
    assert [(card["data-a"], card["data-b"]) for card in pair_cards] == [(h, o), (h, s), (o, s)]
    assert all(not all_by_testid(card, "pair-row") for card in pair_cards)
    assert text(pair_cards[1]).startswith("A · Home B · Shop ")
    assert _terms(pair_cards[1]) == [
        ("Both off", "30m"),
        ("Coinciding outages", "A 1 of 2 B 1 of 2"),
        ("% of A's off", "20%"),
        ("% of B's off", "14%"),
        ("Start gap", "B 30m later"),
        ("End gap", "B 1h later"),
    ]
    assert not all_by_testid(page, "compare-pairs-none")
    # Exactly one simultaneous outage, all three off together inside it.
    _headers, sims = table(page, "compare-simultaneous")
    assert sims == [["Thu 01.10 12:00", "Thu 01.10 13:00", "1h", "Home, Office, Shop all 3"]]
    assert len(all_by_testid(page, "sim-row")) == 1
    assert text(by_testid(page, "sim-all")) == "all 3"
    sim_cards = all_by_testid(page, "sim-card")
    assert [_terms(card) for card in sim_cards] == [
        [
            ("Start", "Thu 01.10 12:00"),
            ("End", "Thu 01.10 13:00"),
            ("Duration", "1h"),
            ("Locations off", "Home, Office, Shop all 3"),
        ]
    ]
    assert not all_by_testid(page, "compare-simultaneous-empty")
    assert not all_by_testid(page, "compare-simultaneous-cap")
    assert not all_by_testid(page, "compare-overlap-single")


@pytest.mark.django_db
def test_compare_page_one_location_charts(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    home, _office_, _shop_ = _fixture(location_factory)

    page = _page(admin, f"?loc={home.pk}&range=7d")

    assert text(by_testid(page, "compare-overlap-single")) == (
        "Pick two or more locations to see when their outages overlap."
    )
    assert not all_by_testid(page, "compare-matrix")
    assert not all_by_testid(page, "compare-simultaneous")
    assert not all_by_testid(page, "pair-card")
    assert not all_by_testid(page, "sim-card")
    lanes = all_by_testid(page, "tl-lane")
    assert {lane["data-location-id"] for lane in lanes} == {str(home.pk)}
    assert len(lanes) == 7
    hour_keys = {cell["data-location-id"] for cell in all_by_testid(page, "hour-cell")}
    assert hour_keys == {str(home.pk)}
    headers, _rows = table(page, "compare-weeks-table")
    assert headers == ["Week", "Home"]


@pytest.mark.django_db
def test_compare_page_hours(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin)

    chart = by_testid(page, "compare-hours-chart")
    assert (chart.name, chart["role"]) == ("svg", "img")
    assert _direct_title(chart)
    cells = {
        (str(cell["data-location-id"]), str(cell["data-hour"])): cell
        for cell in all_by_testid(chart, "hour-cell")
    }
    assert len(cells) == 4 * 24
    assert cells[(str(home.pk), "12")]["data-pct"] == "14"
    assert _direct_title(cells[(str(home.pk), "12")]) == (
        "Home · 12:00–13:00 · off 14% of monitored time (1h of 7h)"
    )
    assert cells[(str(office.pk), "3")]["data-pct"] == "0"
    assert cells[(str(office.pk), "3")]["fill-opacity"] == "0.35"
    assert cells[(str(shop.pk), "12")]["data-pct"] == "7"
    peaks = {
        str(peak["data-location-id"]): text(peak) for peak in all_by_testid(chart, "hour-peak")
    }
    assert peaks[str(home.pk)] == "peak 09–10 · 14%"
    assert peaks["together"] == "peak 12–13 · 7%"
    # The printed shares are plain text: no halo stroke behind any number.
    assert "14" in [text(label) for label in chart.find_all("text")]
    assert not [
        label
        for label in chart.find_all("text")
        if label.has_attr("stroke-width") or label.has_attr("paint-order")
    ]
    assert text(by_testid(page, "compare-hours-hint")) == "Scroll sideways to see the whole day."
    assert text(by_testid(page, "compare-hours-legend")).startswith(
        "Red: share of monitored time the power was off in that hour (darker = more). "
        "Green: power always on in that hour. Grey: not monitored."
    )


@pytest.mark.django_db
def test_compare_page_weeks(admin: Client, location_factory: Callable[..., Any]) -> None:
    home, office, shop = _fixture(location_factory)

    page = _page(admin)

    headers, rows = table(page, "compare-weeks-table")
    assert headers == ["Week", "Home", "Office", "Shop", "All off together"]
    assert [row[0] for row in rows] == ["28.09 – 04.10 partial", "21.09 – 27.09 partial"]
    assert len(all_by_testid(page, "week-partial")) == 2
    cells = {
        (str(cell["data-week"]), str(cell["data-location-id"])): cell
        for cell in all_by_testid(page, "week-cell")
    }
    assert text(cells[("2026-09-28", str(home.pk))]) == "2h 30m · 2"
    assert text(cells[("2026-09-28", str(shop.pk))]) == "3h 30m · 2"
    assert text(cells[("2026-09-28", "together")]) == "30m · 1"
    assert text(cells[("2026-09-21", str(home.pk))]) == "no outages"
    assert cells[("2026-09-28", str(office.pk))]["data-off-us"] == str(60 * 60_000_000)


@pytest.mark.django_db
def test_compare_page_together_zero_case(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # WR-01: Home is off (09:00-10:00 local on 28.09), Barn is on all the time. They were
    # monitored together but never all off at once: "no overlap", never "no outages" or
    # a green "always on" Together cell.
    home = _home(location_factory)
    barn = location_factory(name="Barn", bot_token=TOKEN)
    _store(barn, ("on", START, None, None))
    _live(barn, "on", last_heartbeat_at=_t(10, 1, 15, 59), on_since=START)

    page = _page(admin, f"?loc={home.pk}&loc={barn.pk}")

    assert _value(_kpis(page)["all-off"]) == "no overlap"
    assert _total(page, "2026-09-28", "together") == "no overlap"
    chart = by_testid(page, "compare-hours-chart")
    together = {
        str(cell["data-hour"]): cell
        for cell in all_by_testid(chart, "hour-cell")
        if cell["data-location-id"] == "together"
    }
    nine = together["9"]
    assert "fill-cmp-track" in nine["class"] and "fill-st-on" not in nine["class"]
    assert _direct_title(nine) == "Together (all off) · 09:00–10:00 · never all off at once"
    assert all("fill-st-on" not in cell["class"] for cell in together.values())
    peaks = {
        str(peak["data-location-id"]): text(peak) for peak in all_by_testid(chart, "hour-peak")
    }
    assert (peaks["together"], peaks[str(home.pk)]) == ("no overlap", "peak 09–10 · 14%")
    # Home's own lane still shows its off hour in the off colour.
    home_nine = [
        cell
        for cell in all_by_testid(chart, "hour-cell")
        if (cell["data-location-id"], cell["data-hour"]) == (str(home.pk), "9")
    ]
    assert "fill-st-off" in home_nine[0]["class"]
    assert text(by_testid(page, "compare-hours-legend")).endswith(
        "Together: red = all selected locations off at once; grey = never all off at once, "
        "or not monitored together."
    )
    weeks = {
        (str(cell["data-week"]), str(cell["data-location-id"])): text(cell)
        for cell in all_by_testid(page, "week-cell")
    }
    assert weeks[("2026-09-28", "together")] == "no overlap"
    assert weeks[("2026-09-28", str(barn.pk))] == "no outages"


@pytest.mark.django_db
def test_compare_page_one_location_has_no_together_legend(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    home = _home(location_factory)
    _office(location_factory)

    page = _page(admin, f"?loc={home.pk}")

    assert "Together" not in text(by_testid(page, "compare-hours-legend"))


@pytest.mark.django_db
def test_compare_page_simultaneous_cut_by_the_period(
    admin: Client, location_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # WR-02, the reviewer's probe: both off since 31.10 22:00 local (20:00 UTC) and still
    # off at now (03.11); the period is the single day 01.11.
    _clock(monkeypatch, datetime(2026, 11, 3, 10, 0, tzinfo=UTC))
    since = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    for name in ("Home", "Office"):
        location = location_factory(name=name, bot_token=TOKEN)
        _store(location, ("on", START, since, None), ("off", since, None, since))
        _live(location, "off", last_heartbeat_at=since, outage_started_at=since)

    page = _page(admin, "?range=custom&from=2026-11-01&to=2026-11-01")

    (row,) = all_by_testid(page, "sim-row")
    assert [text(cell) for cell in row.find_all("td")][:3] == [
        "before Sun 01.11 00:00",
        "after Mon 02.11 00:00",
        "≥ 24h",
    ]
    (card,) = all_by_testid(page, "sim-card")
    assert _terms(card)[:3] == [
        ("Start", "before Sun 01.11 00:00"),
        ("End", "after Mon 02.11 00:00"),
        ("Duration", "≥ 24h"),
    ]


@pytest.mark.django_db
def test_compare_page_escapes_names_in_the_charts(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    home = _home(location_factory, MARKUP_NAME)
    _shop(location_factory)

    response = admin.get(URL)
    page = assert_page(response, title="Compare locations", app=True)
    body = response.content.decode()

    assert "<b>x</b>" not in body
    assert_no_injected_script(body, "compare charts")
    lane = _lane(page, "2026-10-01", str(home.pk))
    assert _direct_title(lane) == MARKUP_NAME
    name_text = lane.find("text", recursive=False)
    assert isinstance(name_text, Tag) and MARKUP_NAME.startswith(text(name_text).rstrip("…"))
    matrix = by_testid(page, "compare-matrix")
    heads = [str(th.get("title")) for th in matrix.find_all("th", attrs={"scope": "col"})]
    assert MARKUP_NAME in heads
    segs = [_direct_title(seg) for seg in all_by_testid(lane, "tl-seg")]
    assert segs and all(title.startswith(f"{MARKUP_NAME} · ") for title in segs)


@pytest.mark.django_db
def test_compare_page_svgs_are_titled_or_hidden(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    _fixture(location_factory)

    page = _page(admin)

    titled = [svg for svg in main(page).find_all("svg") if svg.find("title", recursive=False)]
    assert [svg.get("data-testid") for svg in titled] == [
        "compare-timeline-chart",
        "compare-hours-chart",
    ]
    for svg in main(page).find_all("svg"):
        if svg in titled:
            continue
        assert (svg.get("aria-hidden"), svg.get("focusable")) == ("true", "false")
    # The shared defs exist once: the lane clip and the hatch pattern.
    assert len(page.find_all(id="cmp-lane-clip")) == 1
    assert len(page.find_all(id="cmp-hatch")) == 1


@pytest.mark.django_db
def test_compare_page_invalid_has_no_charts(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    _fixture(location_factory)

    page = _page(admin, "?range=year")

    for hook in ("compare-timeline-chart", "compare-hours-chart", "compare-matrix"):
        assert not all_by_testid(page, hook), hook
    assert page.find(id="cmp-lane-clip") is None
