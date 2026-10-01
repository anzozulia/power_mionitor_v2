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
