"""Relative times on the admin pages (UI-11; 06-UI-SPEC Components › Relative time).

``{% load timefmt %}``. A relative time sits next to the absolute one from
``display_time`` and never replaces it. The floors, English only:

- under 1 s, or an instant in the future: "just now";
- under 60 s: "{N} s ago"; under 60 min: "{N} min ago"; under 48 h: "{N} h ago";
- otherwise "{N} d ago".

N is cut off, never rounded. The age is the difference of two UTC instants, so a DST
change never shifts it. The sidebar cell uses the same floors with one-letter units and no
space ("59s", "1m", "47h", "2d"), and its screen-reader sentence the spelled units ("59 s",
"1 min", "47 h", "2 d").

"now" comes from the view's injected clock, never from ``datetime.now()``: a view puts its
clock's reading into the template context as ``now``, and the tags read it from there.
Without one they fall back to ``CLOCK``, a module attribute that tests monkeypatch. The
``iso`` filter gives the ``<time datetime>`` value: ISO 8601 with the display-TZ offset.

The pure functions raise ValueError for a naive datetime, which has no defined instant;
the template filter and tags render "" for None, a naive datetime or anything that is not
a datetime, and never raise.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django import template
from django.conf import settings
from django.template import Context

from powermon.clock import Clock, SystemClock

register = template.Library()

# The tags' fallback "now" when the view put none into the context; tests monkeypatch it.
CLOCK: Clock = SystemClock()

JUST_NOW = "just now"
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# Up to 47 h the age reads in hours, from 48 h in days.
HOURS_LIMIT = timedelta(hours=48)
# The sidebar cell's one-letter units: minutes are "m" there.
COMPACT_UNITS = {"s": "s", "min": "m", "h": "h", "d": "d"}


def age_parts(value: datetime, now: datetime) -> tuple[int, str]:
    """The age of ``value`` at ``now`` as ``(N, unit)``, unit "s", "min", "h" or "d".

    Floored to the unit of its range; under 1 s or in the future it is ``(0, "s")``. A
    naive ``value`` or ``now`` has no defined instant: ValueError.
    """
    if value.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    # In UTC: two datetimes that share one tzinfo would otherwise subtract wall times.
    age = now.astimezone(UTC) - value.astimezone(UTC)
    if age < SECOND:
        return 0, "s"
    if age < MINUTE:
        return age // SECOND, "s"
    if age < HOUR:
        return age // MINUTE, "min"
    if age < HOURS_LIMIT:
        return age // HOUR, "h"
    return age // DAY, "d"


def relative_text(value: datetime, now: datetime) -> str:
    """The relative text: "just now", "{N} s ago", "{N} min ago", "{N} h ago" or "{N} d ago"."""
    count, unit = age_parts(value, now)
    if count == 0 and unit == "s":
        return JUST_NOW
    return f"{count} {unit} ago"


def compact_age(value: datetime, now: datetime) -> str:
    """The sidebar cell: "{N}s" / "{N}m" / "{N}h" / "{N}d" (06-UI-SPEC shell.sb_cell)."""
    count, unit = age_parts(value, now)
    return f"{count}{COMPACT_UNITS[unit]}"


def age_words(value: datetime, now: datetime) -> str:
    """The screen-reader age: "{N} s" / "{N} min" / "{N} h" / "{N} d" (shell.sb_sr)."""
    count, unit = age_parts(value, now)
    return f"{count} {unit}"


def _context_now(context: Context) -> datetime:
    """The view's ``now`` from the context when it is an aware datetime, else ``CLOCK``'s."""
    now = context.get("now")
    if isinstance(now, datetime) and now.utcoffset() is not None:
        return now
    return CLOCK.now()


@register.filter
def iso(value: object) -> str:
    """An aware datetime as ISO 8601 with its display-TZ offset; "" for anything else.

    The ``<time datetime>`` value next to an absolute time, e.g.
    ``2026-10-25T03:30:00+02:00`` in Europe/Kyiv.
    """
    if not isinstance(value, datetime) or value.utcoffset() is None:
        # None, a naive datetime, a date or a string is not an instant.
        return ""
    return value.astimezone(ZoneInfo(settings.TIME_ZONE)).isoformat(timespec="seconds")


@register.simple_tag(takes_context=True)
def relative_time(context: Context, value: object) -> str:
    """``{% relative_time value %}``: ``relative_text`` at the context's now, or ""."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return ""
    return relative_text(value, _context_now(context))


@register.simple_tag(takes_context=True)
def compact_age_tag(context: Context, value: object) -> str:
    """``{% compact_age_tag value %}``: ``compact_age`` at the context's now, or ""."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return ""
    return compact_age(value, _context_now(context))


@register.simple_tag(takes_context=True)
def age_words_tag(context: Context, value: object) -> str:
    """``{% age_words_tag value %}``: ``age_words`` at the context's now, or ""."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return ""
    return age_words(value, _context_now(context))
