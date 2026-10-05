"""The location page's Recent outages section and its Reset history row (DATA-02, DATA-03;
05-UI-SPEC A1, A2).

- The location page's Recent outages card (``section#recent-outages``) lists the outages of
  the last 14 local days, newest first, one row per outage start, in the
  ``outages-table``: "Outage" (start – end, the range never broken after the dash),
  "Off time" (the chart's totals format) and a visually hidden "Action" column with a
  ``remove-outage`` link per ended outage. The current outage carries
  ``data-in-progress``, reads "in progress" and has no link. Without outages it shows one
  of the two empty states, ``outages-empty`` with its ``data-reason`` (UI5-D10).
- The Danger zone's Reset history row (``#reset-history``) shows its sentence, then exactly
  one of the refusal line while an outage is in progress (the stored status is off),
  "There is no power history to reset." without history (``reset-unavailable`` with its
  ``data-reason``), else the ``reset-history`` link to the reset confirmation.
- The end-to-end tests (DATA-02, DATA-03 and the fall-back day) open a confirmation page
  from the location page. They read that page only as text, through ``pages.h1`` /
  ``pages.main`` and ``pages.text``, never through its markup.

Split from tests/web/test_history_pages.py by 06-09 (TEST-STRATEGY §6.1), with every test
function name kept: this file reads the location page (S5) and is migrated by 06-15. The
removal and reset confirmation pages and their POST results live in
tests/web/test_history_confirm.py (06-16).

Histories are built through the engine (``transitions.record_heartbeat``,
``detection.run_cycle``, ``maintenance.set_maintenance``), so these tests are
``django_db(transaction=True)``. The views get a ``FakeClock`` by monkeypatching their
``clock`` attribute. Times are asserted in Europe/Kyiv, pinned by the autouse ``kyiv``
fixture. Template copy is the 06-UI-SPEC copy table's, verbatim. Pages are read through
tests/web/pages.py and the 06-UI-SPEC hooks only. Flashes are the page's toasts, read
through ``pages.messages()`` as (role, text) (UI-09).
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import Client
from pages import (
    all_by_testid,
    assert_no_injected_script,
    by_testid,
    definitions,
    h1,
    main,
    messages,
    parse,
    section,
    table,
    text,
)

from powermon.alerts import ops
from powermon.engine import history, maintenance, restore, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.web.history_views import (
    OUTAGE_REMOVED_MESSAGE,
    HistoryResetView,
    OutageRemoveView,
)
from powermon.web.location_views import LocationDetailView, OutageRow, local_minute, outage_rows
from powermon.worker import detection

User = get_user_model()

KYIV = "Europe/Kyiv"
# 05-UI-SPEC Copywriting › Flashes, verbatim.
RESET_FLASH = (
    "History reset. The location waits for its next heartbeat, which restarts monitoring "
    "without an alert. The old weekly chart is unpinned when the bot can do so; if the pin "
    "stays, unpin it by hand in Telegram."
)
# 06-UI-SPEC copy table, loc.reset_desc, loc.reset_refused, verbatim.
RESET_SENTENCE = (
    "Deletes all recorded power history of this location and unpins its weekly chart where "
    "the bot still can. The location then waits for its next heartbeat, which restarts "
    "monitoring without an alert. Settings, the device key and the switches are kept. There "
    "is no undo."
)
RESET_IN_PROGRESS_LINE = (
    "An outage is in progress. Reset the history after power returns, or delete the location."
)
NOTHING_TO_RESET_LINE = "There is no power history to reset."
# 06-UI-SPEC copy table, loc.outages_*, verbatim.
INTRO = (
    "Outages in the last 14 days, newest first. Remove an outage that was not a real power "
    "cut, for example when the device or its internet connection was down while the power "
    "was on."
)
# Amendment A2 (06-CONTEXT, loc.off_time_note).
OFF_TIME_NOTE = (
    "Off time counts only time recorded as power off, as the chart's daily totals do. Time "
    "that was not monitored is left out, so off time and the end shown can differ from the "
    "alerts."
)
IN_PROGRESS_NOTE = "The outage in progress can be removed after power returns."
NO_OUTAGES = "No outages in the last 14 days."
NO_HISTORY = (
    "No power history yet. Outages are listed here once the device has sent its first heartbeat."
)
# A range of start and end: a no-break space, the dash, a word joiner, a no-break space, so
# it never breaks after the dash; text() reads the no-break spaces as spaces.
RANGE = " –\u2060 "
# 05-UI-SPEC Copywriting › Remove-outage and Reset-history confirmations, the h1s.
REMOVE_H1 = "Remove this outage?"
RESET_H1 = "Reset the history of {name}?"
# A full display_time of the fall-back day's repeated 03:30 or 03:45, with its zone; the
# spaces are optional, so text joined from separate elements is still matched.
FALL_BACK_TIME = re.compile(r"2026-10-25\s*03:(?:30|45):00\s*(EEST|EET)")


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


def _get(admin: Client, location: Any) -> Tag:
    """The location page, parsed."""
    response = admin.get(_page(location))
    assert response.status_code == 200
    return parse(response)


def _section(page: Any) -> Tag:
    """The Recent outages card of the location page."""
    return section(page, "recent-outages")


def _reset_section(page: Any) -> Tag:
    """The Reset history row of the location page's Danger zone."""
    return section(page, "reset-history")


def _rows(page: Any) -> list[list[str]]:
    """The Recent outages table's body rows as cell texts, in order; [] without a table."""
    if not all_by_testid(page, "outages-table"):
        return []
    return table(page, "outages-table")[1]


def _remove_links(page: Any) -> list[tuple[str, str]]:
    """Every remove-outage entry as (href, accessible name), in order."""
    return [(str(link["href"]), text(link)) for link in all_by_testid(page, "remove-outage")]


def _intervals(location: Any) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


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


# The row formatting on its own (UI5-D3, UI5-D4, UI5-D5)


def test_local_minute_cuts_seconds_in_the_display_tz() -> None:
    assert local_minute(_at(9, 59, 59), KYIV) == ("2026-10-01", "12:59")
    # 21:00 UTC is local midnight of the next day in Kyiv (EEST).
    assert local_minute(_at(21, 0), KYIV) == ("2026-10-02", "00:00")
    assert local_minute(_at(21, 0), "UTC") == ("2026-10-01", "21:00")
    with pytest.raises(ValueError, match="naive"):
        local_minute(datetime(2026, 10, 1, 9, 0), KYIV)  # noqa: DTZ001


def test_outage_rows_format_each_outage() -> None:
    outages = [
        history.Outage(start=_at(20, 30), end=None, off_us=20_000_000, in_progress=True),
        history.Outage(start=_at(20, 30), end=_at(22, 0), off_us=5_400_000_000, in_progress=False),
        history.Outage(start=_at(9, 0), end=_at(10, 0), off_us=3_600_000_000, in_progress=False),
    ]

    rows = outage_rows(outages, KYIV)

    assert rows == [
        # In progress: no end; under a minute of off time reads "<1m" (chart-spec §8).
        OutageRow(ops.instant_us(_at(20, 30)), "2026-10-01", "23:30", "", "", True, "<1m"),
        # The end falls on the next local date: it keeps its date.
        OutageRow(
            ops.instant_us(_at(20, 30)),
            "2026-10-01",
            "23:30",
            "2026-10-02",
            "01:00",
            False,
            "1h 30m",
        ),
        # The same local date: the end is a bare time.
        OutageRow(ops.instant_us(_at(9, 0)), "2026-10-01", "12:00", "", "13:00", False, "1h"),
    ]
    assert outage_rows([], KYIV) == []


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

    page = _get(admin, location)

    card = _section(page)
    assert text(by_testid(card, "outages-table").find("caption")) == "Recent outages"
    assert len(_rows(page)) == 2
    links = [href for href, _name in _remove_links(card)]
    assert links == [_remove(location, _at(15, 0)), _remove(location, _at(9, 0))]

    confirm = admin.get(links[1])
    assert confirm.status_code == 200
    # The confirmation is read as text only (06-16 rewrites it): its heading.
    assert text(h1(confirm)) == REMOVE_H1

    response = admin.post(links[1])

    assert response.status_code == 302
    assert response.url == _page(location)
    after = parse(admin.get(response.url))
    # 09:00 UTC is 12:00 in Kyiv (EEST).
    assert _flashes(after) == [("status", OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 12:00"))]
    assert len(_rows(after)) == 1
    assert len(fake_telegram.calls) == 0


# Screen A1: the Recent outages card


@pytest.mark.django_db(transaction=True)
def test_recent_outages_section_copy(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    response = admin.get(_page(location))
    page = parse(response)

    card = _section(page)
    assert INTRO in text(card)
    headers, _shown = table(card, "outages-table")
    # Outage, Off time and the visually hidden Action column, each a column header.
    assert headers == ["Outage", "Off time", "Action"]
    outages = by_testid(card, "outages-table")
    assert [th.get("scope") for th in outages.find_all("th")] == ["col", "col", "col"]
    # The off-time note (amendment A2) always follows the table; no outage is in progress.
    note = by_testid(card, "off-time-note")
    assert text(note) == OFF_TIME_NOTE
    elements = list(card.find_all(True))
    assert elements.index(outages) < elements.index(note)
    assert not all_by_testid(card, "in-progress-note")
    # E1 loading: server-rendered, no injected or inline script, no skeleton.
    assert_no_injected_script(response.content.decode(), "location page")


@pytest.mark.django_db(transaction=True)
def test_recent_outages_table_formats(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    # 20:30-22:00 UTC is 23:30-01:00 in Kyiv: it crosses local midnight.
    assert transitions.record_heartbeat(location.pk, _at(20, 30)) == "plain"
    assert detection.run_cycle(_at(20, 31, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(22, 0)) == "restored"
    _clock(monkeypatch, _at(22, 30))

    rows = _rows(_get(admin, location))

    # The end keeps its date only when it is on another local date; off time right-aligned.
    assert [row[:2] for row in rows] == [
        [f"2026-10-01 23:30{RANGE}2026-10-02 01:00", "1h 30m"],
        [f"2026-10-01 12:00{RANGE}13:00", "1h"],
    ]


@pytest.mark.django_db(transaction=True)
def test_in_progress_row_has_no_remove_link_and_adds_the_note(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    assert transitions.record_heartbeat(location.pk, _at(16, 0)) == "plain"
    assert detection.run_cycle(_at(16, 1, 31)) == 1
    _clock(monkeypatch, _at(16, 30))

    page = _get(admin, location)

    rows = _rows(page)
    assert len(rows) == 3
    # 16:00 UTC is 19:00 in Kyiv; off time counts up to now; the Action cell is empty.
    assert rows[0] == ["2026-10-01 19:00 – in progress", "30m", ""]
    shown = all_by_testid(page, "outage-row")
    assert [row.has_attr("data-in-progress") for row in shown] == [True, False, False]
    assert not all_by_testid(shown[0], "remove-outage")
    card = _section(page)
    assert len(all_by_testid(card, "remove-outage")) == 2
    note = by_testid(card, "in-progress-note")
    assert text(note) == IN_PROGRESS_NOTE
    elements = list(card.find_all(True))
    assert elements.index(by_testid(card, "off-time-note")) < elements.index(note)


@pytest.mark.django_db(transaction=True)
def test_remove_links_have_unique_accessible_names(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    card = _section(_get(admin, location))

    assert _remove_links(card) == [
        (_remove(location, _at(15, 0)), "Remove the outage from 2026-10-01 18:00"),
        (_remove(location, _at(9, 0)), "Remove the outage from 2026-10-01 12:00"),
    ]
    # Plain links to the GET confirmation (R7), never a button or a form that acts.
    for link in all_by_testid(card, "remove-outage"):
        assert link.name == "a"
        assert link.has_attr("data-confirm")
    assert card.find("button") is None
    assert card.find("form") is None


@pytest.mark.django_db
def test_recent_outages_empty_states(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    waiting = location_factory(name="Waiting")
    monitored = location_factory(name="Monitored")
    assert transitions.record_heartbeat(monitored.pk, _at(8, 0)) == "started"
    _clock(monkeypatch, _at(9, 0))

    for location, reason, line, other in (
        (waiting, "no-history", NO_HISTORY, NO_OUTAGES),
        (monitored, "none-in-14-days", NO_OUTAGES, NO_HISTORY),
    ):
        card = _section(_get(admin, location))
        assert INTRO in text(card)
        empty = by_testid(card, "outages-empty")
        assert (empty["data-reason"], text(empty)) == (reason, line)
        assert other not in text(card)
        # Neither shows a table, a note or the stats.
        assert card.find("table") is None
        for hook in ("off-time-note", "in-progress-note", "outages-stats"):
            assert not all_by_testid(card, hook), hook


@pytest.mark.django_db(transaction=True)
def test_fall_back_day_outages_both_read_03_30_and_the_confirmation_shows_the_zone(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    def oct25(hour: int, minute: int, second: int = 0) -> datetime:
        return datetime(2026, 10, 25, hour, minute, second, tzinfo=UTC)

    _no_anchors()
    location = location_factory()
    assert transitions.record_heartbeat(location.pk, oct25(0, 0)) == "started"
    # 00:30 UTC is 03:30 EEST and 01:30 UTC is 03:30 EET: the repeated hour (Pitfall 6).
    for start in (oct25(0, 30), oct25(1, 30)):
        assert transitions.record_heartbeat(location.pk, start) == "plain"
        assert detection.run_cycle(start.replace(minute=31, second=31)) == 1
        assert transitions.record_heartbeat(location.pk, start.replace(minute=45)) == "restored"
    _clock(monkeypatch, oct25(2, 0))

    rows = _rows(_get(admin, location))

    assert [row[:2] for row in rows] == [
        [f"2026-10-25 03:30{RANGE}03:45", "15m"],
        [f"2026-10-25 03:30{RANGE}03:45", "15m"],
    ]
    # The confirmation page's full times carry the zone, so the two are told apart. The
    # page is read as text only (06-16 rewrites it): its <main> shows the start and the end
    # in its own zone, and no full time of the repeated hour in the other zone.
    for start, zone in ((oct25(1, 30), "EET"), (oct25(0, 30), "EEST")):
        shown = text(main(admin.get(_remove(location, start))))
        assert f"2026-10-25 03:30:00 {zone}" in shown
        assert f"2026-10-25 03:45:00 {zone}" in shown
        assert set(FALL_BACK_TIME.findall(shown)) == {zone}


@pytest.mark.django_db
def test_recent_outages_db_error_is_the_500_page(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    def unreachable(*args: Any, **kwargs: Any) -> None:
        raise DatabaseError("could not connect to server")

    monkeypatch.setattr(history, "recent_outages", unreachable)
    admin.raise_request_exception = False

    response = admin.get(_page(location))

    # E1 error: the P1 500 page, no partial table.
    assert response.status_code == 500
    html = response.content.decode()
    assert text(h1(response)) == "Something went wrong"
    assert "<table" not in html
    assert "Recent outages" not in html
    assert "could not connect" not in html


# DATA-03 end to end: reset a location's history from its page (05-05 tracer)


@pytest.mark.django_db(transaction=True)
def test_DATA03_reset_from_the_location_page(
    admin: Client,
    monkeypatch: pytest.MonkeyPatch,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    location = _two_outages(location_factory, name="Office")
    _clock(monkeypatch, _at(16, 0))

    page = _get(admin, location)

    link = by_testid(_reset_section(page), "reset-history")
    assert (link.name, link["href"], text(link)) == ("a", _reset(location), "Reset history…")
    assert link.has_attr("data-confirm")

    confirm = admin.get(_reset(location))
    assert confirm.status_code == 200
    # The confirmation is read as text only (06-16 rewrites it): its heading.
    assert text(h1(confirm)) == RESET_H1.format(name="Office")

    response = admin.post(_reset(location))

    assert response.status_code == 302
    assert response.url == _page(location)
    after = parse(admin.get(response.url))
    assert _flashes(after) == [("status", RESET_FLASH)]
    pill = by_testid(by_testid(after, "location-header"), "status-pill")
    assert (pill["data-status"], text(pill)) == ("waiting", "Waiting for first heartbeat")
    assert ("Last heartbeat", "Never") in definitions(after, "status-panel")
    empty = by_testid(_section(after), "outages-empty")
    assert (empty["data-reason"], text(empty)) == ("no-history", NO_HISTORY)
    refused = by_testid(_reset_section(after), "reset-unavailable")
    assert (refused["data-reason"], text(refused)) == ("no-history", NOTHING_TO_RESET_LINE)
    assert _intervals(location) == []
    assert len(fake_telegram.calls) == 0


# The Danger zone's Reset history row (UI5-D9, R7)


@pytest.mark.django_db(transaction=True)
def test_reset_section_states(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    on = _two_outages(location_factory, name="On")
    paused_on = _two_outages(location_factory, name="Paused on")
    assert maintenance.set_maintenance(paused_on.pk, True, _at(15, 45)) is True
    off = _off_since_9(location_factory, name="Off")
    paused_off = _off_since_9(location_factory, name="Paused off")
    assert maintenance.set_maintenance(paused_off.pk, True, _at(9, 30)) is True
    never = location_factory(name="Never monitored")
    _clock(monkeypatch, _at(16, 0))

    def shows(location: Any, reason: str | None) -> None:
        response = admin.get(_page(location))
        row = _reset_section(parse(response))
        # The description sentence is always shown, verbatim.
        assert RESET_SENTENCE in text(row)
        # Then exactly one of: the link, the in-progress line, the no-history line.
        links = all_by_testid(row, "reset-history")
        refusals = all_by_testid(row, "reset-unavailable")
        if reason is None:
            assert [link["href"] for link in links] == [_reset(location)]
            assert refusals == []
        else:
            assert links == []
            line = RESET_IN_PROGRESS_LINE if reason == "in-progress" else NOTHING_TO_RESET_LINE
            assert [(found["data-reason"], text(found)) for found in refusals] == [(reason, line)]
            assert refusals[0]["id"] == "reset-unavailable"
        # A link to the confirmation at most: never a form or a button that acts here.
        assert row.find("form") is None
        assert row.find("button") is None
        assert_no_injected_script(response.content.decode(), "location page")

    # On, and on in maintenance: the link.
    shows(on, None)
    shows(paused_on, None)
    # Off, whatever the maintenance flag: the D-06 line and no link (D-06).
    shows(off, "in-progress")
    shows(paused_off, "in-progress")
    # No stored interval: nothing to reset and no link.
    shows(never, "no-history")
    # Waiting with history after a restore (05-03): the reset is offered.
    restore.restart_after_restore(_at(16, 0))
    assert LocationState.objects.get(location=off).status == "waiting"
    shows(on, None)
    shows(off, None)


# The Recent outages header stats and the range markup (06-UI-SPEC S5 Recent outages;
# loc.outages_stats; UI-11, UI-12)


def _one_outage(location_factory: Callable[..., Any], **fields: Any) -> Any:
    """On since 08:00, one outage 09:00-10:00 (UTC), on again since 10:00."""
    _no_anchors()
    location = location_factory(**fields)
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, _at(9, 0)) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    assert transitions.record_heartbeat(location.pk, _at(10, 0)) == "restored"
    return location


@pytest.mark.django_db(transaction=True)
def test_recent_outages_stats_count_and_total(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    two = _two_outages(location_factory, name="Two")
    one = _one_outage(location_factory, name="One")
    _clock(monkeypatch, _at(16, 0))

    # "{n} outages · {total}", the total the summed off time of the listed outages
    # (outages_total_text: 1h + 30m); "1 outage" at one.
    for location, stats in ((two, "2 outages · 1h 30m"), (one, "1 outage · 1h")):
        card = _section(_get(admin, location))
        found = by_testid(card, "outages-stats")
        assert text(found) == stats
        # The stats sit in the card's header strip, beside the title, before the table.
        elements = list(card.find_all(True))
        assert elements.index(found) < elements.index(by_testid(card, "outages-table"))
        assert len(_rows(card)) == int(stats[0])


@pytest.mark.django_db(transaction=True)
def test_recent_outages_range_keeps_the_dash_with_the_end(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    assert transitions.record_heartbeat(location.pk, _at(16, 0)) == "plain"
    assert detection.run_cycle(_at(16, 1, 31)) == 1
    _clock(monkeypatch, _at(16, 30))

    rows = all_by_testid(_get(admin, location), "outage-row")

    # Ended: no-break space, dash, word joiner, no-break space, so the range never breaks
    # after the dash; in progress: "– in progress" on one line after a no-break space.
    cells = [row.find("td") for row in rows]
    assert all(isinstance(cell, Tag) for cell in cells)
    raw = [cell.get_text() for cell in cells if isinstance(cell, Tag)]
    assert raw[0].endswith("19:00\xa0–\xa0in progress")
    assert all(value.endswith(end) for value, end in zip(raw[1:], ("18:30", "13:00"), strict=True))
    for value in raw[1:]:
        assert "\xa0–\u2060\xa0" in value
    # Dates and times are local minutes from OutageRow, not instants: no <time>, and the
    # card has no relative time.
    card = _section(_get(admin, location))
    assert card.find("time") is None
    assert not card.select("[data-relative]")
