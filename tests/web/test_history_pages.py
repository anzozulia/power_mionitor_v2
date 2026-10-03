"""The Recent outages section and the removal pages (DATA-02; 05-UI-SPEC A1, B, D, E).

- The location page lists the outages of the last 14 local days, newest first, one row per
  outage start, in a ``.table-wrap`` table: "Outage" (start – end), "Off time" (the chart's
  totals format) and a visually hidden "Action" column with a Remove link per ended outage.
  The current outage reads "in progress" and has no link. Without outages it shows one of
  the two empty lines (UI5-D10).
- Remove is a GET confirmation page, then a POST (CSRF). The GET never writes: an outage
  that is gone or in progress redirects to the location page with its POST's flash (UI5-D7).
  The POST runs ``history.remove_outage`` under the row lock and redirects to the location
  page with the success, info or error flash; no Telegram call is made (KD2).
- An unknown or deleted location, or a start that is not a valid instant, answers 404,
  never 500; an anonymous visitor is sent to sign in; a POST without a CSRF token is 403.

Histories are built through the engine (``transitions.record_heartbeat``,
``detection.run_cycle``, ``maintenance.set_maintenance``), so these tests are
``django_db(transaction=True)``. The views get a ``FakeClock`` by monkeypatching their
``clock`` attribute. Times are asserted in Europe/Kyiv, pinned by the autouse ``kyiv``
fixture. Copy strings are 05-UI-SPEC's, verbatim; they contain double quotes, so pages are
compared after ``html.unescape``.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from typing import Any

import pytest
from conftest import FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.test import Client

from powermon.alerts import ops
from powermon.alerts.models import OutboxMessage
from powermon.engine import transitions
from powermon.engine.models import PowerInterval, SystemState
from powermon.web.history_views import OutageRemoveView
from powermon.web.location_views import LocationDetailView
from powermon.worker import detection

User = get_user_model()

KYIV = "Europe/Kyiv"
# 05-UI-SPEC Copywriting › Flashes, verbatim.
REMOVED_FLASH = (
    "Outage from {start} removed: its time now counts as power on. No message was sent. If it "
    "is within the last 7 days, the pinned chart shows the change within 15 minutes."
)
GONE_FLASH = (
    "This outage is no longer in the history: it was already removed, or the history was "
    "reset. Nothing changed."
)


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    """Pin the display TZ, so the expected times do not depend on the env file."""
    settings.TIME_ZONE = KYIV
    return settings


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _page(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _remove(location: Any, start: datetime) -> str:
    return f"/locations/{location.pk}/outages/{ops.instant_us(start)}/remove/"


def _flashes(page: str) -> list[str]:
    return [unescape(t) for t in re.findall(r'role="(?:status|alert)">([^<]*)<', page)]


def _main(page: str) -> str:
    """The page's <main>: the header (with its sign-out form) left out."""
    return page[page.index("<main") :]


def _section(page: str) -> str:
    """The Recent outages section of the location page."""
    return page[page.index("<h2>Recent outages</h2>") : page.index("<h2>Settings</h2>")]


def _rows(page: str) -> list[str]:
    """The Recent outages table's body rows (inner HTML), in order."""
    body = re.search(r"<tbody>(.*?)</tbody>", _section(page), re.S)
    return [] if body is None else re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S)


def _intervals(location: Any) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _outbox() -> list[tuple[int, str, str]]:
    return list(OutboxMessage.objects.order_by("id").values_list("id", "status", "last_error"))


def _no_anchors() -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _two_outages(location_factory: Callable[..., Any], **fields: Any) -> Any:
    """On since 08:00, outages 09:00-10:00 and 15:00-15:30 (UTC), on again since 15:30."""
    _no_anchors()
    location = location_factory(**fields)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    assert transitions.record_heartbeat(location.pk, _at(15, 0)) == "plain"
    assert detection.run_cycle(_at(15, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(15, 30)) == "restored"
    return location


def _clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> FakeClock:
    """Give the location page and the removal views one FakeClock at ``now``."""
    clock = FakeClock(now)
    monkeypatch.setattr(LocationDetailView, "clock", clock)
    monkeypatch.setattr(OutageRemoveView, "clock", clock)
    return clock


# DATA-02 end to end (the phase tracer)


@pytest.mark.django_db(transaction=True)
def test_DATA02_remove_an_outage_from_the_location_page(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory, name="Office")
    _clock(monkeypatch, _at(16, 0))

    page = _main(admin.get(_page(location)).content.decode())

    assert "<h2>Recent outages</h2>" in page
    assert len(_rows(page)) == 2
    links = re.findall(r'<a href="([^"]+)">Remove<span', _section(page))
    assert links == [_remove(location, _at(15, 0)), _remove(location, _at(9, 0))]

    confirm = admin.get(links[1])
    assert confirm.status_code == 200
    assert "<h1>Remove this outage?</h1>" in confirm.content.decode()

    response = admin.post(links[1])

    assert response.status_code == 302
    assert response.url == _page(location)
    after = admin.get(response.url).content.decode()
    # 09:00 UTC is 12:00 in Kyiv (EEST).
    assert _flashes(after) == [REMOVED_FLASH.format(start="2026-10-01 12:00")]
    assert len(_rows(after)) == 1
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_remove_post_for_a_start_with_no_off_piece_writes_nothing(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))
    intervals, rows = _intervals(location), _outbox()

    # 09:30 lies inside an outage, but no outage starts there.
    response = admin.post(_remove(location, _at(9, 30)))

    assert response.status_code == 302
    assert response.url == _page(location)
    assert _flashes(admin.get(response.url).content.decode()) == [GONE_FLASH]
    assert _intervals(location) == intervals
    assert _outbox() == rows
    assert len(fake_telegram.calls) == 0
