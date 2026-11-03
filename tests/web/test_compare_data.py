"""The Compare page's reads (quick task 261103-lfm, D-10): ``compare_data.load_series``.

- Only the pieces overlapping P come back, in start order, one series per live row in the
  rows' order. Whole outages carry their full off time (also outside P), the same number
  as ``history.group_outages`` and the location page's Recent outages; the current outage
  has no end.
- Both reads run in one read-only REPEATABLE READ snapshot when outermost, and inside a
  caller's transaction without the ``SET``.
- An orphaned off piece is dropped with one WARNING (count and id, never the name).
- The page's data reads are at most 4 queries (live rows: 2, here: 2) whatever the number
  of locations; empty input and a naive ``now`` run none.

The rows are written as the engine would leave them, with a small local helper (the chart
helpers in tests/chart are not importable from here).
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from powermon.chart import compare, model
from powermon.engine import history
from powermon.engine.models import LocationState, PowerInterval
from powermon.web import compare_data, live

KYIV = "Europe/Kyiv"
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
NAIVE = datetime(2026, 10, 1, 16, 0)  # noqa: DTZ001
H = model.HOUR_US
LOGGER = "powermon.web.compare_data"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _store(location: Any, rows: Sequence[tuple[str, str, str | None, str | None]]) -> None:
    """``(state, start, end or None, outage start or None)`` rows as power_interval rows."""
    for state, start, end, outage in rows:
        PowerInterval.objects.create(
            location=location,
            state=state,
            start_at=_utc(start),
            end_at=None if end is None else _utc(end),
            outage_start_at=None if outage is None else _utc(outage),
        )


def _period(first: date, last: date, now: datetime = NOW) -> compare.Period:
    return compare.period_of(first, last, now, KYIV)


def _row(pk: int) -> live.LiveRow:
    return next(r for r in live.live_rows(NOW) if r.pk == pk)


WEEK = (date(2026, 9, 25), date(2026, 10, 1))


@pytest.mark.django_db
def test_load_series_reads_only_pieces_overlapping_p(location_factory: Callable[..., Any]) -> None:
    a = location_factory(name="Alpha")
    b = location_factory(name="Bravo")
    _store(
        a,
        [
            ("on", "2026-09-20 00:00", "2026-09-23 00:00", None),  # before P
            ("on", "2026-09-23 00:00", "2026-09-25 00:00", None),  # across P's start
            ("off", "2026-09-25 00:00", "2026-09-25 01:00", "2026-09-25 00:00"),  # inside
            ("on", "2026-09-25 01:00", "2026-09-28 00:00", None),  # across P's end
            ("on", "2026-09-28 00:00", None, None),  # after P
        ],
    )
    _store(b, [("on", "2026-09-26 00:00", "2026-09-26 12:00", None)])
    period = _period(date(2026, 9, 25), date(2026, 9, 27))

    series = compare_data.load_series([_row(b.pk), _row(a.pk)], period, NOW)

    assert [(s.key, s.name) for s in series] == [(b.pk, "Bravo"), (a.pk, "Alpha")]
    assert [p.start for p in series[1].pieces] == [
        _utc("2026-09-23 00:00"),
        _utc("2026-09-25 00:00"),
        _utc("2026-09-25 01:00"),
    ]
    assert series[1].outages == (
        compare.OutageRec(_utc("2026-09-25 00:00"), _utc("2026-09-25 01:00"), 1 * H),
    )
    assert len(series[0].pieces) == 1 and series[0].outages == ()
    # The reads feed the pure comparison as they are.
    result = compare.compare(series, period, NOW, KYIV)
    assert result.locations[1].off_us == 1 * H


@pytest.mark.django_db
def test_whole_outage_matches_group_outages_and_recent_outages(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory(name="Alpha")
    start = "2026-09-21 20:00"
    _store(
        location,
        [
            ("off", start, "2026-09-21 22:00", start),
            ("not_monitored", "2026-09-21 22:00", "2026-09-24 20:00", None),
            ("off", "2026-09-24 20:00", "2026-09-24 22:00", start),
            ("on", "2026-09-24 22:00", None, None),
        ],
    )

    (series,) = compare_data.load_series([_row(location.pk)], _period(*WEEK), NOW)

    (rec,) = series.outages
    assert rec == compare.OutageRec(_utc(start), _utc("2026-09-24 22:00"), 4 * H)
    grouped = history.group_outages(
        [
            (_utc(start), _utc("2026-09-21 22:00"), _utc(start)),
            (_utc("2026-09-24 20:00"), _utc("2026-09-24 22:00"), _utc(start)),
        ],
        now=NOW,
        current=None,
    )
    recent = history.recent_outages(location.pk, NOW, KYIV).outages
    assert grouped[0].off_us == rec.off_us
    assert [o.off_us for o in recent if o.start == _utc(start)] == [rec.off_us]
    # Only the in-P off piece is a piece; the earlier one counts only in the whole outage.
    assert [p.state for p in series.pieces] == ["off", "on"]


@pytest.mark.django_db
def test_current_outage_has_no_end(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Alpha")
    _store(
        location,
        [
            ("on", "2026-09-30 00:00", "2026-10-01 14:00", None),
            ("off", "2026-10-01 14:00", None, "2026-10-01 14:00"),
        ],
    )
    LocationState.objects.filter(location=location).update(
        status="off", outage_started_at=_utc("2026-10-01 14:00")
    )
    row = _row(location.pk)
    assert row.power == "off"

    (series,) = compare_data.load_series([row], _period(*WEEK), NOW)

    assert series.outages == (compare.OutageRec(_utc("2026-10-01 14:00"), None, 2 * H),)


@pytest.mark.django_db(transaction=True)
def test_reads_run_in_one_read_only_snapshot(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Alpha")
    _store(location, [("on", "2026-09-30 00:00", None, None)])
    rows = [_row(location.pk)]
    assert not connection.in_atomic_block

    with CaptureQueriesContext(connection) as queries:
        compare_data.load_series(rows, _period(*WEEK), NOW)

    sql = [q["sql"] for q in queries.captured_queries]
    # Django 5.2 also records the transaction's BEGIN and COMMIT: one transaction whose
    # first statement is the SET, then the two reads.
    assert sql[0] == "BEGIN" and sql[-1] == "COMMIT"
    statements = sql[1:-1]
    assert len(statements) == 3
    assert statements[0] == compare_data.READ_SNAPSHOT_SQL
    assert statements[1].startswith(compare_data.PIECES_SQL.split("%(")[0])
    assert statements[2].startswith(compare_data.OUTAGES_SQL.split("%(")[0])


@pytest.mark.django_db(transaction=True)
def test_reads_inside_a_callers_transaction_issue_no_set(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory(name="Alpha")
    rows = [_row(location.pk)]

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        with CaptureQueriesContext(connection) as queries:
            series = compare_data.load_series(rows, _period(*WEEK), NOW)

    sql = [q["sql"] for q in queries.captured_queries]
    assert len(sql) == 2
    assert not any(s.startswith("SET") for s in sql)
    assert series[0].pieces == ()


def test_attach_drops_an_orphaned_off_piece(caplog: pytest.LogCaptureFixture) -> None:
    known = _utc("2026-09-25 01:00")
    pieces = (
        model.Piece("on", _utc("2026-09-25 00:00"), known, None),
        model.Piece("off", known, _utc("2026-09-25 02:00"), known),
        model.Piece(
            "off", _utc("2026-09-25 02:00"), _utc("2026-09-25 03:00"), _utc("2026-09-25 02:00")
        ),
    )
    outages = (compare.OutageRec(known, _utc("2026-09-25 02:00"), 1 * H),)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        kept = compare_data._attach(42, pieces, outages)

    assert kept == pieces[:2]
    records = [r for r in caplog.records if r.name == LOGGER]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert records[0].getMessage() == "compare: dropped 1 orphaned off piece(s) of location 42"


def test_attach_keeps_everything_without_orphans(caplog: pytest.LogCaptureFixture) -> None:
    pieces = (model.Piece("on", _utc("2026-09-25 00:00"), None, None),)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert compare_data._attach(1, pieces, ()) == pieces
    assert [r for r in caplog.records if r.name == LOGGER] == []


@pytest.mark.django_db
def test_load_series_never_raises_for_an_orphan(
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory(name="Secret Name")
    _store(
        location,
        [
            ("on", "2026-09-30 00:00", "2026-09-30 01:00", None),
            ("off", "2026-09-30 01:00", "2026-09-30 02:00", "2026-09-30 01:00"),
            ("on", "2026-09-30 02:00", "2026-09-30 03:00", None),
            ("off", "2026-09-30 03:00", "2026-09-30 04:00", "2026-09-30 03:00"),
            ("on", "2026-09-30 04:00", None, None),
        ],
    )
    real = history.group_outages

    def lose_one(*args: Any, **kwargs: Any) -> list[history.Outage]:
        return [o for o in real(*args, **kwargs) if o.start != _utc("2026-09-30 03:00")]

    monkeypatch.setattr(history, "group_outages", lose_one)
    period = _period(*WEEK)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        (series,) = compare_data.load_series([_row(location.pk)], period, NOW)

    assert [p.state for p in series.pieces] == ["on", "off", "on", "on"]
    result = compare.compare([series], period, NOW, KYIV)
    assert (result.locations[0].off_us, result.locations[0].outage_count) == (1 * H, 1)
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert messages == [f"compare: dropped 1 orphaned off piece(s) of location {location.pk}"]
    assert "Secret Name" not in messages[0]


@pytest.mark.django_db
def test_empty_rows_and_empty_period_run_no_query(
    location_factory: Callable[..., Any], django_assert_num_queries: Any
) -> None:
    location = location_factory(name="Alpha")
    rows = [_row(location.pk)]
    future = _period(date(2026, 10, 5), date(2026, 10, 6))
    assert future.length_us == 0

    with django_assert_num_queries(0):
        assert compare_data.load_series([], _period(*WEEK), NOW) == []
        assert compare_data.load_series(rows, future, NOW) == [
            compare.Series(location.pk, "Alpha", (), ())
        ]


@pytest.mark.django_db
def test_naive_now_raises_before_any_query(
    location_factory: Callable[..., Any], django_assert_num_queries: Any
) -> None:
    rows = [_row(location_factory(name="Alpha").pk)]
    with django_assert_num_queries(0), pytest.raises(ValueError):
        compare_data.load_series(rows, _period(*WEEK), NAIVE)


@pytest.mark.django_db
def test_page_reads_stay_within_four_queries(
    location_factory: Callable[..., Any], django_assert_max_num_queries: Any
) -> None:
    for i in range(6):
        location = location_factory(name=f"Loc {i}")
        _store(
            location,
            [
                ("on", "2026-09-20 00:00", "2026-09-30 10:00", None),
                ("off", "2026-09-30 10:00", "2026-09-30 11:00", "2026-09-30 10:00"),
                ("on", "2026-09-30 11:00", None, None),
            ],
        )
    period = _period(*WEEK)

    with django_assert_max_num_queries(4):
        rows = live.live_rows(NOW)
        series = compare_data.load_series(rows, period, NOW)

    assert len(series) == 6
    assert all(len(s.outages) == 1 for s in series)


def test_sql_uses_named_parameters_only() -> None:
    for sql in (compare_data.PIECES_SQL, compare_data.OUTAGES_SQL):
        for name in ("%(ids)s", "%(a)s", "%(b)s"):
            assert name in sql
        assert "{" not in sql
        assert "%s" not in sql
