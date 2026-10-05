"""The sidebar location list: a lazy, anonymous-safe context processor (UI-03, R3, R4, R11;
TEST-STRATEGY §8.5).

- ``context_processors.sidebar`` returns nothing for an anonymous request (the sign-in
  page, an anonymous 404 or 403-CSRF, which Django renders with the request: R11) and for
  a bare RequestFactory request without a ``user`` attribute, and runs no query for either.
- For a signed-in request it returns a lazy object: the call runs no query, and reading it
  runs the location list's query plus at most one incident query, the same count for 1
  and for 6 locations. A response that renders no template (the status JSON, the chart
  PNG) never pays for it.
- Its rows are frozen ``SidebarRow`` instances with exactly the display fields (pk, name,
  status, label, delivery_failing, last_heartbeat_at, outage_started_at), in list order
  (``Lower(name)``, then pk), deleted locations excluded: never a ``Location`` with its
  token or key (R3, R4). ``current_pk`` comes from the resolved URL's ``pk``; the counts
  are the fleet tiles' (UI-04).

The rendered half (``partials/sidebar.html`` on the shell probe of ``urls_shell``, or
rendered alone for a resolved request; 06-UI-SPEC Layout Shell › Sidebar, shell.sb_* copy):

- One ``sidebar-location`` link per non-deleted location, in list order, with
  ``data-location-id``, ``data-status``, ``data-delivery``, ``title`` = the full name and
  the escaped name as its text; ``aria-current="page"`` only on the location the page is
  about, and on the active main-nav item elsewhere.
- Per row: the screen-reader sentence ``[data-live=sidebar-sr]`` (shell.sb_sr), the
  aria-hidden mono cell ``[data-live=sidebar-cell]`` with ``data-cell`` and
  ``data-since`` (shell.sb_cell), and the failing triangle ``[data-live=sidebar-fail]``,
  rendered ``hidden`` while delivery is OK. The cells use the processor's clock, the same
  instant as the layout's ``data-now``.
- The group: its count ``[data-live=sidebar-count]``, the aria-hidden summary
  ``sidebar-summary`` (shell.sb_summary) with its sr sentence ``[data-live=summary-sr]``,
  or ``sidebar-empty`` and no summary with no location; the ``ops-chat-warning`` chip only
  while the ops chat is not configured.

``context_processors.CLOCK`` is the module's clock; tests replace it with a ``FakeClock``.
"""

import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import connection, transaction
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils.functional import SimpleLazyObject
from pages import all_by_testid, assert_page, by_testid, parse, text
from urls_shell import PROBE_PATH, PROBE_TITLE

from powermon.alerts import delivery
from powermon.engine.models import LocationState
from powermon.locations.models import Location
from powermon.web import context_processors
from powermon.web.context_processors import SidebarData, SidebarRow, build_sidebar, sidebar

User = get_user_model()

ROW_FIELDS = {
    "pk",
    "name",
    "status",
    "label",
    "delivery_failing",
    "last_heartbeat_at",
    "outage_started_at",
}
DISTINCT_NAME = "Sidebar Probe Zhytomyr"
SIDEBAR_PROCESSOR = "powermon.web.context_processors.sidebar"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> FakeClock:
    """The processor's clock, fixed at 2026-10-01 08:00 UTC."""
    fake = FakeClock(fixed_now)
    monkeypatch.setattr(context_processors, "CLOCK", fake)
    return fake


def _signed_in(path: str = "/") -> Any:
    """A request for ``path`` from the signed-in admin, resolved as the URL would be."""
    request = RequestFactory().get(path)
    request.user = User.objects.get_or_create(username="admin")[0]
    request.resolver_match = resolve(path)
    return request


def _location_queries(queries: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in queries.captured_queries if '"location"' in q["sql"]]


# Anonymous and bare requests (R11)


@pytest.mark.django_db
def test_UI03_sidebar_processor_anonymous(django_assert_num_queries: Any) -> None:
    request = RequestFactory().get("/login/")
    request.user = AnonymousUser()

    with django_assert_num_queries(0):
        assert sidebar(request) == {}


@pytest.mark.django_db
def test_UI03_sidebar_processor_bare_request(django_assert_num_queries: Any) -> None:
    # The shape view tests render pages with: no user attribute, no resolver match.
    request = RequestFactory().get("/")
    assert not hasattr(request, "user")
    assert getattr(request, "resolver_match", None) is None

    with django_assert_num_queries(0):
        assert sidebar(request) == {}


@pytest.mark.django_db
def test_UI03_sign_in_page_has_no_sidebar(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location_factory(name=DISTINCT_NAME)

    with CaptureQueriesContext(connection) as queries:
        page = client.get("/login/")

    assert page.status_code == 200
    assert "sidebar" not in page.context
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in page.content.decode()


@pytest.mark.django_db
def test_UI03_anonymous_csrf_failure_runs_no_location_query(
    location_factory: Callable[..., Any],
) -> None:
    # CsrfViewMiddleware comes before LoginRequiredMiddleware: an anonymous POST that fails
    # the CSRF check renders 403_csrf.html with the request, so every processor runs (R11).
    location_factory(name=DISTINCT_NAME)
    browser = Client(enforce_csrf_checks=True)

    with CaptureQueriesContext(connection) as queries:
        response = browser.post("/locations/new/", {"name": "x"})

    assert response.status_code == 403
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in response.content.decode()


# Signed in: lazy, constant, frozen display rows (UI-03, R4)


@pytest.mark.django_db
def test_UI03_sidebar_processor_lazy_and_constant(
    clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    first = location_factory(name="b")
    request = _signed_in()

    with CaptureQueriesContext(connection) as calling:
        context = sidebar(request)

    assert calling.captured_queries == []
    assert set(context) == {"sidebar"}
    # Not evaluated yet: type() does not unwrap the lazy object.
    assert type(context["sidebar"]) is SimpleLazyObject

    with CaptureQueriesContext(connection) as one:
        rows = context["sidebar"].rows
    assert [row.pk for row in rows] == [first.pk]

    for i in range(5):
        location = location_factory(name=f"L{i}")
        with transaction.atomic():
            delivery.open_failing(location.pk, fixed_now - timedelta(minutes=i + 1), 403)

    lazy = sidebar(_signed_in())["sidebar"]
    with CaptureQueriesContext(connection) as six:
        six_rows = lazy.rows
    assert len(six_rows) == 6
    # The location list's query plus one incident query, whatever the number of rows.
    assert len(one.captured_queries) == len(six.captured_queries) == 2


@pytest.mark.django_db
def test_UI03_sidebar_rows_are_frozen_display_rows(
    clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    lower_b = location_factory(name="b")
    upper_a = location_factory(name="A")
    lower_a = location_factory(name="a")
    location_factory(name="Gone", deleted_at=fixed_now)
    off_since = fixed_now - timedelta(minutes=7)
    LocationState.objects.filter(location=lower_a).update(
        status="off", outage_started_at=off_since, last_heartbeat_at=off_since
    )
    Location.objects.filter(pk=lower_b.pk).update(maintenance=True)
    with transaction.atomic():
        delivery.open_failing(lower_a.pk, fixed_now - timedelta(minutes=3), 403)

    data = build_sidebar(_signed_in())

    assert type(data) is SidebarData
    assert {field.name for field in dataclasses.fields(SidebarRow)} == ROW_FIELDS
    assert all(type(row) is SidebarRow for row in data.rows)
    with pytest.raises(dataclasses.FrozenInstanceError):
        data.rows[0].name = "changed"  # type: ignore[misc]
    # Lower(name), then the lower id on ties; the deleted location is not listed.
    assert [row.pk for row in data.rows] == [upper_a.pk, lower_a.pk, lower_b.pk]
    a = data.rows[1]
    assert (a.status, a.label, a.delivery_failing) == ("off", "Off", True)
    assert (a.last_heartbeat_at, a.outage_started_at) == (off_since, off_since)
    b = data.rows[2]
    assert (b.status, b.label, b.delivery_failing) == ("maintenance", "Maintenance", False)
    assert (b.last_heartbeat_at, b.outage_started_at) == (None, None)
    # The fleet tiles' counts and the processor's clock.
    assert data.counts == {"on": 0, "off": 1, "maintenance": 1, "waiting": 1, "failing": 1}
    assert data.now == fixed_now
    assert data.ops_configured is False
    # The list page has no current location.
    assert data.current_pk is None


@pytest.mark.django_db
def test_UI03_sidebar_current_pk_from_the_url(
    clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    for path in (
        f"/locations/{location.pk}/",
        f"/locations/{location.pk}/edit/",
        f"/locations/{location.pk}/setup/",
        f"/locations/{location.pk}/delete/",
    ):
        assert build_sidebar(_signed_in(path)).current_pk == location.pk, path
    # No resolver match (a bare request): no current location, no exception.
    bare = RequestFactory().get(f"/locations/{location.pk}/")
    assert build_sidebar(bare).current_pk is None


@pytest.mark.django_db
def test_UI03_sidebar_ops_configured(
    clock: FakeClock, ops_settings: Any, location_factory: Callable[..., Any]
) -> None:
    location_factory()

    assert build_sidebar(_signed_in()).ops_configured is True


@pytest.mark.django_db
def test_UI03_signed_in_page_carries_the_lazy_sidebar(
    clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    client = Client()
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    page = client.get(f"/locations/{location.pk}/")

    assert page.status_code == 200
    data = page.context["sidebar"]
    assert [row.name for row in data.rows] == ["Office"]
    assert data.current_pk == location.pk


@pytest.mark.django_db
def test_UI03_json_and_png_run_no_sidebar_query(
    settings: Any, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    client = Client()
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    urls = ("/locations/status.json", f"/locations/{location.pk}/chart.png")

    def capture() -> list[CaptureQueriesContext]:
        captured = []
        for url in urls:
            # An empty render cache, so both runs of the chart PNG do the same work.
            cache.clear()
            with CaptureQueriesContext(connection) as queries:
                assert client.get(url).status_code == 200, url
            captured.append(queries)
        cache.clear()
        return captured

    registered = capture()
    processors = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
    assert SIDEBAR_PROCESSOR in processors
    without = [name for name in processors if name != SIDEBAR_PROCESSOR]
    settings.TEMPLATES = [
        {**settings.TEMPLATES[0], "OPTIONS": {"context_processors": without}},
    ]
    unregistered = capture()

    # Neither renders a template, so the sidebar never runs: the same queries either way,
    # and one location query each (the live rows' and the PNG's 404 check).
    for url, with_sidebar, without_sidebar in zip(urls, registered, unregistered, strict=True):
        assert len(with_sidebar.captured_queries) == len(without_sidebar.captured_queries), url
        assert len(_location_queries(with_sidebar)) == 1, url


# The rendered sidebar (partials/sidebar.html; UI-03, UI-12, R1, R11)

LONG_NAME = ("Very long location name " * 5)[:100]
SCRIPT_NAME = "<script>alert(1)</script>"


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _row(
    pk: int,
    name: str,
    status: str,
    *,
    failing: bool = False,
    heartbeat: datetime | None = None,
    outage: datetime | None = None,
) -> SidebarRow:
    labels = {"on": "On", "off": "Off", "maintenance": "Maintenance", "waiting": "Waiting"}
    return SidebarRow(pk, name, status, labels[status], failing, heartbeat, outage)


def _data(rows: list[SidebarRow], now: datetime) -> SidebarData:
    """SidebarData as the processor builds it: the counts from the rows, nothing current."""
    counts = dict.fromkeys(("on", "off", "maintenance", "waiting", "failing"), 0)
    for row in rows:
        counts[row.status] += 1
        counts["failing"] += row.delivery_failing
    return SidebarData(tuple(rows), counts, None, now, True)


def _render(data: SidebarData) -> Any:
    """``partials/sidebar.html`` alone, for ``data`` (no request: nothing is current)."""
    return parse(render_to_string("partials/sidebar.html", {"sidebar": data}))


def _live(link: Any, kind: str) -> Any:
    """The one ``[data-live=<kind>]`` element inside a sidebar link."""
    found = link.select(f'[data-live="{kind}"]')
    assert len(found) == 1, (kind, len(found))
    return found[0]


def _group(soup: Any, kind: str) -> list[Any]:
    """Every ``[data-live=<kind>]`` element of the page (the group's count and summary)."""
    return list(soup.select(f'[data-live="{kind}"]'))


@pytest.mark.django_db
@pytest.mark.urls("urls_shell")
def test_UI03_sidebar_rows(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    # Two names that differ only in case keep pk order; the deleted one is not listed.
    lower = location_factory(name="a")
    upper_b = location_factory(name="B")
    upper_a = location_factory(name="A")
    location_factory(name=DISTINCT_NAME, deleted_at=fixed_now)
    LocationState.objects.filter(location=upper_b).update(
        status="off", outage_started_at=fixed_now - timedelta(hours=1)
    )
    Location.objects.filter(pk=upper_a.pk).update(maintenance=True)
    with transaction.atomic():
        delivery.open_failing(lower.pk, fixed_now - timedelta(minutes=3), 403)

    soup = assert_page(admin.get(PROBE_PATH), title=PROBE_TITLE, app=True)

    links = all_by_testid(soup, "sidebar-location")
    assert [
        (link["data-location-id"], link["data-status"], link["data-delivery"], link["title"])
        for link in links
    ] == [
        (str(lower.pk), "waiting", "failing", "a"),
        (str(upper_a.pk), "maintenance", "ok", "A"),
        (str(upper_b.pk), "off", "ok", "B"),
    ]
    assert DISTINCT_NAME not in str(soup)
    # Every per-location live element names its location (the poll's targets, UI-05).
    for link in links:
        for kind in ("sidebar-sr", "sidebar-cell", "sidebar-fail"):
            assert _live(link, kind)["data-location-id"] == link["data-location-id"], kind


@pytest.mark.django_db
def test_UI03_sidebar_cells(fixed_now: datetime) -> None:
    now = fixed_now
    data = _data(
        [
            _row(1, "Alpha", "on", failing=True, heartbeat=now - timedelta(seconds=12)),
            _row(2, "Bravo", "off", outage=now - timedelta(hours=4)),
            # An Off location whose outage start is not known.
            _row(3, "Charlie", "off"),
            _row(4, "Delta", "maintenance", heartbeat=now - timedelta(minutes=1)),
            _row(5, "Echo", "waiting"),
            _row(6, "Foxtrot", "on", heartbeat=now - timedelta(days=3)),
        ],
        now,
    )

    links = all_by_testid(_render(data), "sidebar-location")

    cells = [_live(link, "sidebar-cell") for link in links]
    # aria-hidden mono text (shell.sb_cell), with what the relative component reads.
    assert [cell["aria-hidden"] for cell in cells] == ["true"] * 6
    assert [(cell["data-cell"], cell.get_text()) for cell in cells] == [
        ("age", "12s"),
        ("off", "OFF 4h"),
        ("off", "OFF"),
        ("mnt", "MNT"),
        ("wait", "—"),
        ("age", "3d"),
    ]
    since = [cell["data-since"] for cell in cells]
    assert since[2:5] == ["", "", ""]
    assert [datetime.fromisoformat(since[i]) for i in (0, 1, 5)] == [
        now - timedelta(seconds=12),
        now - timedelta(hours=4),
        now - timedelta(days=3),
    ]
    # The screen-reader sentence after the name (shell.sb_sr).
    assert [_live(link, "sidebar-sr").get_text() for link in links] == [
        ", On, last heartbeat 12 s ago, delivery failing",
        ", Off for 4 h",
        ", Off",
        ", Maintenance",
        ", Waiting for first heartbeat",
        ", On, last heartbeat 3 d ago",
    ]
    assert text(links[0]) == "Alpha, On, last heartbeat 12 s ago, delivery failing"
    # The failing triangle is always rendered, hidden while delivery is OK.
    hidden = [_live(link, "sidebar-fail").has_attr("hidden") for link in links]
    assert hidden == [False, True, True, True, True, True]
    assert [(link["data-status"], link["data-delivery"]) for link in links] == [
        ("on", "failing"),
        ("off", "ok"),
        ("off", "ok"),
        ("maintenance", "ok"),
        ("waiting", "ok"),
        ("on", "ok"),
    ]


@pytest.mark.django_db
@pytest.mark.urls("urls_shell")
def test_UI03_sidebar_cells_use_the_pages_now(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    # Only the processor's clock is fixed: the cells and body data-now share that instant.
    on = location_factory(name="On place")
    off = location_factory(name="Off place")
    LocationState.objects.filter(location=on).update(
        status="on", last_heartbeat_at=fixed_now - timedelta(seconds=12), on_since=fixed_now
    )
    LocationState.objects.filter(location=off).update(
        status="off", outage_started_at=fixed_now - timedelta(hours=4)
    )

    soup = parse(admin.get(PROBE_PATH))

    assert datetime.fromisoformat(soup.find("body")["data-now"]) == fixed_now
    links = all_by_testid(soup, "sidebar-location")
    assert [_live(link, "sidebar-cell").get_text() for link in links] == ["OFF 4h", "12s"]
    assert [_live(link, "sidebar-sr").get_text() for link in links] == [
        ", Off for 4 h",
        ", On, last heartbeat 12 s ago",
    ]


@pytest.mark.django_db
def test_UI03_sidebar_summary_and_empty(fixed_now: datetime) -> None:
    now = fixed_now
    failing = _render(
        _data(
            [
                _row(1, "A", "on", failing=True, heartbeat=now),
                _row(2, "B", "on", heartbeat=now),
                _row(3, "C", "off", outage=now),
                _row(4, "D", "waiting"),
            ],
            now,
        )
    )
    healthy = _render(_data([_row(1, "A", "on", heartbeat=now), _row(2, "B", "off")], now))
    one = _render(_data([_row(1, "A", "maintenance")], now))
    empty = _render(_data([], now))

    # Expected: the summary (shell.sb_summary, aria-hidden) and its sr sentence.
    summary = by_testid(failing, "sidebar-summary")
    assert (summary["aria-hidden"], summary["data-live"]) == ("true", "summary")
    assert summary.get_text() == "2 on · 1 off · 1 fail"
    assert [sr.get_text() for sr in _group(failing, "summary-sr")] == [
        "2 on, 1 off, 1 with delivery failing"
    ]
    assert [count.get_text() for count in _group(failing, "sidebar-count")] == ["4"]
    # The fail part only when failing > 0.
    assert by_testid(healthy, "sidebar-summary").get_text() == "1 on · 1 off"
    assert [sr.get_text() for sr in _group(healthy, "summary-sr")] == ["1 on, 1 off"]
    # One location: count 1 and a summary; zero: the empty line, count 0, no summary.
    assert [count.get_text() for count in _group(one, "sidebar-count")] == ["1"]
    assert by_testid(one, "sidebar-summary").get_text() == "0 on · 0 off"
    assert all_by_testid(one, "sidebar-empty") == []
    assert text(by_testid(empty, "sidebar-empty")) == "No locations yet"
    assert [count.get_text() for count in _group(empty, "sidebar-count")] == ["0"]
    assert all_by_testid(empty, "sidebar-summary") == []
    assert _group(empty, "summary-sr") == []
    assert all_by_testid(empty, "sidebar-location") == []


@pytest.mark.django_db
@pytest.mark.urls("urls_shell")
def test_UI03_sidebar_escaping_and_long_names(
    admin: Client, clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location_factory(name=SCRIPT_NAME)
    location_factory(name=LONG_NAME)

    # assert_page also proves the name never runs as a script (R1).
    soup = assert_page(admin.get(PROBE_PATH), title=PROBE_TITLE, app=True)

    links = all_by_testid(soup, "sidebar-location")
    by_title = {link["title"]: link for link in links}
    assert set(by_title) == {SCRIPT_NAME, LONG_NAME}
    for name, link in by_title.items():
        # The whole name, escaped, as the title and as the start of the accessible name.
        assert text(link).startswith(f"{name}, "), name
        assert link.find("script") is None


@pytest.mark.django_db
def test_UI03_aria_current(clock: FakeClock, location_factory: Callable[..., Any]) -> None:
    location_factory(name="Other")
    target = location_factory(name="Target")
    base = f"/locations/{target.pk}/"
    paths = (
        base,
        f"{base}edit/",
        f"{base}setup/",
        f"{base}delete/",
        f"{base}setup/regenerate/",
        f"{base}outages/1790000000000000/remove/",
        f"{base}reset/",
    )

    for path in paths:
        soup = parse(render_to_string("partials/sidebar.html", request=_signed_in(path)))
        current = [
            link["data-location-id"]
            for link in all_by_testid(soup, "sidebar-location")
            if link.get("aria-current") == "page"
        ]
        assert current == [str(target.pk)], path
        for hook in ("nav-locations", "nav-add-location"):
            assert not by_testid(soup, hook).has_attr("aria-current"), (path, hook)

    # Elsewhere the active main-nav item is current, never a location.
    for path, active in (("/", "nav-locations"), ("/locations/new/", "nav-add-location")):
        soup = parse(render_to_string("partials/sidebar.html", request=_signed_in(path)))
        links = all_by_testid(soup, "sidebar-location")
        assert [link for link in links if link.has_attr("aria-current")] == [], path
        assert by_testid(soup, active)["aria-current"] == "page", path
        nav = [
            hook
            for hook in ("nav-locations", "nav-add-location")
            if by_testid(soup, hook).has_attr("aria-current")
        ]
        assert nav == [active], path


@pytest.mark.django_db
@pytest.mark.urls("urls_shell")
@pytest.mark.parametrize("configured", [False, True], ids=["ops-off", "ops-on"])
def test_UI03_ops_chip(
    request: pytest.FixtureRequest,
    admin: Client,
    clock: FakeClock,
    location_factory: Callable[..., Any],
    configured: bool,
) -> None:
    if configured:
        request.getfixturevalue("ops_settings")
    location_factory()

    soup = assert_page(admin.get(PROBE_PATH), title=PROBE_TITLE, app=True)

    chips = all_by_testid(soup, "ops-chat-warning")
    if configured:
        assert chips == []
        return
    assert len(chips) == 1
    chip = chips[0]
    assert chip.find_parent(attrs={"data-testid": "sidebar"}) is not None
    assert (chip.name, chip["href"], chip["title"], text(chip)) == (
        "a",
        "/",
        "Ops chat off",
        "Ops chat off",
    )
