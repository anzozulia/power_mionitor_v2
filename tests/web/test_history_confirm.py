"""The outage removal pages and the history reset pages (DATA-02, DATA-03; 05-UI-SPEC B, C,
D, E).

- Remove is a GET confirmation page, then a POST (CSRF). The GET never writes: an outage
  that is gone or in progress redirects to the location page with its POST's flash (UI5-D7).
  The POST runs ``history.remove_outage`` under the row lock and redirects to the location
  page with the success, info or error flash; no Telegram call is made (KD2).
- An unknown or deleted location, or a start that is not a valid instant, answers 404,
  never 500; an anonymous visitor is sent to sign in; a POST without a CSRF token is 403.
- Reset is a GET confirmation page, then a POST (CSRF). Its POST runs
  ``history.reset_history`` under the row lock and redirects to the location page with the
  success, error or info flash; no Telegram call is made (the worker unpins the old chart
  later, D-08).

Split from tests/web/test_history_pages.py by 06-09 (TEST-STRATEGY §6.1), with every test
function name kept: this file reads the removal and reset confirmation pages (S10, S11) and
is migrated by 06-16. It reads the location page (S5) only through ``pages.messages()``
and whole-body secret scans. The location page's Recent outages section and Reset history
row live in tests/web/test_history_outages.py (06-15).

Histories are built through the engine (``transitions.record_heartbeat``,
``detection.run_cycle``, ``maintenance.set_maintenance``), so these tests are
``django_db(transaction=True)``. The views get a ``FakeClock`` by monkeypatching their
``clock`` attribute. Times are asserted in Europe/Kyiv, pinned by the autouse ``kyiv``
fixture. Copy strings are 06-UI-SPEC's copy rows remove.* and reset.*, verbatim. The two
confirmation pages are read through ``pages.py`` and the 06-UI-SPEC hooks (the ``confirm``
root, ``confirm-title``, ``outage-details``, ``consequences``, ``confirm-form``, ``keep``
and ``confirm-submit``); their modal fragments are tested in tests/web/test_fragments.py.
Flashes are the page's toasts, read through ``pages.messages()`` as (role, text) (UI-09).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.test import Client
from pages import assert_page, breadcrumbs, by_testid, definitions, h1, main, messages, text

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart.models import ChartMessage
from powermon.engine import history, maintenance, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations import keys
from powermon.locations.models import Location
from powermon.web.history_views import (
    OUTAGE_REMOVED_MESSAGE,
    REMOVAL_DEFERRED_MESSAGE,
    HistoryResetView,
    OutageRemoveView,
)
from powermon.web.location_views import LocationDetailView
from powermon.worker import detection, io_loop

User = get_user_model()

KYIV = "Europe/Kyiv"
# 05-UI-SPEC Copywriting › Flashes, verbatim.
GONE_FLASH = (
    "This outage is no longer in the history: it was already removed, or the history was "
    "reset. Nothing changed."
)
REFUSED_FLASH = "This outage is still in progress. It can be removed after power returns."
RESET_FLASH = (
    "History reset. The location waits for its next heartbeat, which restarts monitoring "
    "without an alert. The old weekly chart is unpinned when the bot can do so; if the pin "
    "stays, unpin it by hand in Telegram."
)
RESET_REFUSED_FLASH = (
    "An outage is in progress. Reset the history after power returns, or delete the location."
)
NOTHING_TO_RESET_FLASH = "There is no power history to reset. Nothing changed."
# 05-UI-SPEC Copywriting › Reset-history confirmation, verbatim.
RESET_LEAD = "This cannot be undone. Resetting the history:"
RESET_CONSEQUENCES = [
    "deletes all recorded power history of this location: the chart and the daily totals "
    "show no data for the time before the reset, and Recent outages is empty;",
    "unpins its weekly chart in the channel if the bot can still pin there; otherwise unpin "
    "it by hand in Telegram (the posted messages stay in the channel);",
    "sets it to Waiting for first heartbeat: nothing is detected until the next heartbeat, "
    "which restarts monitoring as On without an alert and posts and pins a new chart;",
    "still sends the alerts already queued, because they report real events;",
    "keeps the settings, the device key, the switches and the Delivery status.",
]
RESET_ALTERNATIVE = (
    "To remove a single false outage instead, use Recent outages on the location page."
)
# 06-UI-SPEC copy rows remove.*, verbatim (remove.c3 is amendment A3, the refined D-04).
LEAD = "This cannot be undone. Removing this outage:"
CONSEQUENCE_1 = (
    "records its off time as power on, so the chart and the daily totals no longer count it "
    "as off time or as an outage;"
)
CONSEQUENCE_2 = (
    "keeps the time inside it that was not monitored (maintenance, server downtime) as not "
    "monitored;"
)
CONSEQUENCE_3 = (
    "sends nothing itself, and drops its queued OFF and ON alerts if the OFF alert was never "
    "sent (if it already went out, its queued ON alert is still sent, so the channel is not "
    "left at power off);"
)
CONSEQUENCE_4 = (
    "leaves the live status unchanged, including the On since time from which the next OFF "
    'alert counts "was ON for".'
)
CLOSING = (
    "The chart shows the change at its next update. It shows the last 7 days; charts already "
    "posted for earlier days do not change."
)
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
XSS_NAME = "<script>alert(1)</script>"


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


def _reset(location: Any) -> str:
    return f"/locations/{location.pk}/reset/"


def _flashes(page: str) -> list[tuple[str, str]]:
    """Each flash (toast) on a page as (role, text) (UI-09)."""
    return [(flash.role, flash.text) for flash in messages(page)]


def _is_info(page: str) -> bool:
    """Every flash on the page is an info toast."""
    return all(flash.level == "info" for flash in messages(page))


def _remove_trail(location: Any, name: str) -> list[tuple[str, str | None]]:
    """The S10 breadcrumbs: Locations > {name} > Remove outage (the current page)."""
    return [("Locations", "/"), (name, _page(location)), ("Remove outage", None)]


def _reset_trail(location: Any, name: str) -> list[tuple[str, str | None]]:
    """The S11 breadcrumbs: Locations > {name} > Reset history (the current page)."""
    return [("Locations", "/"), (name, _page(location)), ("Reset history", None)]


def _items(root: Any, testid: str) -> list[str]:
    """The text of each item of the list hook ``testid`` inside ``root``."""
    return [text(item) for item in by_testid(root, testid).find_all("li")]


def _intervals(location: Any) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _outbox() -> list[tuple[int, str, str]]:
    return list(OutboxMessage.objects.order_by("id").values_list("id", "status", "last_error"))


def _written(location: Any) -> tuple[Any, ...]:
    """Everything a removal or a reset could write: the timeline, the outbox, the live state
    and the chart records' reset marks."""
    state = LocationState.objects.filter(location=location).values_list(
        "status", "last_heartbeat_at", "on_since", "outage_started_at", "state_version"
    )
    marks = ChartMessage.objects.filter(location=location).order_by("id")
    return (
        _intervals(location),
        _outbox(),
        list(state),
        list(marks.values_list("id", "history_reset_at", "retired_at")),
    )


def _off_since_9(location_factory: Callable[..., Any], **fields: Any) -> Any:
    """On since 08:00, OFF from 09:00 (UTC): the outage is in progress."""
    _no_anchors()
    location = location_factory(**fields)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    return location


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
    monkeypatch.setattr(HistoryResetView, "clock", clock)
    return clock


# DATA-02: a removal POST for a start with no OFF piece


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
    assert _flashes(admin.get(response.url).content.decode()) == [("status", GONE_FLASH)]
    assert _intervals(location) == intervals
    assert _outbox() == rows
    assert len(fake_telegram.calls) == 0


# Screen B: the removal confirmation


@pytest.mark.django_db(transaction=True)
def test_remove_confirmation_page(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office")
    _clock(monkeypatch, _at(16, 0))
    url = _remove(location, _at(9, 0))
    before = _written(location)

    response = admin.get(url)

    # assert_page also proves there is no injected or inline script (R5).
    soup = assert_page(response, app=True, title="Office · Remove outage")
    root = by_testid(main(soup), "confirm")
    assert breadcrumbs(soup) == _remove_trail(location, "Office")
    # The h1 carries no name (UI5-D13).
    assert text(h1(soup)) == "Remove this outage?"
    assert definitions(root, "outage-details") == [
        ("Start", "2026-10-01 12:00:00 EEST"),
        ("End", "2026-10-01 13:00:00 EEST"),
        ("Off time", "1h"),
    ]
    # No not-monitored time inside this outage: no Consequence 2.
    assert _items(root, "consequences") == [CONSEQUENCE_1, CONSEQUENCE_3, CONSEQUENCE_4]
    # Consequence 3 states the refined D-04 in the 06-UI-SPEC amendment A3 wording.
    assert _items(root, "consequences")[1] == (
        "sends nothing itself, and drops its queued OFF and ON alerts if the OFF alert was "
        "never sent (if it already went out, its queued ON alert is still sent, so the channel "
        "is not left at power off);"
    )
    words = text(root)
    assert words.index(LEAD) < words.index(CONSEQUENCE_1) < words.index(CLOSING)
    # One form in main: the destructive POST, after Keep (back to Recent outages).
    [form] = main(soup).find_all("form")
    assert (form.get("method"), form.get("action")) == ("post", url)
    assert form is by_testid(root, "confirm-form")
    button = by_testid(form, "confirm-submit")
    assert (button.get("data-variant"), text(button)) == ("danger", "Remove outage")
    keep = by_testid(root, "keep")
    assert (keep.get("href"), text(keep)) == (f"{_page(location)}#recent-outages", "Keep outage")
    assert root.find_all(["a", "button"])[-2:] == [keep, button]
    dangers = main(soup).find_all(attrs={"data-variant": "danger"})
    assert [found.get("data-testid") for found in dangers] == ["confirm-submit"]
    assert main(soup).find_all(attrs={"data-variant": "primary"}) == []
    assert soup.find_all(autofocus=True) == []
    # The GET wrote nothing.
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_remove_confirmation_shows_the_not_monitored_consequence_only_when_needed(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    plain = _two_outages(location_factory, name="Plain")
    paused = _off_since_9(location_factory, name="Paused")
    assert maintenance.set_maintenance(paused.pk, True, _at(10, 0)) is True
    assert maintenance.set_maintenance(paused.pk, False, _at(10, 10)) is True
    assert transitions.record_heartbeat(paused.pk, _at(11, 0)) == "restored"
    _clock(monkeypatch, _at(16, 0))

    page = main(assert_page(admin.get(_remove(paused, _at(9, 0))), app=True))

    # 09:00-11:00 with 10:00-10:10 not monitored: off time is shorter than the span.
    assert definitions(page, "outage-details") == [
        ("Start", "2026-10-01 12:00:00 EEST"),
        ("End", "2026-10-01 14:00:00 EEST"),
        ("Off time", "1h 50m"),
    ]
    assert _items(page, "consequences") == [
        CONSEQUENCE_1,
        CONSEQUENCE_2,
        CONSEQUENCE_3,
        CONSEQUENCE_4,
    ]
    other = main(assert_page(admin.get(_remove(plain, _at(15, 0))), app=True))
    assert CONSEQUENCE_2 not in _items(other, "consequences")


# Screens D and E: results, refusals and edge responses


@pytest.mark.django_db(transaction=True)
def test_remove_get_redirects_with_the_post_flash_when_a_check_fails(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _off_since_9(location_factory)
    _clock(monkeypatch, _at(9, 30))
    before = _written(location)

    in_progress = admin.get(_remove(location, _at(9, 0)))

    assert in_progress.status_code == 302
    assert in_progress.url == _page(location)
    page = admin.get(in_progress.url).content.decode()
    assert _flashes(page) == [("alert", REFUSED_FLASH)]

    missing = admin.get(_remove(location, _at(8, 30)))

    assert missing.status_code == 302
    assert missing.url == _page(location)
    page = admin.get(missing.url).content.decode()
    assert _flashes(page) == [("status", GONE_FLASH)]
    assert _is_info(page)
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_INV07_3_crafted_post_for_an_outage_in_progress_is_refused(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _off_since_9(location_factory)
    _clock(monkeypatch, _at(9, 30))
    url = _remove(location, _at(9, 0))

    for maintenance_on in (False, True):
        if maintenance_on:
            # Its open piece is not monitored now: still the current outage.
            assert maintenance.set_maintenance(location.pk, True, _at(9, 40)) is True
        before = _written(location)

        response = admin.post(url)

        assert response.status_code == 302
        assert response.url == _page(location)
        page = admin.get(response.url).content.decode()
        assert _flashes(page) == [("alert", REFUSED_FLASH)]
        assert _written(location) == before
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_D04_W1_A1_removal_post_while_the_off_alert_is_sending_is_deferred(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))
    off = OutboxMessage.objects.get(location=location, kind="power_off", event_at=_at(9, 0))
    # The relay claimed the outage's OFF alert: an attempt is in flight.
    assert outbox.claim(off.pk) is True
    url = _remove(location, _at(9, 0))
    before = _written(location)

    # The confirmation GET does not check it: the attempt settles within seconds.
    assert admin.get(url).status_code == 200

    response = admin.post(url)

    assert response.status_code == 302
    assert response.url == _page(location)
    page = admin.get(response.url).content.decode()
    assert _flashes(page) == [("status", REMOVAL_DEFERRED_MESSAGE)]
    assert _is_info(page)
    assert _written(location) == before
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_remove_double_submit_says_already_gone(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))
    url = _remove(location, _at(15, 0))

    first = admin.post(url)

    assert first.status_code == 302
    assert _flashes(admin.get(first.url).content.decode()) == [
        ("status", OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 18:00"))
    ]
    after = _written(location)

    second = admin.post(url)

    assert second.status_code == 302
    assert second.url == _page(location)
    page = admin.get(second.url).content.decode()
    assert _flashes(page) == [("status", GONE_FLASH)]
    assert _is_info(page)
    assert _written(location) == after
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_remove_success_flash_names_the_start_from_the_stored_instant(
    admin: Client,
    kyiv: Any,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
) -> None:
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    # Two outages whose starts carry seconds: 09:00:45 and 11:00:59 UTC.
    for start in (_at(9, 0, 45), _at(11, 0, 59)):
        assert transitions.record_heartbeat(location.pk, start) == "plain"
        assert detection.run_cycle(start.replace(minute=2, second=30)) == 1
        assert transitions.record_heartbeat(location.pk, start.replace(minute=30)) == "restored"
    _clock(monkeypatch, _at(12, 0))

    first = admin.post(_remove(location, _at(9, 0, 45)))

    # In the display TZ, seconds cut off, never rounded (UI5-D4, UI5-D15).
    assert _flashes(admin.get(first.url).content.decode()) == [
        ("status", OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 12:00"))
    ]
    kyiv.TIME_ZONE = "UTC"
    second = admin.post(_remove(location, _at(11, 0, 59)))
    assert _flashes(admin.get(second.url).content.decode()) == [
        ("status", OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 11:00"))
    ]


@pytest.mark.django_db(transaction=True)
def test_remove_urls_answer_404(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office")
    gone = _two_outages(location_factory, name="Gone")
    Location.objects.filter(pk=gone.pk).update(deleted_at=_at(16, 0))
    _clock(monkeypatch, _at(16, 0))
    before = (_written(location), _written(gone))
    start_us = ops.instant_us(_at(9, 0))

    for url in (
        f"/locations/{gone.pk + 1000}/outages/{start_us}/remove/",
        f"/locations/{gone.pk}/outages/{start_us}/remove/",
        # Digits, but no valid instant: out of the datetime range.
        f"/locations/{location.pk}/outages/{10**20}/remove/",
        # More digits than int() accepts: the URL converter finds no match.
        f"/locations/{location.pk}/outages/{'1' * 5000}/remove/",
        f"/locations/{location.pk}/outages/abc/remove/",
    ):
        got = admin.get(url)
        assert got.status_code == 404, url[:80]
        # The start is never echoed (R12).
        assert str(10**20) not in got.content.decode(), url[:80]
        assert admin.post(url).status_code == 404, url[:80]

    assert (_written(location), _written(gone)) == before


@pytest.mark.django_db(transaction=True)
def test_remove_post_for_a_location_deleted_mid_request_answers_404(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory, name="Office")
    _clock(monkeypatch, _at(16, 0))
    before = _written(location)
    remove_outage = history.remove_outage

    def delete_then_remove(pk: int, start: datetime, **kwargs: Any) -> history.RemoveResult:
        # A second tab deletes the location after the view's lookup, before the row lock.
        Location.objects.filter(pk=pk).update(deleted_at=_at(16, 0))
        return remove_outage(pk, start, **kwargs)

    monkeypatch.setattr(history, "remove_outage", delete_then_remove)

    response = admin.post(_remove(location, _at(15, 0)))

    # 404, as for any deleted location (05-UI-SPEC E), never the "already removed" flash.
    assert response.status_code == 404
    assert _flashes(admin.get("/").content.decode()) == []
    assert _written(location) == before
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_remove_post_needs_csrf(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    before = _written(location)

    response = client.post(_remove(location, _at(9, 0)))

    assert response.status_code == 403
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_anonymous_remove_url_redirects_to_sign_in(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    url = _remove(location, _at(9, 0))
    before = _written(location)

    assert client.get(url).url == f"/login/?next={url}"
    response = client.post(url)

    assert response.status_code == 302
    assert response.url == f"/login/?next={url}"
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_remove_page_escapes_the_name(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name=XSS_NAME)
    _clock(monkeypatch, _at(16, 0))

    response = admin.get(_remove(location, _at(9, 0)))

    # assert_page also proves nothing renders as a script (R1).
    soup = assert_page(response, app=True, title=f"{XSS_NAME} · Remove outage")
    assert breadcrumbs(soup)[1] == (XSS_NAME, _page(location))
    # The h1 carries no name (UI5-D13).
    assert text(h1(soup)) == "Remove this outage?"
    assert XSS_NAME not in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_removal_pages_show_no_secret(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office", bot_token=TOKEN)
    assert transitions.record_heartbeat(location.pk, _at(16, 0)) == "plain"
    assert detection.run_cycle(_at(16, 1, 31)) == 1
    _clock(monkeypatch, _at(16, 30))
    key = location.device_key

    detail = admin.get(_page(location)).content.decode()
    confirm = admin.get(_remove(location, _at(9, 0))).content.decode()
    flash_pages = [
        # Removed (success), already gone (info), in progress (error).
        admin.post(_remove(location, _at(9, 0)), follow=True),
        admin.post(_remove(location, _at(9, 0)), follow=True),
        admin.post(_remove(location, _at(16, 0)), follow=True),
    ]
    flashes = [response.content.decode() for response in flash_pages]
    assert [_flashes(page) for page in flashes] == [
        [("status", OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 12:00"))],
        [("status", GONE_FLASH)],
        [("alert", REFUSED_FLASH)],
    ]

    for html in (detail, confirm, *flashes):
        assert key not in html
        assert keys.mask_key(key) not in html
        assert TOKEN not in html
        assert SECRET not in html
    # The confirmation page has no settings panel: not even the masked token.
    assert "•" not in str(main(confirm))
    for response in flash_pages:
        for url, _status in response.redirect_chain:
            assert key not in url
            assert SECRET not in url


# DATA-03: a reset POST during an outage


@pytest.mark.django_db(transaction=True)
def test_reset_post_during_an_outage_is_refused(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _off_since_9(location_factory)
    _clock(monkeypatch, _at(9, 30))
    before = _written(location)

    response = admin.post(_reset(location))

    assert response.status_code == 302
    assert response.url == _page(location)
    page = admin.get(response.url).content.decode()
    assert _flashes(page) == [("alert", RESET_REFUSED_FLASH)]
    assert _written(location) == before
    assert len(fake_telegram.calls) == 0


# Screen C: the reset confirmation


@pytest.mark.django_db(transaction=True)
def test_reset_confirmation_page(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office")
    _clock(monkeypatch, _at(16, 0))
    url = _reset(location)
    before = _written(location)

    response = admin.get(url)

    # assert_page also proves there is no injected or inline script (R5).
    soup = assert_page(response, app=True, title="Office · Reset history")
    root = by_testid(main(soup), "confirm")
    assert breadcrumbs(soup) == _reset_trail(location, "Office")
    assert text(h1(soup)) == "Reset the history of Office?"
    assert _items(root, "consequences") == RESET_CONSEQUENCES
    words = text(root)
    assert words.index(RESET_LEAD) < words.index(RESET_CONSEQUENCES[0])
    assert words.index(RESET_CONSEQUENCES[-1]) < words.index(RESET_ALTERNATIVE)
    # One form in main: the destructive POST, after Keep (back to the Reset history row).
    [form] = main(soup).find_all("form")
    assert (form.get("method"), form.get("action")) == ("post", url)
    assert form is by_testid(root, "confirm-form")
    button = by_testid(form, "confirm-submit")
    assert (button.get("data-variant"), text(button)) == ("danger", "Reset history")
    keep = by_testid(root, "keep")
    assert (keep.get("href"), text(keep)) == (f"{_page(location)}#reset-history", "Keep history")
    assert root.find_all(["a", "button"])[-2:] == [keep, button]
    dangers = main(soup).find_all(attrs={"data-variant": "danger"})
    assert [found.get("data-testid") for found in dangers] == ["confirm-submit"]
    assert main(soup).find_all(attrs={"data-variant": "primary"}) == []
    assert soup.find_all(autofocus=True) == []
    # No settings panel: not even the masked token.
    assert main(soup).find_all(attrs={"data-testid": "settings-panel"}) == []
    assert "•" not in str(main(soup))
    # The GET wrote nothing.
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_reset_get_redirects_with_the_post_flash_when_a_check_fails(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    off = _off_since_9(location_factory, name="Off")
    never = location_factory(name="Never monitored")
    _clock(monkeypatch, _at(9, 20))

    for maintenance_on in (False, True):
        if maintenance_on:
            assert maintenance.set_maintenance(off.pk, True, _at(9, 30)) is True
        before = (_written(off), _written(never))

        refused = admin.get(_reset(off))

        # UI5-D7: a refused GET redirects with the error flash its POST would give.
        assert refused.status_code == 302
        assert refused.url == _page(off)
        page = admin.get(refused.url).content.decode()
        assert _flashes(page) == [("alert", RESET_REFUSED_FLASH)]

        nothing = admin.get(_reset(never))

        assert nothing.status_code == 302
        assert nothing.url == _page(never)
        page = admin.get(nothing.url).content.decode()
        assert _flashes(page) == [("status", NOTHING_TO_RESET_FLASH)]
        assert _is_info(page)
        assert (_written(off), _written(never)) == before


@pytest.mark.django_db(transaction=True)
def test_reset_crafted_post_during_an_outage_is_refused(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _off_since_9(location_factory)
    ChartMessage.objects.create(
        location=location,
        local_date=_at(9, 0).date(),
        chat_id=location.chat_id,
        bot_key=io_loop.bot_key(location.bot_token),
        message_id=501,
        pinned=True,
        last_rendered_at=_at(9, 0),
        created_at=_at(9, 0),
    )
    _clock(monkeypatch, _at(9, 20))

    for maintenance_on in (False, True):
        if maintenance_on:
            # Its open piece is not monitored now: the status is still off (D-06).
            assert maintenance.set_maintenance(location.pk, True, _at(9, 30)) is True
        before = _written(location)

        response = admin.post(_reset(location))

        assert response.status_code == 302
        assert response.url == _page(location)
        page = admin.get(response.url).content.decode()
        assert _flashes(page) == [("alert", RESET_REFUSED_FLASH)]
        assert _written(location) == before
    assert len(fake_telegram.calls) == 0


# Screens D and E: results, double submit and edge responses


@pytest.mark.django_db(transaction=True)
def test_reset_double_submit_says_nothing_to_reset(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    first = admin.post(_reset(location))

    assert first.status_code == 302
    page = admin.get(first.url).content.decode()
    assert _flashes(page) == [("status", RESET_FLASH)]
    after = _written(location)

    second = admin.post(_reset(location))

    assert second.status_code == 302
    assert second.url == _page(location)
    page = admin.get(second.url).content.decode()
    assert _flashes(page) == [("status", NOTHING_TO_RESET_FLASH)]
    assert _is_info(page)
    assert _written(location) == after
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_reset_flashes_stack_in_queue_order(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    # Two actions before the page is read: each flash in its own toast, in order (E5).
    admin.post(_reset(location))
    admin.post(_reset(location))

    page = admin.get(_page(location)).content.decode()
    assert _flashes(page) == [("status", RESET_FLASH), ("status", NOTHING_TO_RESET_FLASH)]
    # A page reached without a Phase 5 action shows no flash.
    assert _flashes(admin.get(_page(location)).content.decode()) == []


@pytest.mark.django_db(transaction=True)
def test_reset_urls_answer_404(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office")
    gone = location_factory(name="Gone")
    assert transitions.record_heartbeat(gone.pk, _at(16, 0)) == "started"
    Location.objects.filter(pk=gone.pk).update(deleted_at=_at(16, 1))
    _clock(monkeypatch, _at(16, 5))
    before = (_written(location), _written(gone))

    for url in (f"/locations/{gone.pk + 1000}/reset/", _reset(gone)):
        assert admin.get(url).status_code == 404, url
        assert admin.post(url).status_code == 404, url

    assert (_written(location), _written(gone)) == before
    # Deleted between the page's lookup and the reset's row lock: 404 too, never a flash.
    monkeypatch.setattr(history, "reset_history", lambda pk, now: "gone")
    assert admin.post(_reset(location)).status_code == 404
    assert _written(location) == before[0]


@pytest.mark.django_db(transaction=True)
def test_reset_post_needs_csrf(location_factory: Callable[..., Any]) -> None:
    location = _two_outages(location_factory)
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    before = _written(location)

    response = client.post(_reset(location))

    assert response.status_code == 403
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_anonymous_reset_url_redirects_to_sign_in(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    url = _reset(location)
    before = _written(location)

    assert client.get(url).url == f"/login/?next={url}"
    response = client.post(url)

    assert response.status_code == 302
    assert response.url == f"/login/?next={url}"
    assert _written(location) == before


@pytest.mark.django_db(transaction=True)
def test_reset_page_escapes_the_name_and_shows_a_long_name_whole(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name=XSS_NAME)
    name = "x" * 100
    long = location_factory(name=name)
    assert transitions.record_heartbeat(long.pk, _at(16, 0)) == "started"
    _clock(monkeypatch, _at(16, 5))

    response = admin.get(_reset(location))

    # assert_page also proves nothing renders as a script (R1).
    soup = assert_page(response, app=True, title=f"{XSS_NAME} · Reset history")
    assert text(h1(soup)) == f"Reset the history of {XSS_NAME}?"
    assert breadcrumbs(soup)[1] == (XSS_NAME, _page(location))
    assert XSS_NAME not in response.content.decode()
    # E10 long-text: a 100-character name is shown whole, never truncated.
    page = assert_page(admin.get(_reset(long)), app=True, title=f"{name} · Reset history")
    assert text(h1(page)) == f"Reset the history of {name}?"
    assert breadcrumbs(page) == _reset_trail(long, name)
    assert breadcrumbs(page, "breadcrumbs-compact") == _reset_trail(long, name)


@pytest.mark.django_db(transaction=True)
def test_reset_pages_show_no_secret(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office", bot_token=TOKEN)
    off = _off_since_9(location_factory, name="Off", bot_token=TOKEN)
    _clock(monkeypatch, _at(16, 0))
    keys_shown = (location.device_key, off.device_key)

    # The whole location page is scanned, its Reset history row included.
    detail = admin.get(_page(location)).content.decode()
    confirm = admin.get(_reset(location)).content.decode()
    flash_pages = [
        # Reset (success), nothing to reset (info), refused during an outage (error).
        admin.post(_reset(location), follow=True),
        admin.post(_reset(location), follow=True),
        admin.post(_reset(off), follow=True),
    ]
    flashes = [response.content.decode() for response in flash_pages]
    assert [_flashes(page) for page in flashes] == [
        [("status", RESET_FLASH)],
        [("status", NOTHING_TO_RESET_FLASH)],
        [("alert", RESET_REFUSED_FLASH)],
    ]

    for html in (detail, confirm, *flashes):
        for key in keys_shown:
            assert key not in html
            assert keys.mask_key(key) not in html
        assert TOKEN not in html
        assert SECRET not in html
    # The confirmation page has no settings panel: not even the masked token.
    assert "•" not in str(main(confirm))
    for response in flash_pages:
        for url, _status in response.redirect_chain:
            for key in keys_shown:
                assert key not in url
            assert SECRET not in url
