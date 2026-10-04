"""The ``display_time`` filters: an admin-page time in the display TZ (P-3).

``{% load display_time %}`` then ``{{ value|display_time }}``. It renders
``YYYY-MM-DD HH:MM:SS TZ`` with the zone abbreviation from ``%Z``, so the repeated
autumn hour stays unambiguous (2026-10-25 03:30:00 EEST, then 03:30:00 EET). Django's
``date:"T"`` drops the zone name for exactly those times, so templates never use it.

``{{ value|display_time_compact }}`` is the compact form ``YYYY-MM-DD HH:MM`` for narrow
cells, always shown next to the full one (UI-SPEC). The status JSON carries both strings,
so the browser never formats a time (UI-05).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django import template
from django.conf import settings

register = template.Library()

NEVER = "Never"


@register.filter
def display_time(value: object) -> str:
    """An aware datetime in the display TZ; ``Never`` for None; empty for anything else."""
    if value is None:
        return NEVER
    if not isinstance(value, datetime) or value.utcoffset() is None:
        # A naive datetime has no defined instant; a string or a date is not a time.
        return ""
    local = value.astimezone(ZoneInfo(settings.TIME_ZONE))
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


@register.filter
def display_time_compact(value: object) -> str:
    """``YYYY-MM-DD HH:MM`` in the display TZ; ``Never`` for None; empty for anything else."""
    if value is None:
        return NEVER
    if not isinstance(value, datetime) or value.utcoffset() is None:
        # The same bad-input contract as display_time.
        return ""
    local = value.astimezone(ZoneInfo(settings.TIME_ZONE))
    return local.strftime("%Y-%m-%d %H:%M")
