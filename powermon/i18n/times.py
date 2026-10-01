"""Local times in the display TZ for alert and ops texts (D-06, D-11, CHRT-06).

Every stored time is an aware UTC instant. This module converts one to the display time
zone with ``zoneinfo`` and formats it with ``strftime`` only. DST needs nothing special:
zoneinfo knows the offsets, so on 2026-10-25 both Kyiv 03:30s read "03:30" and on
2027-03-28 02:59 EET is followed by 04:00 EEST. A naive datetime has no defined instant
and raises ValueError, so a missing tzinfo can never come out as a wrong local time.

Formats: ``HH:MM`` (``hm``), ``HH:MM:SS`` (``hms``) and ``DD.MM`` (``dm``, the chart's
row-date format). A time on another local date than ``now`` gets the date in front
(``event_prefix``, ``when_s``), as a late alert does (D-06); a span names the end's date
too when it is not the start's (``span``, the D-11 gap notice).

Pure: imports nothing from Django. Like ``duration.py`` it never divides with "/" and never
calls the built-in rounding (a test scans every file in this package).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

EN_DASH = "–"


def local(instant: datetime, tz: str) -> datetime:
    """``instant`` in the IANA zone ``tz``; ValueError for a naive datetime."""
    if not isinstance(instant, datetime):
        raise TypeError(f"a time must be a datetime, not {type(instant).__name__}")
    if instant.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return instant.astimezone(ZoneInfo(tz))


def hm(instant: datetime, tz: str) -> str:
    """Local ``HH:MM``, e.g. ``13:05``."""
    return local(instant, tz).strftime("%H:%M")


def hms(instant: datetime, tz: str) -> str:
    """Local ``HH:MM:SS``, e.g. ``10:02:05``."""
    return local(instant, tz).strftime("%H:%M:%S")


def dm(instant: datetime, tz: str) -> str:
    """Local ``DD.MM``, e.g. ``01.10``."""
    return local(instant, tz).strftime("%d.%m")


def event_prefix(event_at: datetime, now: datetime, tz: str) -> str:
    """``HH:MM`` of the event, or ``DD.MM HH:MM`` when its local date is not now's (D-06)."""
    event = local(event_at, tz)
    if event.date() == local(now, tz).date():
        return event.strftime("%H:%M")
    return event.strftime("%d.%m %H:%M")


def span(start: datetime, end: datetime, tz: str) -> str:
    """``DD.MM HH:MM:SS – HH:MM:SS``, or ``… – DD.MM HH:MM:SS`` across local midnight."""
    first = local(start, tz)
    last = local(end, tz)
    end_format = "%H:%M:%S" if first.date() == last.date() else "%d.%m %H:%M:%S"
    return f"{first.strftime('%d.%m %H:%M:%S')} {EN_DASH} {last.strftime(end_format)}"


def when_s(at: datetime, now: datetime, tz: str) -> str:
    """``HH:MM:SS``, or ``DD.MM HH:MM:SS`` when the local date of ``at`` is not now's."""
    moment = local(at, tz)
    if moment.date() == local(now, tz).date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%d.%m %H:%M:%S")
