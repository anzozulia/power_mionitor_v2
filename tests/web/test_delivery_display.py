"""Delivery health on the location list (LOC-03, OPS-03; D-10, D-13, UI-D6; UI-SPEC screen A).

- The list's fourth column, Delivery, shows "OK" as plain text while the location has no
  open ``delivery_failing`` incident, and "Failing since {time} ({code})" with the failing
  dot while it has one. The open incident is the badge's single source (D-10).
- UI-D6: ``{time}`` is HH:MM in the display TZ when the incident started on today's local
  date, else YYYY-MM-DD HH:MM; minutes are truncated, never rounded. ``{code}`` is
  ``http_{status}`` of the refusal the incident describes.
- One query reads the incidents of every listed location; nothing is read per row.
- E1: zero locations give the P1 empty panel, one and twenty the same table with no
  pagination and no count; no script; the table scrolls inside its wrapper.

The list view takes an injected clock (``LocationListView.as_view(clock=...)``) for the
"today" decision, through RequestFactory as in tests/web/test_locations.py (LOC-02).
"""

# class-guard: pending migration

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeClock
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from powermon.alerts import delivery
from powermon.web import status
from powermon.web.views import LocationListView

User = get_user_model()

KYIV = ZoneInfo("Europe/Kyiv")
# Now on the list: 2026-10-02 12:00 in Kyiv (09:00 UTC).
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=KYIV)
CSS_PATH = Path(settings.BASE_DIR) / "powermon" / "web" / "static" / "web" / "app.css"


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


def _text(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def _headers(html: str) -> list[str]:
    return re.findall(r'<th scope="col">([^<]*)</th>', html)


def _rows(html: str) -> list[list[str]]:
    """Each body row's cells as raw HTML."""
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    assert body is not None, "no table body in the page"
    rows = re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S)
    return [re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S) for row in rows]


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
    today = location_factory(name="B today")
    older = location_factory(name="C older")
    _fail(today, _local(2, 14, 5, 59), 403)
    _fail(older, _local(1, 23, 59, 59), 401)
    now = _local(2, 15, 0)

    with CaptureQueriesContext(connection) as queries:
        html = _list(rf, now)

    assert _headers(html) == ["Name", "Status", "Last heartbeat", "Delivery"]
    cells = _rows(html)
    assert [len(row) for row in cells] == [4, 4, 4]
    # "OK" is plain text with no status dot (UI-SPEC screen A).
    assert cells[0][3] == "OK"
    assert (
        cells[1][3] == '<span class="status status--failing">Failing since 14:05 (http_403)</span>'
    )
    assert cells[2][3] == (
        '<span class="status status--failing">Failing since 2026-10-01 23:59 (http_401)</span>'
    )
    # A failure shows only in its own row's Delivery cell (E1 error).
    assert html.count("status--failing") == 2
    # One query for the incidents of all rows, none per row.
    incident_reads = [q["sql"] for q in queries.captured_queries if '"ops_incident"' in q["sql"]]
    assert len(incident_reads) == 1


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

    assert _text(_rows(failing)[0][3]) == "Failing since 09:00 (http_400)"

    with transaction.atomic():
        delivery.close_failing(location.pk, _local(2, 10, 0))

    assert _rows(_list(rf, _local(2, 10, 1)))[0][3] == "OK"


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


# E1: zero, one and many rows; no script; overflow


@pytest.mark.django_db
def test_list_empty_and_single_and_many(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    empty = admin.get("/").content.decode()

    assert "<h2>No locations yet</h2>" in empty
    assert empty.count("btn--primary") == 1
    assert "<table" not in empty
    assert "<script" not in empty

    location_factory(name="Only")
    single = admin.get("/").content.decode()
    for n in range(19):
        location_factory(name=f"Place {n:02d}")
    many = admin.get("/").content.decode()

    for html, count in ((single, 1), (many, 20)):
        assert html.count("<table") == 1
        assert len(_rows(html)) == count
        assert all(row[3] == "OK" for row in _rows(html))
        assert re.search(r'<div class="table-wrap">\s*<table>', html)
        # No pagination and no count text, whatever the number of rows.
        assert "page=" not in html
        assert str(count) not in _text(html.split("<tbody>")[0])
        assert "<script" not in html
        assert "No locations yet" not in html


@pytest.mark.django_db
def test_list_long_name_and_cells_keep_their_classes(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "y" * 100
    location = location_factory(name=name)

    html = admin.get("/").content.decode()

    [row] = _rows(html)
    # The 100-character name is shown whole in its .name link; Last heartbeat keeps .num.
    assert row[0] == f'<a class="name" href="/locations/{location.pk}/">{name}</a>'
    assert '<td class="num">Never</td>' in html
    css = CSS_PATH.read_text(encoding="utf-8")
    assert ".table-wrap { overflow-x: auto; }" in css
    assert re.search(r"\.num \{[^}]*white-space: nowrap", css)
    assert re.search(r"\.status-cell \{[^}]*flex-wrap: wrap", css, re.S)
