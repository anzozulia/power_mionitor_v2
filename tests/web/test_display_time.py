"""The ``display_time`` template filter: admin times in the display TZ (P-3, CHRT-06 rules).

Django's ``date:"... T"`` drops the zone name for an ambiguous local time, so both
instants of the repeated autumn hour would render the same. The filter formats with
``%Z`` after converting to the display TZ, so they stay distinct. Every input is an aware
datetime built with ``tzinfo=UTC``; no clock is patched.
"""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.template import Context, Template

KYIV = "Europe/Kyiv"


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """The default display TZ, set explicitly so the test does not depend on the env file."""
    settings.TIME_ZONE = KYIV
    return settings


def test_display_time_dst_fallback_hour(kyiv: Any) -> None:
    from powermon.web.templatetags.display_time import display_time

    # 2026-10-25: Kyiv repeats 03:00-04:00 local; the zone name tells the two apart.
    assert display_time(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == "2026-10-25 03:30:00 EEST"
    assert display_time(datetime(2026, 10, 25, 1, 30, tzinfo=UTC)) == "2026-10-25 03:30:00 EET"


def test_display_time_dst_spring_gap(kyiv: Any) -> None:
    from powermon.web.templatetags.display_time import display_time

    # 2027-03-28: Kyiv skips 03:00-04:00 local.
    assert display_time(datetime(2027, 3, 28, 0, 59, 59, tzinfo=UTC)) == "2027-03-28 02:59:59 EET"
    assert display_time(datetime(2027, 3, 28, 1, 0, tzinfo=UTC)) == "2027-03-28 04:00:00 EEST"


def test_display_time_converts_any_aware_time_to_the_display_tz(settings: Any) -> None:
    from powermon.web.templatetags.display_time import display_time

    new_york = datetime(2026, 10, 1, 4, 0, tzinfo=ZoneInfo("America/New_York"))
    settings.TIME_ZONE = KYIV
    assert display_time(new_york) == "2026-10-01 11:00:00 EEST"

    # The display TZ is instance-wide and configurable.
    settings.TIME_ZONE = "UTC"
    assert display_time(new_york) == "2026-10-01 08:00:00 UTC"


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-25 00:30",
        # The one deliberate naive datetime: it is the bad input under test.
        datetime(2026, 10, 25, 0, 30),  # noqa: DTZ001
        date(2026, 10, 25),
        0,
    ],
    ids=["string", "naive-datetime", "date", "number"],
)
def test_display_time_never_and_bad_input(kyiv: Any, value: object) -> None:
    from powermon.web.templatetags.display_time import display_time

    # No heartbeat yet reads "Never" (UI-SPEC); anything that is not an aware time is blank.
    assert display_time(None) == "Never"
    assert display_time(value) == ""


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 10, 25, 1, 30, tzinfo=UTC), None],
    ids=["aware", "none"],
)
def test_display_time_is_a_template_filter(kyiv: Any, value: datetime | None) -> None:
    from powermon.web.templatetags.display_time import display_time

    rendered = Template("{% load display_time %}{{ t|display_time }}").render(Context({"t": value}))

    assert rendered == display_time(value)
