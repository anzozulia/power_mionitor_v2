"""The one duration formatter (docs/chart-spec.md section 8, D-16).

Alerts use ``format_alert_duration``; the Phase 3 row totals and caption use
``format_total_duration``. Both take integer microseconds and do integer arithmetic only:
floats drift, and Python's built-in rounding is half-even, while section 8 asks for half
up. A test fails if any file in this package names that built-in or divides with "/".

Pure: imports nothing from Django.
"""

from powermon.i18n.strings import SEP, UNITS, resolve_language

US = 1_000_000  # one second, in microseconds
MIN_US = 60 * US

# Indexes into UNITS[lang].
_DAY, _HOUR, _MINUTE, _SECOND = range(4)
_MINUTES_PER_DAY = 24 * 60


def _check(us: int) -> None:
    if not isinstance(us, int):
        raise TypeError(f"a duration must be integer microseconds, not {type(us).__name__}")
    if us < 0:
        raise ValueError(f"negative duration: {us} us")


def _half_up(value: int, unit: int) -> int:
    """``value / unit`` rounded half up, for ``value >= 0``."""
    return (value + unit // 2) // unit


def _join(parts: list[tuple[int, int]], lang: str) -> str:
    """Join the non-zero ``(number, unit index)`` parts: ``"5 год 12 хв"``, ``"5h 12m"``."""
    sep, units = SEP[lang], UNITS[lang]
    return " ".join(f"{number}{sep}{units[index]}" for number, index in parts if number)


def format_alert_duration(us: int, lang: str) -> str:
    """Alert duration: ``45s``, ``12m 5s``, ``5h 12m``, ``1d 5h`` (and uk/ru forms).

    Seconds below 1 min, minutes and seconds below 1 h, hours and minutes below 24 h,
    days, hours and minutes from 24 h. The value is rounded half up to the smallest unit
    shown; when that reaches the next band, the next band is used (59 min 59.6 s is
    ``1h``). Zero parts are left out. A duration that rounds to 0 s is ``0s``.
    """
    _check(us)
    lang = resolve_language(lang)
    seconds = _half_up(us, US)
    if seconds < 60:
        return f"{seconds}{SEP[lang]}{UNITS[lang][_SECOND]}"
    if seconds < 3600:
        minutes, seconds = divmod(seconds, 60)
        return _join([(minutes, _MINUTE), (seconds, _SECOND)], lang)
    # Hours band and up: re-round from the raw value in the band's smallest unit.
    total_minutes = _half_up(us, MIN_US)
    if total_minutes < _MINUTES_PER_DAY:
        hours, minutes = divmod(total_minutes, 60)
        return _join([(hours, _HOUR), (minutes, _MINUTE)], lang)
    days, rest = divmod(total_minutes, _MINUTES_PER_DAY)
    hours, minutes = divmod(rest, 60)
    return _join([(days, _DAY), (hours, _HOUR), (minutes, _MINUTE)], lang)


def format_total_duration(us: int, lang: str) -> str:
    """Row total or caption duration: ``3h 20m``, ``4h``, ``45m``, ``<1m``; never days.

    Minutes are rounded half up. OFF time that rounds to 0 minutes is ``<1m``; callers
    show the "no outages" text instead when there was no OFF time at all.
    """
    _check(us)
    lang = resolve_language(lang)
    total_minutes = _half_up(us, MIN_US)
    if total_minutes == 0:
        return f"<1{SEP[lang]}{UNITS[lang][_MINUTE]}"
    hours, minutes = divmod(total_minutes, 60)
    return _join([(hours, _HOUR), (minutes, _MINUTE)], lang)
