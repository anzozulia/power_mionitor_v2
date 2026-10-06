"""The chart update period's slots: ``lifecycle.refresh_slot`` (pure, no database).

CHRT-02, amended by quick task 261006-of9: each location picks a chart update period of 1,
5, 10, 15, 30 or 60 minutes (default 15), and today's chart is refreshed once per slot.
Slots start on the local clock minutes of the display zone that are multiples of the
period (10 min: :00, :10 ... :50; 60 min: on the hour; 1 min: every minute).

CHRT-06 / INV-08: on Kyiv's fall-back day the repeated 03:xx hour gets its slots twice,
and on the spring-forward day there is no gap. INV-13: ``refresh_slot`` never raises for
a valid period in any zone, a 30-minute DST shift included, so one location's period can
never stop every chart. DST instants are built as explicit UTC datetimes: ``kyiv()`` uses
fold 0 and cannot name the second 03:xx.
"""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from chart_fixtures import KYIV, kyiv

from powermon.chart import lifecycle

PERIODS = (1, 5, 10, 15, 30, 60)
# Kyiv's 2026 fall-back: 04:00 EEST (01:00 UTC) becomes 03:00 EET.
FALL_BACK_DAY = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
# Kyiv's 2027 spring-forward: 03:00 EET (01:00 UTC) becomes 04:00 EEST.
SPRING_FORWARD_DAY = datetime(2027, 3, 27, 20, 0, tzinfo=UTC)
# Lord Howe Island moves from +10:30 to +11:00 at 2026-10-04 02:00 local (2026-10-03 15:30 UTC).
LORD_HOWE = "Australia/Lord_Howe"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _slots(every_min: int, start: datetime, end: datetime, tz: str = KYIV) -> list[datetime]:
    """The distinct slots of every whole minute from ``start`` to ``end`` (both included)."""
    seen: list[datetime] = []
    now = start
    while now <= end:
        slot = lifecycle.refresh_slot(now, every_min, tz)
        if not seen or seen[-1] != slot:
            seen.append(slot)
        now += timedelta(minutes=1)
    return seen


def _wall(instant: datetime, tz: str = KYIV) -> str:
    return instant.astimezone(ZoneInfo(tz)).strftime("%H:%M %Z")


# Expected


@pytest.mark.parametrize(
    ("every_min", "now", "slot"),
    [
        (10, "2026-10-01 12:03:27", "2026-10-01 12:00"),
        (15, "2026-10-01 12:19:59", "2026-10-01 12:15"),
        (1, "2026-10-01 12:05:00.500000", "2026-10-01 12:05"),
        (60, "2026-10-01 12:59:59", "2026-10-01 12:00"),
        (10, "2026-10-01 12:10:00", "2026-10-01 12:10"),
    ],
)
def test_CHRT02_slot_is_the_last_clock_multiple_of_the_period(
    every_min: int, now: str, slot: str
) -> None:
    got = lifecycle.refresh_slot(kyiv(now), every_min, KYIV)

    assert got == kyiv(slot)
    assert got.utcoffset() == timedelta(0)
    assert (got.second, got.microsecond) == (0, 0)


# Edge: DST (CHRT-06, INV-08) and a zone whose offset is not a whole hour


@pytest.mark.parametrize("every_min", [15, 60])
def test_CHRT06_fall_back_repeated_hour_refreshes_both_times(every_min: int) -> None:
    slots = _slots(every_min, _utc("2026-10-24 23:00"), _utc("2026-10-25 02:00"))

    # No gap and no repeat: consecutive slots are exactly one period apart in real time.
    assert all(b - a == timedelta(minutes=every_min) for a, b in pairwise(slots))
    if every_min == 60:
        # 03:00 local comes twice (EEST, then EET), and each gets its slot.
        threes = [slot for slot in slots if _wall(slot).startswith("03:00")]
        assert threes == [_utc("2026-10-25 00:00"), _utc("2026-10-25 01:00")]
    else:
        i = slots.index(_utc("2026-10-25 00:45"))
        assert slots[i : i + 3] == [
            _utc("2026-10-25 00:45"),
            _utc("2026-10-25 01:00"),
            _utc("2026-10-25 01:15"),
        ]
        assert [_wall(slot) for slot in slots[i : i + 3]] == [
            "03:45 EEST",
            "03:00 EET",
            "03:15 EET",
        ]


def test_CHRT06_spring_forward_no_gap() -> None:
    after = lifecycle.refresh_slot(_utc("2027-03-28 01:05"), 15, KYIV)
    before = lifecycle.refresh_slot(_utc("2027-03-28 00:59"), 15, KYIV)

    assert (after, _wall(after)) == (_utc("2027-03-28 01:00"), "04:00 EEST")
    assert (before, _wall(before)) == (_utc("2027-03-28 00:45"), "02:45 EET")
    # 02:45 EET and 04:00 EEST are 15 real minutes apart: the skipped hour costs no slot.
    assert after - before == timedelta(minutes=15)


def test_boundary_is_local_not_utc() -> None:
    # Asia/Kolkata is UTC+5:30: on the hour local is :30 UTC.
    kolkata = "Asia/Kolkata"
    now = _utc("2026-10-01 07:10")
    assert _wall(now, kolkata) == "12:40 IST"

    slot = lifecycle.refresh_slot(now, 60, kolkata)

    assert slot == _utc("2026-10-01 06:30")
    assert _wall(slot, kolkata) == "12:00 IST"


@pytest.mark.parametrize("day", [FALL_BACK_DAY, SPRING_FORWARD_DAY], ids=["fall", "spring"])
@pytest.mark.parametrize("every_min", PERIODS)
def test_CHRT06_every_period_stays_aligned_across_a_dst_day(every_min: int, day: datetime) -> None:
    period = timedelta(minutes=every_min)
    now, end = day + timedelta(microseconds=1), day + timedelta(hours=28)
    while now <= end:
        slot = lifecycle.refresh_slot(now, every_min, KYIV)
        local = slot.astimezone(ZoneInfo(KYIV))
        assert slot <= now
        assert now - slot < period
        assert (local.minute % every_min, local.second, local.microsecond) == (0, 0, 0)
        now += timedelta(seconds=37)


@pytest.mark.parametrize("every_min", PERIODS)
def test_INV13_refresh_slot_never_raises_on_a_30_minute_dst_shift(every_min: int) -> None:
    # One slot at the shift may be off its clock minute; nothing raises and no slot is
    # in the future or more than one period old.
    period = timedelta(minutes=every_min)
    now = _utc("2026-10-03 12:00")
    assert _wall(_utc("2026-10-03 15:29"), LORD_HOWE).startswith("01:59")
    assert _wall(_utc("2026-10-03 15:30"), LORD_HOWE).startswith("02:30")
    while now <= _utc("2026-10-03 20:00"):
        slot = lifecycle.refresh_slot(now, every_min, LORD_HOWE)
        assert slot <= now
        assert now - slot < period
        now += timedelta(minutes=1)


# Failure


@pytest.mark.parametrize("every_min", [0, 7, 90, -5])
def test_refresh_slot_refuses_a_period_that_does_not_divide_60(every_min: int) -> None:
    with pytest.raises(ValueError, match="divide 60"):
        lifecycle.refresh_slot(kyiv("2026-10-01 12:03"), every_min, KYIV)


def test_refresh_slot_refuses_a_naive_now() -> None:
    with pytest.raises(ValueError, match="aware"):
        lifecycle.refresh_slot(datetime(2026, 10, 1, 12, 3), 15, KYIV)  # noqa: DTZ001
