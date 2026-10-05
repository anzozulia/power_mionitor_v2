"""The location page's Recent outages section and its Reset history row (DATA-02, DATA-03;
05-UI-SPEC A1, A2).

- The location page lists the outages of the last 14 local days, newest first, one row per
  outage start, in a ``.table-wrap`` table: "Outage" (start – end), "Off time" (the chart's
  totals format) and a visually hidden "Action" column with a Remove link per ended outage.
  The current outage reads "in progress" and has no link. Without outages it shows one of
  the two empty lines (UI5-D10).
- The Reset history section shows its sentence, then the D-06 line while the stored status
  is off, "There is no power history to reset." without history, else the link-button to
  the reset confirmation.
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
fixture. Copy strings are 05-UI-SPEC's, verbatim; they contain double quotes, so pages are
compared after ``html.unescape``. Flashes are read through ``pages.messages()`` as
(role, text), toast or legacy callout alike (UI-09).
"""

# class-guard: pending migration

import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from typing import Any

import pytest
from conftest import FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import Client
from pages import h1, main, messages, text

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
# 05-UI-SPEC Copywriting › Reset history, verbatim.
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
# 05-UI-SPEC Copywriting › Recent outages, verbatim.
INTRO = (
    "Outages in the last 14 days, newest first. Remove an outage that was not a real power "
    "cut, for example when the device or its internet connection was down while the power "
    "was on."
)
OFF_TIME_NOTE = (
    "Off time counts only time recorded as power off, as the chart's daily totals do. Time "
    "that was not monitored (maintenance, server downtime) is left out, so off time can be "
    'shorter than the span from start to end and than the ON alert\'s "was OFF for". The end '
    "shown is the last time recorded as power off, so it can be earlier than the ON alert "
    "when power came back while the location was not monitored."
)
IN_PROGRESS_NOTE = "The outage in progress can be removed after power returns."
NO_OUTAGES = "No outages in the last 14 days."
NO_HISTORY = (
    "No power history yet. Outages are listed here once the device has sent its first heartbeat."
)
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
    """Each flash on a page as (role, text), toast or legacy callout alike (UI-09)."""
    return [(flash.role, flash.text) for flash in messages(page)]


def _main(page: str) -> str:
    """The page's <main>: the header (with its sign-out form) left out."""
    return page[page.index("<main") :]


def _h1_text(page: str) -> str:
    """The page's first h1 as text: tags dropped, whitespace collapsed, entities decoded.

    So an h1 with attributes or an aria-hidden icon inside still reads as its copy.
    """
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S)
    assert match is not None, "no h1 on the page"
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())


def _section(page: str) -> str:
    """The Recent outages section of the location page."""
    return page[page.index("<h2>Recent outages</h2>") : page.index("<h2>Settings</h2>")]


def _reset_section(page: str) -> str:
    """The Reset history section of the location page."""
    return page[page.index("<h2>Reset history</h2>") : page.index("<h2>Delete location</h2>")]


def _rows(page: str) -> list[str]:
    """The Recent outages table's body rows (inner HTML), in order."""
    body = re.search(r"<tbody>(.*?)</tbody>", _section(page), re.S)
    return [] if body is None else re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S)


def _cells(row: str) -> list[str]:
    """The cells (inner HTML) of one table row."""
    return re.findall(r"<td>(.*?)</td>", row, re.S)


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

    page = _main(admin.get(_page(location)).content.decode())

    assert "<h2>Recent outages</h2>" in page
    assert len(_rows(page)) == 2
    links = re.findall(r'<a href="([^"]+)">Remove<span', _section(page))
    assert links == [_remove(location, _at(15, 0)), _remove(location, _at(9, 0))]

    confirm = admin.get(links[1])
    assert confirm.status_code == 200
    # The confirmation is read as text only (06-16 rewrites it): its heading.
    assert text(h1(confirm)) == REMOVE_H1

    response = admin.post(links[1])

    assert response.status_code == 302
    assert response.url == _page(location)
    after = admin.get(response.url).content.decode()
    # 09:00 UTC is 12:00 in Kyiv (EEST).
    assert _flashes(after) == [("status", OUTAGE_REMOVED_MESSAGE.format(start="2026-10-01 12:00"))]
    assert len(_rows(after)) == 1
    assert len(fake_telegram.calls) == 0


# Screen A1: the Recent outages section


@pytest.mark.django_db(transaction=True)
def test_recent_outages_section_copy(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    html = admin.get(_page(location)).content.decode()

    section = _section(_main(html))
    assert f"<p>{INTRO}</p>" in section
    assert '<div class="table-wrap">\n<table>' in section
    assert re.findall(r"<th\b([^>]*)>(.*?)</th>", section) == [
        (' scope="col"', "Outage"),
        (' scope="col"', "Off time"),
        (' scope="col"', '<span class="visually-hidden">Action</span>'),
    ]
    # The off-time note always follows the table; no outage is in progress.
    note = f'<p class="help">{OFF_TIME_NOTE}</p>'
    assert note in unescape(section)
    assert unescape(section).index("</table>") < unescape(section).index(note)
    assert IN_PROGRESS_NOTE not in section
    # E1 loading: server-rendered, no script, no skeleton.
    assert "<script" not in html


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

    rows = _rows(_main(admin.get(_page(location)).content.decode()))

    assert [_cells(row)[:2] for row in rows] == [
        [
            '<span class="num">2026-10-01</span> <span class="num">23:30</span> – '
            '<span class="num">2026-10-02</span> <span class="num">01:00</span>',
            '<span class="num">1h 30m</span>',
        ],
        [
            '<span class="num">2026-10-01</span> <span class="num">12:00</span> – '
            '<span class="num">13:00</span>',
            '<span class="num">1h</span>',
        ],
    ]


@pytest.mark.django_db(transaction=True)
def test_in_progress_row_has_no_remove_link_and_adds_the_note(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    assert transitions.record_heartbeat(location.pk, _at(16, 0)) == "plain"
    assert detection.run_cycle(_at(16, 1, 31)) == 1
    _clock(monkeypatch, _at(16, 30))

    page = _main(admin.get(_page(location)).content.decode())

    rows = _rows(page)
    assert len(rows) == 3
    # 16:00 UTC is 19:00 in Kyiv; off time counts up to now; the Remove cell is empty.
    assert _cells(rows[0]) == [
        '<span class="num">2026-10-01</span> <span class="num">19:00</span> – in progress',
        '<span class="num">30m</span>',
        "",
    ]
    section = _section(page)
    assert section.count(">Remove<") == 2
    assert f'<p class="help">{IN_PROGRESS_NOTE}</p>' in section
    shown = unescape(section)
    assert shown.index(OFF_TIME_NOTE) < shown.index(IN_PROGRESS_NOTE)


@pytest.mark.django_db(transaction=True)
def test_remove_links_have_unique_accessible_names(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    section = _section(_main(admin.get(_page(location)).content.decode()))

    assert re.findall(r'<td><a href="([^"]+)">(.*?)</a></td>', section) == [
        (
            _remove(location, _at(15, 0)),
            'Remove<span class="visually-hidden"> the outage from 2026-10-01 18:00</span>',
        ),
        (
            _remove(location, _at(9, 0)),
            'Remove<span class="visually-hidden"> the outage from 2026-10-01 12:00</span>',
        ),
    ]
    # Plain accent text links, never buttons (UI5-D6).
    assert "btn" not in section


@pytest.mark.django_db
def test_recent_outages_empty_states(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    waiting = location_factory(name="Waiting")
    monitored = location_factory(name="Monitored")
    assert transitions.record_heartbeat(monitored.pk, _at(8, 0)) == "started"
    _clock(monkeypatch, _at(9, 0))

    for location, line, other in (
        (waiting, NO_HISTORY, NO_OUTAGES),
        (monitored, NO_OUTAGES, NO_HISTORY),
    ):
        section = _section(_main(admin.get(_page(location)).content.decode()))
        assert f"<p>{INTRO}</p>" in section
        assert f"<p>{line}</p>" in section
        assert other not in section
        # Neither shows a table or a note.
        assert "<table" not in section
        assert "Off time counts" not in section
        assert IN_PROGRESS_NOTE not in section


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

    rows = _rows(_main(admin.get(_page(location)).content.decode()))

    same = '<span class="num">2026-10-25</span> <span class="num">03:30</span> – '
    assert [_cells(row)[:2] for row in rows] == [
        [same + '<span class="num">03:45</span>', '<span class="num">15m</span>'],
        [same + '<span class="num">03:45</span>', '<span class="num">15m</span>'],
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
    assert _h1_text(html) == "Something went wrong"
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

    page = _main(admin.get(_page(location)).content.decode())

    section = _reset_section(page)
    assert (
        f'<p><a class="btn btn--secondary" href="{_reset(location)}">Reset history</a></p>'
        in section
    )

    confirm = admin.get(_reset(location))
    assert confirm.status_code == 200
    # The confirmation is read as text only (06-16 rewrites it): its heading.
    assert text(h1(confirm)) == RESET_H1.format(name="Office")

    response = admin.post(_reset(location))

    assert response.status_code == 302
    assert response.url == _page(location)
    after = admin.get(response.url).content.decode()
    assert _flashes(after) == [("status", RESET_FLASH)]
    main_html = _main(after)
    assert "Waiting for first heartbeat" in main_html
    assert '<dd><span class="num">Never</span></dd>' in main_html
    assert f"<p>{NO_HISTORY}</p>" in _section(main_html)
    assert f"<p>{NOTHING_TO_RESET_LINE}</p>" in _reset_section(main_html)
    assert _intervals(location) == []
    assert len(fake_telegram.calls) == 0


# Screen A2: the Reset history section (UI5-D9)


def _reset_link(location: Any) -> str:
    return f'<p><a class="btn btn--secondary" href="{_reset(location)}">Reset history</a></p>'


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

    def section(location: Any) -> str:
        return _reset_section(_main(admin.get(_page(location)).content.decode()))

    def shows(location: Any, line: str | None) -> None:
        shown = section(location)
        # The description sentence is always shown, verbatim.
        assert f"<p>{RESET_SENTENCE}</p>" in shown
        # Then exactly one of: the link-button, the D-06 line, the no-history line.
        lines = [f"<p>{RESET_IN_PROGRESS_LINE}</p>", f"<p>{NOTHING_TO_RESET_LINE}</p>"]
        for candidate in lines:
            assert (candidate in shown) == (candidate == line), candidate
        assert (_reset_link(location) in shown) == (line is None)
        # A secondary link-button at most: never a form or a destructive button here.
        assert "<form" not in shown
        assert "btn--danger" not in shown
        assert "<script" not in shown

    # On, and on in maintenance: the link-button.
    shows(on, None)
    shows(paused_on, None)
    # Off, whatever the maintenance flag: the D-06 line and no link (D-06).
    shows(off, f"<p>{RESET_IN_PROGRESS_LINE}</p>")
    shows(paused_off, f"<p>{RESET_IN_PROGRESS_LINE}</p>")
    # No stored interval: nothing to reset and no link.
    shows(never, f"<p>{NOTHING_TO_RESET_LINE}</p>")
    # Waiting with history after a restore (05-03): the reset is offered.
    restore.restart_after_restore(_at(16, 0))
    assert LocationState.objects.get(location=off).status == "waiting"
    shows(on, None)
    shows(off, None)
