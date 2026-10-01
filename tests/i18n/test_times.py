"""Local times in the display TZ for alert and ops texts (D-06, D-11, CHRT-06).

Inputs are aware UTC instants, as stored. The expected strings are computed by hand for
Europe/Kyiv: UTC+3 (EEST) until 2026-10-25 01:00 UTC, UTC+2 (EET) until 2027-03-28 01:00
UTC, then UTC+3 again. The module is pure, so these tests need no database or settings.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from powermon.i18n import times

KYIV = "Europe/Kyiv"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_hm_and_dm_in_kyiv() -> None:
    at = _utc("2026-10-01T10:05:00")

    assert times.hm(at, KYIV) == "13:05"
    assert times.dm(at, KYIV) == "01.10"


def test_local_converts_to_the_display_tz() -> None:
    at = _utc("2026-10-01T10:05:00")
    local = times.local(at, KYIV)

    assert (local.hour, local.minute, local.utcoffset()) == (13, 5, timedelta(hours=3))
    # The same instant, only shown in another zone.
    assert local == at


def test_event_prefix_is_the_time_on_the_same_local_date() -> None:
    event = _utc("2026-10-01T10:05:00")

    assert times.event_prefix(event, _utc("2026-10-01T10:06:31"), KYIV) == "13:05"
    # 20:59 UTC is still 23:59 on 01.10 in Kyiv.
    assert times.event_prefix(event, _utc("2026-10-01T20:59:59"), KYIV) == "13:05"


def test_event_prefix_adds_the_date_when_the_delivery_is_on_another_local_date() -> None:
    event = _utc("2026-10-01T10:05:00")

    # 21:00 UTC is 00:00 on 02.10 in Kyiv, while the UTC date is still 01.10.
    assert times.event_prefix(event, _utc("2026-10-01T21:00:00"), KYIV) == "01.10 13:05"


@pytest.mark.parametrize("fn", [times.hm, times.dm, times.local], ids=lambda fn: fn.__name__)
def test_a_naive_datetime_raises(fn: Callable[[datetime, str], object]) -> None:
    with pytest.raises(ValueError, match="naive"):
        fn(datetime(2026, 10, 1, 10, 5), KYIV)  # noqa: DTZ001


def test_event_prefix_refuses_a_naive_event_or_now() -> None:
    aware = _utc("2026-10-01T10:05:00")
    naive = datetime(2026, 10, 1, 10, 5)  # noqa: DTZ001

    with pytest.raises(ValueError, match="naive"):
        times.event_prefix(naive, aware, KYIV)
    with pytest.raises(ValueError, match="naive"):
        times.event_prefix(aware, naive, KYIV)


def test_a_value_that_is_not_a_datetime_raises() -> None:
    with pytest.raises(TypeError):
        times.hm("2026-10-01T10:05:00Z", KYIV)  # type: ignore[arg-type]


# Seconds, spans and "when" (D-11 notice times)


def test_hms_in_kyiv() -> None:
    assert times.hms(_utc("2026-10-01T07:02:05"), KYIV) == "10:02:05"


def test_span_on_one_local_date() -> None:
    # The en dash U+2013 with a space on each side, as in the D-11 gap sample.
    start, end = _utc("2026-10-01T07:00:12"), _utc("2026-10-01T07:10:40")

    assert times.span(start, end, KYIV) == "01.10 10:00:12 – 10:10:40"


def test_span_across_local_midnight_dates_both_ends() -> None:
    # 20:58 UTC is 23:58 on 30.09 in Kyiv; 21:05 UTC is already 00:05 on 01.10.
    start, end = _utc("2026-09-30T20:58:00"), _utc("2026-09-30T21:05:00")

    assert times.span(start, end, KYIV) == "30.09 23:58:00 – 01.10 00:05:00"


def test_span_over_the_repeated_autumn_hour() -> None:
    # 2026-10-25: 00:30 UTC is 03:30 EEST and 01:30 UTC is 03:30 EET, one hour apart.
    start, end = _utc("2026-10-25T00:30:00"), _utc("2026-10-25T01:30:00")

    assert times.span(start, end, KYIV) == "25.10 03:30:00 – 03:30:00"


def test_when_s_is_the_time_on_the_same_local_date() -> None:
    now = _utc("2026-10-01T07:07:10")

    assert times.when_s(_utc("2026-10-01T07:02:05"), now, KYIV) == "10:02:05"


def test_when_s_adds_the_date_when_the_local_dates_differ() -> None:
    now = _utc("2026-10-01T07:00:00")

    assert times.when_s(_utc("2026-09-30T20:58:00"), now, KYIV) == "30.09 23:58:00"


@pytest.mark.parametrize("fn", [times.hms, times.dm], ids=lambda fn: fn.__name__)
def test_hms_and_dm_refuse_a_naive_datetime(fn: Callable[[datetime, str], str]) -> None:
    with pytest.raises(ValueError, match="naive"):
        fn(datetime(2026, 10, 1, 7, 2, 5), KYIV)  # noqa: DTZ001


def test_span_and_when_s_refuse_a_naive_datetime() -> None:
    aware = _utc("2026-10-01T07:00:00")
    naive = datetime(2026, 10, 1, 7, 0)  # noqa: DTZ001

    for call in (
        lambda: times.span(naive, aware, KYIV),
        lambda: times.span(aware, naive, KYIV),
        lambda: times.when_s(naive, aware, KYIV),
        lambda: times.when_s(aware, naive, KYIV),
    ):
        with pytest.raises(ValueError, match="naive"):
            call()


# DST days (CHRT-06, D-06): vectors computed in the container (RESEARCH, spike 9)


def test_dst_event_before_midnight_delivered_on_the_fallback_day() -> None:
    # 24.10 23:58 EEST, delivered at 25.10 03:00 local.
    event, now = _utc("2026-10-24T20:58:00"), _utc("2026-10-25T00:00:00")

    assert times.event_prefix(event, now, KYIV) == "24.10 23:58"


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-10-25T00:30:00", "03:30"),  # first 03:30, EEST (fold 0)
        ("2026-10-25T01:30:00", "03:30"),  # second 03:30, EET (fold 1): the same text
        ("2026-10-25T01:00:00", "03:00"),  # right after the fall-back
        ("2027-03-28T00:59:00", "02:59"),  # last minute before the spring-forward gap
        ("2027-03-28T01:00:00", "04:00"),  # 03:00-04:00 local does not exist
    ],
)
def test_dst_hm(instant: str, expected: str) -> None:
    assert times.hm(_utc(instant), KYIV) == expected


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-10-25T21:59:00", "25.10"),  # 23:59 EET
        ("2026-10-25T22:00:00", "26.10"),  # midnight is at 22:00 UTC after the fall-back
        ("2027-03-28T20:59:00", "28.03"),  # 23:59 EEST
        ("2027-03-28T21:00:00", "29.03"),  # midnight is at 21:00 UTC after spring-forward
    ],
)
def test_dst_local_date_changes(instant: str, expected: str) -> None:
    assert times.dm(_utc(instant), KYIV) == expected
