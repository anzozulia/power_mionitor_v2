"""The Recent outages section, the removal pages and the reset pages (DATA-02, DATA-03;
05-UI-SPEC A1, A2, B, C, D, E).

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
- The Reset history section shows its sentence, then the D-06 line while the stored status
  is off, "There is no power history to reset." without history, else the link-button to
  the reset confirmation. Its POST runs ``history.reset_history`` under the row lock and
  redirects to the location page with the success, error or info flash; no Telegram call
  is made (the worker unpins the old chart later, D-08).

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
from django.db import DatabaseError
from django.test import Client

from powermon.alerts import ops, outbox
from powermon.alerts.models import OutboxMessage
from powermon.chart.models import ChartMessage
from powermon.engine import history, maintenance, restore, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations import keys
from powermon.locations.models import Location
from powermon.web.history_views import HistoryResetView, OutageRemoveView
from powermon.web.location_views import LocationDetailView, OutageRow, local_minute, outage_rows
from powermon.worker import detection, io_loop

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
REFUSED_FLASH = "This outage is still in progress. It can be removed after power returns."
# Amended 2026-10-03 (wave-1 audit, W1-A1): the outage's OFF alert is being sent.
DEFERRED_FLASH = (
    "An alert about this outage is being sent to the channel right now. Nothing changed. "
    "Try again in a minute."
)
RESET_FLASH = (
    "History reset. The location waits for its next heartbeat, which restarts monitoring "
    "without an alert. The old weekly chart is unpinned when the bot can do so; if the pin "
    "stays, unpin it by hand in Telegram."
)
RESET_REFUSED_FLASH = (
    "An outage is in progress. Reset the history after power returns, or delete the location."
)
NOTHING_TO_RESET_FLASH = "There is no power history to reset. Nothing changed."
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
# 05-UI-SPEC Copywriting › Remove-outage confirmation, verbatim (Consequence 3 as amended on
# 2026-10-03 for the refined D-04).
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
    "sends no new message to the channel. If its OFF alert was never sent, its queued OFF and "
    "ON alerts are dropped; if the OFF alert already went out, a queued ON alert is still "
    "sent, so the channel is not left at power off;"
)
CONSEQUENCE_4 = (
    "leaves the live status unchanged, including the On since time from which the next OFF "
    'alert counts "was ON for".'
)
CLOSING = (
    "The chart updates within 15 minutes. It shows the last 7 days; charts already posted for "
    "earlier days do not change."
)
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
XSS_NAME = "<script>alert(1)</script>"
ESCAPED_XSS_NAME = "&lt;script&gt;alert(1)&lt;/script&gt;"


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


def _flashes(page: str) -> list[str]:
    return [unescape(t) for t in re.findall(r'role="(?:status|alert)">([^<]*)<', page)]


def _main(page: str) -> str:
    """The page's <main>: the header (with its sign-out form) left out."""
    return page[page.index("<main") :]


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


def _crumbs(page: str) -> list[tuple[str, str]]:
    trail = re.search(r'<ol class="crumbs">(.*?)</ol>', page, re.S)
    assert trail is not None, "no breadcrumb trail"
    items = re.findall(r"<li\b([^>]*)>(.*?)</li>", trail.group(1), re.S)
    return [(attrs, inner.strip()) for attrs, inner in items]


def _panel(page: str) -> list[tuple[str, str]]:
    """The confirmation's details panel as (term, value HTML) pairs."""
    panel = re.search(r'<dl class="panel settings">(.*?)</dl>', page, re.S)
    assert panel is not None, "no details panel"
    return re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", panel.group(1), re.S)


def _consequences(page: str) -> list[str]:
    found = re.search(r'<ul class="list">(.*?)</ul>', page, re.S)
    assert found is not None, "no consequence list"
    return [unescape(item) for item in re.findall(r"<li>(.*?)</li>", found.group(1), re.S)]


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
    text = unescape(section)
    assert text.index(OFF_TIME_NOTE) < text.index(IN_PROGRESS_NOTE)


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
    # The confirmation page's full times carry the zone, so the two are told apart.
    for start, zone in ((oct25(1, 30), "EET"), (oct25(0, 30), "EEST")):
        page = _main(admin.get(_remove(location, start)).content.decode())
        assert _panel(page)[:2] == [
            ("Start", f'<span class="num">2026-10-25 03:30:00 {zone}</span>'),
            ("End", f'<span class="num">2026-10-25 03:45:00 {zone}</span>'),
        ]


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
    assert "<h1>Something went wrong</h1>" in html
    assert "<table" not in html
    assert "Recent outages" not in html
    assert "could not connect" not in html


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

    assert response.status_code == 200
    html = response.content.decode()
    page = _main(html)
    assert "<title>Office · Remove outage · Power Monitor</title>" in html
    assert _crumbs(page) == [
        ("", '<a href="/">Locations</a>'),
        ("", f'<a class="name" href="/locations/{location.pk}/">Office</a>'),
        (' aria-current="page"', "Remove outage"),
    ]
    assert "<h1>Remove this outage?</h1>" in page
    assert _panel(page) == [
        ("Start", '<span class="num">2026-10-01 12:00:00 EEST</span>'),
        ("End", '<span class="num">2026-10-01 13:00:00 EEST</span>'),
        ("Off time", '<span class="num">1h</span>'),
    ]
    assert f"<p>{LEAD}</p>" in page
    # No not-monitored time inside this outage: no Consequence 2.
    assert _consequences(page) == [CONSEQUENCE_1, CONSEQUENCE_3, CONSEQUENCE_4]
    # Consequence 3 states the refined D-04 (05-UI-SPEC, amended 2026-10-03).
    assert _consequences(page)[1] == (
        "sends no new message to the channel. If its OFF alert was never sent, its queued OFF "
        "and ON alerts are dropped; "
        "if the OFF alert already went out, a queued ON alert is still sent, so the channel is not left at power off;"  # noqa: E501
    )
    assert f"<p>{CLOSING}</p>" in page
    assert re.findall(r"<form\b[^>]*>", page) == [f'<form method="post" action="{url}">']
    button = '<button class="btn btn--danger" type="submit">Remove outage</button>'
    keep = f'<a class="btn btn--secondary" href="/locations/{location.pk}/">Keep outage</a>'
    assert page.index(button) < page.index(keep)
    assert page.count("btn--danger") == 1
    assert "btn--primary" not in page
    assert "autofocus" not in page
    assert "<script" not in html
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

    page = _main(admin.get(_remove(paused, _at(9, 0))).content.decode())

    # 09:00-11:00 with 10:00-10:10 not monitored: off time is shorter than the span.
    assert _panel(page) == [
        ("Start", '<span class="num">2026-10-01 12:00:00 EEST</span>'),
        ("End", '<span class="num">2026-10-01 14:00:00 EEST</span>'),
        ("Off time", '<span class="num">1h 50m</span>'),
    ]
    assert _consequences(page) == [CONSEQUENCE_1, CONSEQUENCE_2, CONSEQUENCE_3, CONSEQUENCE_4]
    other = _main(admin.get(_remove(plain, _at(15, 0))).content.decode())
    assert CONSEQUENCE_2 not in _consequences(other)


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
    assert f'<p class="callout callout--error" role="alert">{REFUSED_FLASH}</p>' in page

    missing = admin.get(_remove(location, _at(8, 30)))

    assert missing.status_code == 302
    assert missing.url == _page(location)
    page = admin.get(missing.url).content.decode()
    assert f'<p class="callout" role="status">{GONE_FLASH}</p>' in page
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
        assert _flashes(page) == [REFUSED_FLASH]
        assert f'<p class="callout callout--error" role="alert">{REFUSED_FLASH}</p>' in page
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
    assert _flashes(page) == [DEFERRED_FLASH]
    assert f'<p class="callout" role="status">{DEFERRED_FLASH}</p>' in page
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
        REMOVED_FLASH.format(start="2026-10-01 18:00")
    ]
    after = _written(location)

    second = admin.post(url)

    assert second.status_code == 302
    assert second.url == _page(location)
    page = admin.get(second.url).content.decode()
    assert _flashes(page) == [GONE_FLASH]
    assert f'<p class="callout" role="status">{GONE_FLASH}</p>' in page
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
        REMOVED_FLASH.format(start="2026-10-01 12:00")
    ]
    kyiv.TIME_ZONE = "UTC"
    second = admin.post(_remove(location, _at(11, 0, 59)))
    assert _flashes(admin.get(second.url).content.decode()) == [
        REMOVED_FLASH.format(start="2026-10-01 11:00")
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
        assert admin.get(url).status_code == 404, url[:80]
        assert admin.post(url).status_code == 404, url[:80]

    assert (_written(location), _written(gone)) == before


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

    html = admin.get(_remove(location, _at(9, 0))).content.decode()

    assert f"<title>{ESCAPED_XSS_NAME} · Remove outage · Power Monitor</title>" in html
    assert _crumbs(html)[1] == (
        "",
        f'<a class="name" href="/locations/{location.pk}/">{ESCAPED_XSS_NAME}</a>',
    )
    # The h1 carries no name (UI5-D13), and nothing renders as a script.
    assert "<h1>Remove this outage?</h1>" in html
    assert "<script" not in html


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
        [REMOVED_FLASH.format(start="2026-10-01 12:00")],
        [GONE_FLASH],
        [REFUSED_FLASH],
    ]

    for html in (detail, confirm, *flashes):
        assert key not in html
        assert keys.mask_key(key) not in html
        assert TOKEN not in html
        assert SECRET not in html
    # The confirmation page has no settings panel: not even the masked token.
    assert "•" not in _main(confirm)
    for response in flash_pages:
        for url, _status in response.redirect_chain:
            assert key not in url
            assert SECRET not in url


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
    assert '<h1 class="name">Reset the history of Office?</h1>' in confirm.content.decode()

    response = admin.post(_reset(location))

    assert response.status_code == 302
    assert response.url == _page(location)
    after = admin.get(response.url).content.decode()
    assert _flashes(after) == [RESET_FLASH]
    main = _main(after)
    assert "Waiting for first heartbeat" in main
    assert '<dd><span class="num">Never</span></dd>' in main
    assert f"<p>{NO_HISTORY}</p>" in _section(main)
    assert f"<p>{NOTHING_TO_RESET_LINE}</p>" in _reset_section(main)
    assert _intervals(location) == []
    assert len(fake_telegram.calls) == 0


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
    assert _flashes(page) == [RESET_REFUSED_FLASH]
    assert f'<p class="callout callout--error" role="alert">{RESET_REFUSED_FLASH}</p>' in page
    assert _written(location) == before
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
        text = section(location)
        # The description sentence is always shown, verbatim.
        assert f"<p>{RESET_SENTENCE}</p>" in text
        # Then exactly one of: the link-button, the D-06 line, the no-history line.
        lines = [f"<p>{RESET_IN_PROGRESS_LINE}</p>", f"<p>{NOTHING_TO_RESET_LINE}</p>"]
        for candidate in lines:
            assert (candidate in text) == (candidate == line), candidate
        assert (_reset_link(location) in text) == (line is None)
        # A secondary link-button at most: never a form or a destructive button here.
        assert "<form" not in text
        assert "btn--danger" not in text
        assert "<script" not in text

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

    assert response.status_code == 200
    html = response.content.decode()
    page = _main(html)
    assert "<title>Office · Reset history · Power Monitor</title>" in html
    assert _crumbs(page) == [
        ("", '<a href="/">Locations</a>'),
        ("", f'<a class="name" href="/locations/{location.pk}/">Office</a>'),
        (' aria-current="page"', "Reset history"),
    ]
    assert '<h1 class="name">Reset the history of Office?</h1>' in page
    assert f"<p>{RESET_LEAD}</p>" in page
    assert _consequences(page) == RESET_CONSEQUENCES
    assert f"<p>{RESET_ALTERNATIVE}</p>" in page
    assert page.index(RESET_LEAD) < page.index('<ul class="list">') < page.index(RESET_ALTERNATIVE)
    assert re.findall(r"<form\b[^>]*>", page) == [f'<form method="post" action="{url}">']
    button = '<button class="btn btn--danger" type="submit">Reset history</button>'
    keep = f'<a class="btn btn--secondary" href="/locations/{location.pk}/">Keep history</a>'
    assert page.index(RESET_ALTERNATIVE) < page.index(button) < page.index(keep)
    assert page.count("btn--danger") == 1
    assert "btn--primary" not in page
    assert "autofocus" not in page
    assert "<script" not in html
    # No settings panel: not even the masked token.
    assert '<dl class="panel settings">' not in page
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
        assert _flashes(page) == [RESET_REFUSED_FLASH]
        assert f'<p class="callout callout--error" role="alert">{RESET_REFUSED_FLASH}</p>' in page

        nothing = admin.get(_reset(never))

        assert nothing.status_code == 302
        assert nothing.url == _page(never)
        page = admin.get(nothing.url).content.decode()
        assert _flashes(page) == [NOTHING_TO_RESET_FLASH]
        assert f'<p class="callout" role="status">{NOTHING_TO_RESET_FLASH}</p>' in page
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
        assert _flashes(page) == [RESET_REFUSED_FLASH]
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
    assert _flashes(page) == [RESET_FLASH]
    assert f'<p class="callout" role="status">{RESET_FLASH}</p>' in page
    after = _written(location)

    second = admin.post(_reset(location))

    assert second.status_code == 302
    assert second.url == _page(location)
    page = admin.get(second.url).content.decode()
    assert _flashes(page) == [NOTHING_TO_RESET_FLASH]
    assert f'<p class="callout" role="status">{NOTHING_TO_RESET_FLASH}</p>' in page
    assert _written(location) == after
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_reset_flashes_stack_in_queue_order(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory)
    _clock(monkeypatch, _at(16, 0))

    # Two actions before the page is read: each flash in its own callout, in order (E5).
    admin.post(_reset(location))
    admin.post(_reset(location))

    page = admin.get(_page(location)).content.decode()
    assert _flashes(page) == [RESET_FLASH, NOTHING_TO_RESET_FLASH]
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

    html = admin.get(_reset(location)).content.decode()

    assert f"<title>{ESCAPED_XSS_NAME} · Reset history · Power Monitor</title>" in html
    assert f'<h1 class="name">Reset the history of {ESCAPED_XSS_NAME}?</h1>' in html
    assert _crumbs(html)[1] == (
        "",
        f'<a class="name" href="/locations/{location.pk}/">{ESCAPED_XSS_NAME}</a>',
    )
    assert "<script" not in html
    # E4 long-text: a 100-character name is shown whole, never truncated.
    page = admin.get(_reset(long)).content.decode()
    assert f"<title>{name} · Reset history · Power Monitor</title>" in page
    assert f'<h1 class="name">Reset the history of {name}?</h1>' in page
    assert _crumbs(page) == [
        ("", '<a href="/">Locations</a>'),
        ("", f'<a class="name" href="/locations/{long.pk}/">{name}</a>'),
        (' aria-current="page"', "Reset history"),
    ]


@pytest.mark.django_db(transaction=True)
def test_reset_pages_show_no_secret(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = _two_outages(location_factory, name="Office", bot_token=TOKEN)
    off = _off_since_9(location_factory, name="Off", bot_token=TOKEN)
    _clock(monkeypatch, _at(16, 0))
    keys_shown = (location.device_key, off.device_key)

    detail = admin.get(_page(location)).content.decode()
    section = _reset_section(_main(detail))
    confirm = admin.get(_reset(location)).content.decode()
    flash_pages = [
        # Reset (success), nothing to reset (info), refused during an outage (error).
        admin.post(_reset(location), follow=True),
        admin.post(_reset(location), follow=True),
        admin.post(_reset(off), follow=True),
    ]
    flashes = [response.content.decode() for response in flash_pages]
    assert [_flashes(page) for page in flashes] == [
        [RESET_FLASH],
        [NOTHING_TO_RESET_FLASH],
        [RESET_REFUSED_FLASH],
    ]

    for html in (detail, section, confirm, *flashes):
        for key in keys_shown:
            assert key not in html
            assert keys.mask_key(key) not in html
        assert TOKEN not in html
        assert SECRET not in html
    # The confirmation page has no settings panel: not even the masked token.
    assert "•" not in _main(confirm)
    for response in flash_pages:
        for url, _status in response.redirect_chain:
            for key in keys_shown:
                assert key not in url
            assert SECRET not in url
