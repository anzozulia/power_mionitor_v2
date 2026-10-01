"""``timeline.overwrite``: a lapse window becomes not monitored, nothing else (MON-05, D-04).

The lapse carve rewrites the stored timeline over ``[a, b)`` with one call per location
(RESEARCH Pattern 4, verified in spike 11). These cases run on the real constraints
(CHECKs, the one-open unique index and the no-overlap exclusion constraint), each inside
``transaction.atomic()`` with the location's ``location_state`` row locked, which is what
every caller does:

- Time the location had data for inside ``[a, b)`` becomes ``not_monitored``; time with no
  data (before the first heartbeat, K-1) stays no data.
- Pieces outside the window are untouched, and the pieces it splits end at ``a`` and
  resume at ``b`` with no gap and no overlap (adjacency).
- An off piece split by the window keeps its ``outage_start_at`` on both sides, so an
  outage in progress stays one outage (INV-11 #2, D-02).
- A piece already in the target state is skipped, so a re-run is a no-op and a later
  ``b`` only appends (crash mid-carve, then a re-run).
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import connection, transaction

from powermon.engine import timeline
from powermon.engine.models import PowerInterval

Interval = tuple[str, datetime, datetime | None, datetime | None]

pytestmark = pytest.mark.django_db


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


A, B = _at(10, 0), _at(10, 10)


def _insert(location: Any, state: str, start: datetime, end: datetime | None, **extra: Any) -> int:
    row = PowerInterval.objects.create(
        location=location, state=state, start_at=start, end_at=end, **extra
    )
    return row.pk


def _intervals(location: Any) -> list[Interval]:
    """The location's stored intervals as (state, start_at, end_at, outage_start_at)."""
    rows = PowerInterval.objects.filter(location=location).order_by("start_at")
    return [(r.state, r.start_at, r.end_at, r.outage_start_at) for r in rows]


def _ids(location: Any) -> list[int]:
    return list(
        PowerInterval.objects.filter(location=location)
        .order_by("start_at")
        .values_list("pk", flat=True)
    )


def _overwrite(
    location: Any, a: datetime = A, b: datetime = B, state: str = "not_monitored"
) -> int:
    """``timeline.overwrite`` as the carve calls it: row lock first, one transaction."""
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT 1 FROM location_state WHERE location_id = %s FOR UPDATE", [location.pk])
        return timeline.overwrite(cur, location.pk, a, b, state)


# Expected shapes (spike 11 cases A, B, E, F, G, I)


def test_MON05_on_across_the_window_is_split_around_not_monitored(
    location_factory: Any,
) -> None:
    location = location_factory()
    _insert(location, "on", _at(9, 0), None)

    assert _overwrite(location) == 1

    assert _intervals(location) == [
        ("on", _at(9, 0), A, None),
        ("not_monitored", A, B, None),
        ("on", B, None, None),
    ]


def test_INV11_off_across_the_window_stays_one_outage(location_factory: Any) -> None:
    location = location_factory()
    _insert(location, "off", _at(9, 0), None, outage_start_at=_at(9, 0))

    assert _overwrite(location) == 1

    # Both off pieces carry the original outage start: one outage (INV-11 #2, D-02).
    assert _intervals(location) == [
        ("off", _at(9, 0), A, _at(9, 0)),
        ("not_monitored", A, B, None),
        ("off", B, None, _at(9, 0)),
    ]


def test_K1_first_heartbeat_inside_the_window_leaves_no_data_before_it(
    location_factory: Any,
) -> None:
    location = location_factory()
    _insert(location, "on", _at(10, 5), None)

    assert _overwrite(location) == 1

    # 10:00-10:05 had no data, and it stays no data (no not_monitored piece over it).
    assert _intervals(location) == [
        ("not_monitored", _at(10, 5), B, None),
        ("on", B, None, None),
    ]


def test_MON05_restore_inside_the_window_gives_two_not_monitored_pieces(
    location_factory: Any,
) -> None:
    location = location_factory()
    _insert(location, "off", _at(9, 0), _at(10, 5), outage_start_at=_at(9, 0))
    _insert(location, "on", _at(10, 5), None)

    assert _overwrite(location) == 2

    # The accepted shape (ARCHITECTURE Open Edge 2): two adjacent not_monitored pieces.
    assert _intervals(location) == [
        ("off", _at(9, 0), A, _at(9, 0)),
        ("not_monitored", A, _at(10, 5), None),
        ("not_monitored", _at(10, 5), B, None),
        ("on", B, None, None),
    ]


def test_MON05_pieces_that_only_touch_the_window_are_untouched(location_factory: Any) -> None:
    location = location_factory()
    _insert(location, "on", _at(8, 0), A)
    _insert(location, "on", A, B)
    _insert(location, "off", B, _at(10, 20), outage_start_at=B)
    _insert(location, "on", _at(10, 20), None)
    before, middle, after, last = _ids(location)

    assert _overwrite(location) == 1

    # The piece ending at a and the pieces starting at or after b are the same rows.
    assert _intervals(location) == [
        ("on", _at(8, 0), A, None),
        ("not_monitored", A, B, None),
        ("off", B, _at(10, 20), B),
        ("on", _at(10, 20), None, None),
    ]
    ids = _ids(location)
    assert (ids[0], ids[2], ids[3]) == (before, after, last)
    assert middle not in ids


def test_MON05_maintenance_not_monitored_is_unchanged(location_factory: Any) -> None:
    location = location_factory()
    _insert(location, "not_monitored", _at(9, 0), None)
    ids = _ids(location)

    assert _overwrite(location) == 0

    assert _intervals(location) == [("not_monitored", _at(9, 0), None, None)]
    assert _ids(location) == ids


def test_MON05_a_closed_piece_inside_the_window_is_replaced(location_factory: Any) -> None:
    location = location_factory()
    _insert(location, "on", _at(9, 0), _at(10, 2))
    _insert(location, "off", _at(10, 2), _at(10, 6), outage_start_at=_at(10, 2))
    _insert(location, "on", _at(10, 6), None)

    assert _overwrite(location) == 3

    assert _intervals(location) == [
        ("on", _at(9, 0), A, None),
        ("not_monitored", A, _at(10, 2), None),
        ("not_monitored", _at(10, 2), _at(10, 6), None),
        ("not_monitored", _at(10, 6), B, None),
        ("on", B, None, None),
    ]


# Idempotent re-run, and a later end after a crash mid-carve


def test_MON05_rerunning_the_same_window_changes_nothing(location_factory: Any) -> None:
    location = location_factory()
    _insert(location, "on", _at(9, 0), None)
    assert _overwrite(location) == 1
    rows, ids = _intervals(location), _ids(location)

    assert _overwrite(location) == 0

    assert (_intervals(location), _ids(location)) == (rows, ids)


def test_MON05_a_later_end_appends_and_moves_the_open_piece(location_factory: Any) -> None:
    location = location_factory()
    _insert(location, "on", _at(9, 0), None)
    assert _overwrite(location) == 1

    assert _overwrite(location, b=_at(10, 12)) == 1

    assert _intervals(location) == [
        ("on", _at(9, 0), A, None),
        ("not_monitored", A, B, None),
        ("not_monitored", B, _at(10, 12), None),
        ("on", _at(10, 12), None, None),
    ]


# Edges and failures


@pytest.mark.parametrize("b", [A, _at(9, 59)], ids=["empty", "reversed"])
def test_MON05_an_empty_or_reversed_window_writes_nothing(
    location_factory: Any, b: datetime
) -> None:
    location = location_factory()
    _insert(location, "on", _at(9, 0), None)
    rows, ids = _intervals(location), _ids(location)

    assert _overwrite(location, a=A, b=b) == 0

    assert (_intervals(location), _ids(location)) == (rows, ids)


def test_MON05_a_location_without_intervals_gets_nothing(location_factory: Any) -> None:
    location = location_factory()

    assert _overwrite(location) == 0

    assert _intervals(location) == []


def test_overwrite_refuses_the_off_state(location_factory: Any) -> None:
    # An off piece needs an outage start; overwrite writes "not_monitored" (the carve) or
    # "on" (Phase 5's false-outage removal) only, and refuses before any write.
    location = location_factory()
    _insert(location, "on", _at(9, 0), None)
    ids = _ids(location)

    with pytest.raises(ValueError, match="off"):
        _overwrite(location, state="off")

    assert _ids(location) == ids


def test_overwrite_with_on_clears_the_outage_start(location_factory: Any) -> None:
    # Phase 5 (DATA-02) reuses overwrite with state "on": covered off time loses its outage.
    location = location_factory()
    _insert(location, "on", _at(9, 0), A)
    _insert(location, "off", A, None, outage_start_at=A)

    assert _overwrite(location, state="on") == 1

    assert _intervals(location) == [
        ("on", _at(9, 0), A, None),
        ("on", A, B, None),
        ("off", B, None, A),
    ]
