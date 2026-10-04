"""Relative times next to absolute ones (UI-11; 06-UI-SPEC Components › Relative time).

- ``timefmt.relative_text`` floors the age: under 1 s or in the future "just now", then
  "{N} s ago", "{N} min ago" under 60 min, "{N} h ago" under 48 h, else "{N} d ago". The
  sidebar cell (``compact_age``) and its screen-reader words (``age_words``) use the same
  floors. Ages are UTC differences, so a DST change never shifts them; a naive datetime
  raises ValueError.
- The ``iso`` filter gives the ``<time datetime>`` value with the display-TZ offset, and ""
  for anything that is not an aware datetime.
- The tags read "now" from the template context when the view put it there, else from
  ``timefmt.CLOCK``, which the tests monkeypatch: no freezegun.
- The location page puts its injected clock's reading into the context as ``now``, the
  heartbeat URL from ``PUBLIC_BASE_URL`` (never the request's Host, R13) and the listed
  outages' total off time; the settings context carries only the public bot id (R3).
"""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeClock
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.template import Context, Template
from django.test import RequestFactory
from django.test.signals import template_rendered

from powermon.engine import history
from powermon.locations import examples
from powermon.locations.models import Location
from powermon.web import location_views
from powermon.web.templatetags import timefmt
from powermon.web.templatetags.timefmt import (
    age_parts,
    age_words,
    compact_age,
    iso,
    relative_text,
)

KYIV = "Europe/Kyiv"
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
BASE_URL = "https://power.example.org"
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"

# (age, relative text, sidebar cell, screen-reader words): 06-UI-SPEC shell.relative,
# shell.sb_cell and shell.sb_sr at each floor.
FLOORS = [
    (timedelta(seconds=0.4), "just now", "0s", "0 s"),
    (timedelta(seconds=-5), "just now", "0s", "0 s"),
    (timedelta(seconds=1), "1 s ago", "1s", "1 s"),
    (timedelta(seconds=59), "59 s ago", "59s", "59 s"),
    (timedelta(seconds=60), "1 min ago", "1m", "1 min"),
    (timedelta(minutes=59, seconds=59), "59 min ago", "59m", "59 min"),
    (timedelta(minutes=60), "1 h ago", "1h", "1 h"),
    (timedelta(hours=47, minutes=59), "47 h ago", "47h", "47 h"),
    (timedelta(hours=48), "2 d ago", "2d", "2 d"),
]
FLOOR_IDS = ["0.4s", "future", "1s", "59s", "60s", "59m59s", "60m", "47h59m", "48h"]


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so the test does not depend on the env file."""
    settings.TIME_ZONE = KYIV
    return settings


# The pure helpers


@pytest.mark.parametrize(("age", "text", "cell", "words"), FLOORS, ids=FLOOR_IDS)
def test_UI11_relative_text_floors(age: timedelta, text: str, cell: str, words: str) -> None:
    assert relative_text(NOW - age, NOW) == text


@pytest.mark.parametrize(("age", "text", "cell", "words"), FLOORS, ids=FLOOR_IDS)
def test_UI11_compact_age_and_words(age: timedelta, text: str, cell: str, words: str) -> None:
    assert compact_age(NOW - age, NOW) == cell
    assert age_words(NOW - age, NOW) == words


def test_UI11_age_parts_counts_and_units() -> None:
    assert age_parts(NOW - timedelta(hours=47, minutes=59, seconds=59), NOW) == (47, "h")
    assert age_parts(NOW - timedelta(days=9, hours=23), NOW) == (9, "d")
    assert age_parts(NOW + timedelta(days=1), NOW) == (0, "s")


def test_UI11_age_is_a_utc_difference_across_the_dst_change() -> None:
    # 2026-10-25 Kyiv repeats 03:00-04:00: the two 03:30 wall times are one hour apart.
    zone = ZoneInfo(KYIV)
    first = datetime(2026, 10, 25, 3, 30, tzinfo=zone)
    second = datetime(2026, 10, 25, 3, 30, fold=1, tzinfo=zone)

    assert relative_text(first, second) == "1 h ago"
    assert compact_age(first, second) == "1h"


@pytest.mark.parametrize("helper", [age_parts, relative_text, compact_age, age_words])
@pytest.mark.parametrize("naive", ["value", "now"])
def test_UI11_naive_raises(helper: Callable[[datetime, datetime], object], naive: str) -> None:
    # The one deliberate naive datetime: it is the bad input under test.
    wall = datetime(2026, 10, 1, 8, 0)  # noqa: DTZ001
    value, now = (wall, NOW) if naive == "value" else (NOW, wall)

    with pytest.raises(ValueError, match="naive"):
        helper(value, now)


# The iso filter


def test_UI11_iso_filter() -> None:
    # After the autumn change Kyiv is +02:00; before it +03:00 (CHRT-06 instants).
    assert iso(datetime(2026, 10, 25, 1, 30, tzinfo=UTC)) == "2026-10-25T03:30:00+02:00"
    assert iso(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == "2026-10-25T03:30:00+03:00"
    # Seconds only: microseconds never reach the attribute.
    assert iso(datetime(2026, 10, 1, 8, 0, 5, 999_999, tzinfo=UTC)) == "2026-10-01T11:00:05+03:00"


@pytest.mark.parametrize(
    "value",
    [
        None,
        # The one deliberate naive datetime: it is the bad input under test.
        datetime(2026, 10, 25, 1, 30),  # noqa: DTZ001
        date(2026, 10, 25),
        "2026-10-25T01:30:00+00:00",
    ],
    ids=["none", "naive-datetime", "date", "string"],
)
def test_UI11_iso_filter_bad_input_is_blank(value: object) -> None:
    assert iso(value) == ""


def test_UI11_iso_is_a_template_filter() -> None:
    rendered = Template("{% load timefmt %}{{ t|iso }}").render(
        Context({"t": datetime(2026, 10, 25, 1, 30, tzinfo=UTC)})
    )

    assert rendered == "2026-10-25T03:30:00+02:00"


# The template tags


def _render(source: str, **context: object) -> str:
    return Template("{% load timefmt %}" + source).render(Context(context))


def test_UI11_tag_uses_context_now(monkeypatch: pytest.MonkeyPatch) -> None:
    # A CLOCK far away proves the context's now wins.
    monkeypatch.setattr(timefmt, "CLOCK", FakeClock(NOW + timedelta(days=30)))
    t = NOW - timedelta(minutes=12, seconds=30)

    assert _render("{% relative_time t %}", t=t, now=NOW) == "12 min ago"
    assert _render("{% compact_age_tag t %}", t=t, now=NOW) == "12m"
    assert _render("{% age_words_tag t %}", t=t, now=NOW) == "12 min"


def test_UI11_tag_falls_back_to_the_module_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timefmt, "CLOCK", FakeClock(NOW))
    t = NOW - timedelta(seconds=42)
    # The one deliberate naive datetime: a naive "now" is ignored like a missing one.
    naive_now = datetime(2026, 10, 1, 9, 0)  # noqa: DTZ001

    assert _render("{% relative_time t %}", t=t) == "42 s ago"
    assert _render("{% relative_time t %}", t=t, now=naive_now) == "42 s ago"
    assert _render("{% relative_time t %}", t=t, now="not a time") == "42 s ago"
    assert _render("{% compact_age_tag t %}", t=t) == "42s"
    assert _render("{% age_words_tag t %}", t=t) == "42 s"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "<b>2026-10-01</b>",
        # The one deliberate naive datetime: it is the bad input under test.
        datetime(2026, 10, 1, 7, 0),  # noqa: DTZ001
        date(2026, 10, 1),
    ],
    ids=["none", "string", "naive-datetime", "date"],
)
@pytest.mark.parametrize("tag", ["relative_time", "compact_age_tag", "age_words_tag"])
def test_UI11_tags_render_blank_for_a_non_instant(tag: str, value: object) -> None:
    # Never raises on bad input, and never echoes it (R1).
    assert _render("{% " + tag + " t %}", t=value, now=NOW) == ""


# The location page's context


def _location(token: str = TOKEN) -> Location:
    """An unsaved location: settings_context reads its attributes only."""
    return Location(name="Office", period_s=60, grace_s=30, language="en", bot_token=token)


def _outage(minutes: int, start: datetime) -> history.Outage:
    end = start + timedelta(minutes=minutes)
    return history.Outage(start=start, end=end, off_us=minutes * 60_000_000, in_progress=False)


def test_location_context_totals_and_bot_id() -> None:
    outages = [
        _outage(30, datetime(2026, 10, 1, 6, 0, tzinfo=UTC)),
        _outage(65, datetime(2026, 9, 30, 6, 0, tzinfo=UTC)),
    ]

    assert location_views.outages_total_text(outages) == "1h 35m"
    assert location_views.outages_total_text([]) == ""

    context = location_views.settings_context(_location())
    assert context["token_bot_id"] == "987654321"
    # R3: only the public bot id; the secret part is in no value of the context.
    assert all(SECRET not in str(value) for value in context.values())
    # A stored token without a colon has no public part to show.
    assert location_views.settings_context(_location(SECRET))["token_bot_id"] == ""


@pytest.fixture
def rendered() -> Iterator[list[dict[str, Any]]]:
    """The flattened context of every template rendered while the test runs."""
    contexts: list[dict[str, Any]] = []

    def store(sender: object, context: Context, **kwargs: object) -> None:
        contexts.append(context.flatten())

    template_rendered.connect(store)
    yield contexts
    template_rendered.disconnect(store)


def _detail(rf: RequestFactory, location: Any, now: datetime) -> Any:
    """GET the location page through the view with an injected clock, from another Host.

    A bare request: session and message storage attached as in test_delete.py, no user
    attribute, no resolver match.
    """
    request = rf.get(f"/locations/{location.pk}/", HTTP_HOST="127.0.0.1")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    view = location_views.LocationDetailView.as_view(clock=FakeClock(now))
    return view(request, pk=location.pk)


@pytest.mark.django_db
def test_UI11_location_page_context_has_now(
    settings: Any,
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    rendered: list[dict[str, Any]],
) -> None:
    settings.PUBLIC_BASE_URL = BASE_URL
    settings.ALLOWED_HOSTS = ["testserver", "power.example.org", "127.0.0.1"]
    location = location_factory(name="Office")

    response = _detail(rf, location, fixed_now)

    assert response.status_code == 200
    page = rendered[0]
    assert page["now"] == fixed_now
    # R13: the configured base URL, never the request's Host (127.0.0.1 here).
    assert page["heartbeat_url"] == examples.heartbeat_url(BASE_URL)
    assert "127.0.0.1" not in page["heartbeat_url"]
    assert page["outages_total_text"] == ""
    assert page["token_bot_id"] == location.bot_token.partition(":")[0]


@pytest.mark.django_db
def test_location_page_context_totals_the_listed_outages(
    monkeypatch: pytest.MonkeyPatch,
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    rendered: list[dict[str, Any]],
) -> None:
    location = location_factory(name="Office")
    listed = (
        _outage(30, fixed_now - timedelta(hours=2)),
        _outage(65, fixed_now - timedelta(days=1)),
    )
    seen: list[datetime] = []

    def recent(location_id: int, now: datetime, tz: str) -> history.RecentOutages:
        seen.append(now)
        return history.RecentOutages(outages=listed, has_history=True)

    monkeypatch.setattr(history, "recent_outages", recent)

    assert _detail(rf, location, fixed_now).status_code == 200
    assert rendered[0]["outages_total_text"] == "1h 35m"
    # The page reads its clock once: the outages and the relative times share one now.
    assert seen == [fixed_now]
    assert rendered[0]["now"] == fixed_now
