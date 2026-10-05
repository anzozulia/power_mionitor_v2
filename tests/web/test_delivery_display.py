"""Delivery health on the location list (LOC-03, OPS-03; D-10, D-13, UI-D6; 06-UI-SPEC S3).

- The list's fourth column, Delivery, shows "OK" while the location has no open
  ``delivery_failing`` incident, and the failing pill "Failing since {time} ({code})" while
  it has one; the pill's text is ``live.delivery_text``. The open incident is the badge's
  single source (D-10). The row and the cell carry ``data-delivery`` ok or failing.
- UI-D6: ``{time}`` is HH:MM in the display TZ when the incident started on today's local
  date, else YYYY-MM-DD HH:MM; minutes are truncated, never rounded. ``{code}`` is
  ``http_{status}`` of the refusal the incident describes.
- The incidents of every listed location are read at once: the number of incident reads is
  the same for one row as for three, never one per row.
- E1: zero locations give the empty state, one and twenty the same table with no
  pagination; no injected script; a 100-character name shows whole.

The list view takes an injected clock (``LocationListView.as_view(clock=...)``) for the
"today" decision, through RequestFactory as in tests/web/test_locations.py (LOC-02). Rows
and cells are read only inside the locations table (06-18 adds the phone cards with the
same hooks), after dropping every element that carries the ``hidden`` attribute.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup, Tag
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from pages import all_by_testid, assert_no_injected_script, by_testid, main, parse, table, text

from powermon.alerts import delivery
from powermon.web import status
from powermon.web.live import delivery_text
from powermon.web.views import LocationListView

User = get_user_model()

KYIV = ZoneInfo("Europe/Kyiv")
# Now on the list: 2026-10-02 12:00 in Kyiv (09:00 UTC).
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=KYIV)
HEADERS = ["Name", "Status", "Last heartbeat", "Delivery"]


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


def _local(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """An aware instant in Kyiv local time, in October 2026."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=KYIV)


def _list(rf: RequestFactory, now: datetime) -> str:
    """The location list as the view renders it at ``now`` (injected clock)."""
    request = rf.get("/")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    response = LocationListView.as_view(clock=FakeClock(now))(request)
    assert response.status_code == 200
    return response.content.decode()


def _shown(html: str) -> BeautifulSoup:
    """The page parsed, without the elements that carry the ``hidden`` attribute."""
    soup = parse(html)
    for element in [found for found in soup.find_all(True) if found.has_attr("hidden")]:
        element.extract()
    return soup


def _rows(html: str) -> list[list[str]]:
    """The cell texts of each shown row of the locations table."""
    headers, rows = table(_shown(html), "locations-table")
    assert headers == HEADERS
    return rows


def _row_elements(html: str) -> list[Tag]:
    """The ``location-row`` elements of the locations table, in order."""
    return all_by_testid(by_testid(_shown(html), "locations-table"), "location-row")


def _delivery_cell(row: Tag) -> Tag:
    """The row's one delivery element."""
    [cell] = all_by_testid(row, "delivery")
    return cell


def _count(html: str) -> str | None:
    """The text of the meta count item ("3 locations"), or None when the page has none."""
    values = main(parse(html)).find_all(attrs={"data-count-value": True})
    if not values:
        return None
    item = values[0].find_parent("li")
    assert isinstance(item, Tag), "the meta count is not inside a meta line item"
    return text(item)


def _incident_reads(queries: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in queries.captured_queries if '"ops_incident"' in q["sql"]]


def _fail(location: Any, started_at: datetime, status: int = 403) -> None:
    """Open the location's delivery_failing incident as the relay does (D-10)."""
    with transaction.atomic():
        delivery.open_failing(location.pk, started_at, status)


# UI-D6: the time in "Failing since …"


def test_failing_since_text() -> None:
    tz = "Europe/Kyiv"
    # Today in the display TZ: HH:MM only, from the first second of the day.
    assert status.failing_since_text(_local(2, 0, 0, 0), NOW, tz) == "00:00"
    # Minutes are truncated, never rounded: 09:41:59 stays 09:41.
    assert status.failing_since_text(_local(2, 9, 41, 59), NOW, tz) == "09:41"
    # The last second of yesterday: the date too.
    assert status.failing_since_text(_local(1, 23, 59, 59), NOW, tz) == "2026-10-01 23:59"
    # UTC inputs are converted first: 2026-10-01 21:00 UTC is 00:00 on the 2nd in Kyiv.
    assert status.failing_since_text(datetime(2026, 10, 1, 21, 0, tzinfo=UTC), NOW, tz) == "00:00"
    # The day boundary is the display TZ's: in UTC the same instant is still the 1st.
    assert (
        status.failing_since_text(datetime(2026, 10, 1, 21, 0, tzinfo=UTC), NOW, "UTC")
        == "2026-10-01 21:00"
    )
    # Several days ago, and across the autumn DST change (EEST -> EET on 2026-10-25).
    assert status.failing_since_text(_local(28, 3, 5), _local(30, 8, 0), tz) == "2026-10-28 03:05"
    assert status.failing_since_text(_local(25, 3, 30), _local(25, 23, 0), tz) == "03:30"


@pytest.mark.parametrize(
    ("started_at", "now"),
    [
        (datetime(2026, 10, 2, 9, 0), NOW),  # noqa: DTZ001
        (_local(2, 9, 0), datetime(2026, 10, 2, 12, 0)),  # noqa: DTZ001
    ],
    ids=["naive-start", "naive-now"],
)
def test_failing_since_text_refuses_a_naive_time(started_at: datetime, now: datetime) -> None:
    with pytest.raises(ValueError, match="naive"):
        status.failing_since_text(started_at, now, "Europe/Kyiv")


# The Delivery column (D-13, UI-D6)


@pytest.mark.django_db
def test_list_delivery_column(
    rf: RequestFactory, kyiv: Any, location_factory: Callable[..., Any]
) -> None:
    location_factory(name="A healthy")
    now = _local(2, 15, 0)
    with CaptureQueriesContext(connection) as one:
        _list(rf, now)
    today = location_factory(name="B today")
    older = location_factory(name="C older")
    _fail(today, _local(2, 14, 5, 59), 403)
    _fail(older, _local(1, 23, 59, 59), 401)

    with CaptureQueriesContext(connection) as three:
        html = _list(rf, now)

    rows = _rows(html)
    assert [len(row) for row in rows] == [4, 4, 4]
    assert [row[3] for row in rows] == [
        "OK",
        "Failing since 14:05 (http_403)",
        "Failing since 2026-10-01 23:59 (http_401)",
    ]
    elements = _row_elements(html)
    assert [row["data-delivery"] for row in elements] == ["ok", "failing", "failing"]
    cells = [_delivery_cell(row) for row in elements]
    assert [cell["data-delivery"] for cell in cells] == ["ok", "failing", "failing"]
    # The failing pill's text is the list's delivery_text of the open incident; OK has none.
    failing = delivery.failing_incidents([today.pk, older.pk])
    for cell, location in zip(cells[1:], (today, older), strict=True):
        [label] = cell.find_all(attrs={"data-label": True})
        assert text(label) == delivery_text(failing[location.pk], now)
    assert cells[0].find_all(attrs={"data-label": True}) == []
    assert text(cells[0]) == "OK"
    # The incidents of all rows are read at once: as many reads for three rows as for one.
    assert len(_incident_reads(three)) == len(_incident_reads(one)) >= 1


@pytest.mark.django_db
def test_list_delivery_follows_the_open_incident_only(
    rf: RequestFactory, kyiv: Any, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    started = _local(2, 9, 0)
    _fail(location, started, 403)
    # A refusal while failing replaces the code the badge shows (latest refusal, D-10).
    _fail(location, started + timedelta(minutes=15), 400)

    failing = _list(rf, _local(2, 10, 0))

    assert _rows(failing)[0][3] == "Failing since 09:00 (http_400)"
    [row] = _row_elements(failing)
    assert row["data-delivery"] == "failing"

    with transaction.atomic():
        delivery.close_failing(location.pk, _local(2, 10, 0))

    closed = _list(rf, _local(2, 10, 1))
    assert _rows(closed)[0][3] == "OK"
    [row] = _row_elements(closed)
    assert row["data-delivery"] == "ok"
    assert _delivery_cell(row)["data-delivery"] == "ok"


@pytest.mark.django_db
def test_list_query_count_does_not_grow_with_the_rows(
    rf: RequestFactory, kyiv: Any, location_factory: Callable[..., Any]
) -> None:
    first = location_factory(name="one")
    _fail(first, _local(2, 9, 0))
    with CaptureQueriesContext(connection) as one:
        _list(rf, NOW)
    for n in range(4):
        _fail(location_factory(name=f"more {n}"), _local(2, 9, n))
    with CaptureQueriesContext(connection) as five:
        _list(rf, NOW)

    assert len(five.captured_queries) == len(one.captured_queries)


# E1: zero, one and many rows; no injected script; long names


@pytest.mark.django_db
def test_list_empty_and_single_and_many(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    empty = admin.get("/").content.decode()

    by_testid(parse(empty), "empty-state")
    assert all_by_testid(parse(empty), "locations-table") == []
    assert parse(empty).find("table") is None
    assert_no_injected_script(empty, "empty")

    location_factory(name="Only")
    single = admin.get("/").content.decode()
    for n in range(19):
        location_factory(name=f"Place {n:02d}")
    many = admin.get("/").content.decode()

    for html, count, noun in ((single, 1, "location"), (many, 20, "locations")):
        assert len(parse(html).find_all("table")) == 1
        rows = _rows(html)
        assert len(rows) == count
        assert all(row[3] == "OK" for row in rows)
        # No pagination; the meta line counts every row.
        assert "page=" not in html
        assert _count(html) == f"{count} {noun}"
        assert_no_injected_script(html, noun)
        assert all_by_testid(parse(html), "empty-state") == []


@pytest.mark.django_db
def test_list_long_name_and_cells_keep_their_classes(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "y" * 100
    location = location_factory(name=name)

    html = admin.get("/").content.decode()

    [row] = _row_elements(html)
    # The 100-character name is shown whole in its link and title; its row has no
    # heartbeat yet, so the cell reads Never with no time element.
    [link] = all_by_testid(row, "location-link")
    assert text(link) == name
    assert link["title"] == name
    assert link["href"] == f"/locations/{location.pk}/"
    [heartbeat] = all_by_testid(row, "last-heartbeat")
    assert text(heartbeat) == "Never"
    assert heartbeat.find_all("time") == []
