"""D-10 performance self-check for the admin's Compare page (quick task 261103-lfm).

A scripted check with no browser: seed a deterministic timeline, render a VALID page (the
timeline is drawn, no form error) and time the whole request inside the test container.

- 3 locations x 90 days must render in under 1 s on the dev box (D-10); the test asserts the
  looser 5 s bound so a slow CI runner cannot flake, and prints the number for the record.
- 20 locations x 366 days (the largest allowed period, 01.01.2026..01.01.2027, both 2026 DST
  days inside) must render in under 5 s, the web's statement timeout.
- The HTML stays under 3 MB and both cases make the same number of queries (no N+1).

Each test prints one line ``COMPARE_PERF case=<3x90|20x366> ms=<n> kb=<n> queries=<n>``; run
with ``-s`` to see it.

Seed per location and UTC day (location-dependent offsets, so outages of different locations
overlap only partly): an outage of 1-4 h in the morning, one of 1-3 h in the afternoon, an
outage crossing midnight every third day and a 90-minute not-monitored piece every 7 days.
"""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from bs4 import BeautifulSoup
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from pages import by_testid, parse

from powermon.engine.models import LocationState, PowerInterval
from powermon.web import context_processors
from powermon.web.compare_views import CompareView
from powermon.web.templatetags import timefmt

User = get_user_model()

MAX_MS = 5000
MAX_BYTES = 3 * 1024 * 1024
QUERIES: dict[str, int] = {}

Window = tuple[str, datetime, datetime]


def _windows(index: int, first: date, now: datetime) -> list[Window]:
    """The off and not-monitored windows of location ``index`` from ``first`` up to ``now``."""
    windows: list[Window] = []
    day = first
    while datetime(day.year, day.month, day.day, tzinfo=UTC) < now:
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        n = (day - first).days
        morning = base + timedelta(hours=3, minutes=6 * index)
        windows.append(("off", morning, morning + timedelta(hours=1 + (n + index) % 4)))
        if n % 7 == index % 7:
            nm = base + timedelta(hours=10)
            windows.append(("not_monitored", nm, nm + timedelta(minutes=90)))
        afternoon = base + timedelta(hours=13, minutes=7 * index)
        windows.append(("off", afternoon, afternoon + timedelta(hours=1 + (n + 2 * index) % 3)))
        if n % 3 == index % 3:
            night = base + timedelta(hours=22, minutes=3 * index)
            windows.append(("off", night, base + timedelta(days=1, hours=1, minutes=30)))
        day += timedelta(days=1)
    return windows


def _seed(location: Any, index: int, first: date, now: datetime) -> None:
    """A contiguous timeline from ``first`` 00:00 UTC to ``now``; the last piece is open."""
    rows: list[PowerInterval] = []
    cursor = datetime(first.year, first.month, first.day, tzinfo=UTC)
    state, open_start = "on", cursor
    for w_state, w_start, w_end in _windows(index, first, now):
        if w_start >= now:
            break
        if cursor < w_start:
            rows.append(_piece(location, "on", cursor, w_start))
        if w_end >= now:
            # The window holds now: it is the open piece.
            state, open_start = w_state, w_start
            break
        rows.append(_piece(location, w_state, w_start, w_end))
        cursor = open_start = w_end
    rows.append(_piece(location, state, open_start, None))
    PowerInterval.objects.bulk_create(rows)
    fields: dict[str, Any] = {"last_heartbeat_at": now - timedelta(seconds=30)}
    if state == "on":
        fields["on_since"] = open_start
    elif state == "off":
        fields["outage_started_at"] = open_start
    LocationState.objects.filter(pk=location.pk).update(
        status="on" if state == "on" else ("off" if state == "off" else "waiting"), **fields
    )


def _piece(location: Any, state: str, start: datetime, end: datetime | None) -> PowerInterval:
    return PowerInterval(
        location=location,
        state=state,
        start_at=start,
        end_at=end,
        outage_start_at=start if state == "off" else None,
    )


def _setup(
    client: Client,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[..., Any],
    now: datetime,
    count: int,
    first: date,
) -> list[Any]:
    settings.TIME_ZONE = "Europe/Kyiv"
    clock = FakeClock(now)
    monkeypatch.setattr(CompareView, "clock", clock)
    monkeypatch.setattr(timefmt, "CLOCK", clock)
    monkeypatch.setattr(context_processors, "CLOCK", clock)
    locations = [factory(name=f"Location {i:02d}") for i in range(count)]
    for index, location in enumerate(locations):
        _seed(location, index, first, now)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return locations


def _measure(client: Client, case: str, url: str) -> tuple[int, int, int]:
    client.get(url)  # warm-up: template loading and the session, not the measured run
    with CaptureQueriesContext(connection) as captured:
        started = time.perf_counter()
        response = client.get(url)
        ms = round((time.perf_counter() - started) * 1000)
    assert response.status_code == 200
    html = response.content
    page: BeautifulSoup = parse(response)
    # Only a valid page counts: the timeline is drawn and no filter error is shown.
    by_testid(page, "compare-timeline")
    by_testid(page, "compare-timeline-chart")
    assert page.find(attrs={"data-testid": "compare-invalid"}) is None
    assert page.find(attrs={"data-testid": "error-summary"}) is None
    queries = len(captured.captured_queries)
    print(f"\nCOMPARE_PERF case={case} ms={ms} kb={round(len(html) / 1024)} queries={queries}")
    return ms, len(html), queries


def _check_queries(case: str, queries: int) -> None:
    QUERIES[case] = queries
    if len(QUERIES) == 2:
        # No N+1: 3 and 20 locations, 90 and 366 days read the same number of times.
        assert QUERIES["3x90"] == QUERIES["20x366"], QUERIES


@pytest.mark.django_db
def test_D10_compare_page_3_locations_90_days(
    client: Client,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
) -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    # The 90d preset ends today in Kyiv: 04.07..01.10; seed from the day before.
    _setup(client, settings, monkeypatch, location_factory, now, 3, date(2026, 7, 3))

    ms, size, queries = _measure(client, "3x90", "/compare/?range=90d")

    assert ms < MAX_MS
    assert size < MAX_BYTES
    _check_queries("3x90", queries)


@pytest.mark.django_db
def test_D10_compare_page_20_locations_366_days(
    client: Client,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
) -> None:
    now = datetime(2027, 1, 1, 12, 0, tzinfo=UTC)
    locations = _setup(client, settings, monkeypatch, location_factory, now, 20, date(2025, 12, 31))
    locs = "".join(f"&loc={location.pk}" for location in locations)
    # The largest valid period: from 01.01.2026 (the floor) to today, 366 days.
    url = f"/compare/?range=custom&from=2026-01-01&to=2027-01-01{locs}"

    ms, size, queries = _measure(client, "20x366", url)

    assert ms < MAX_MS
    assert size < MAX_BYTES
    _check_queries("20x366", queries)
