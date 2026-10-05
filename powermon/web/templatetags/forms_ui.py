"""Template helpers for the location forms (UI-01, UI-12; 06-UI-SPEC S4 Add / S6 Edit).

``{% load forms_ui %}`` gives three tags, used by ``web/_location_fields.html``,
``partials/_error_summary.html`` and the project field group ``django/forms/field.html``:

- ``{% off_after_initial form as off_after %}``: the server-rendered value of the live
  "Reported OFF after" hint (N8). It is period + grace when both values are whole numbers
  from 10 to 3600, the form's own bounds (``MIN_SECONDS``, ``MAX_SECONDS``), read from the
  posted data on a bound form and from the initial values otherwise; else None, and the
  hint shows its fallback sentence. The rule is the one admin.js's ``offAfterHint`` applies
  while the admin types: ASCII digits only, surrounding whitespace ignored.
- ``{% summary_links form as links %}``: the jump links of the error summary (N10), one
  ``(control id, label, first error)`` per invalid field, in field order.
- ``{% field_control field kind %}``: the field's control exactly as ``{{ field }}``
  renders it, plus the extra attributes of ``kind`` (``CONTROL_ATTRS``). Django's own
  wiring (``required``, ``aria-invalid``, ``aria-describedby``, the id) is unchanged.

Nothing here builds markup or a class name: the control comes from the widget template, and
every class is written in the templates, where the Tailwind scan sees it.
"""

import re

from django import template
from django.forms import BaseForm, BoundField

from powermon.locations.models import MAX_SECONDS, MIN_SECONDS

register = template.Library()

# ASCII digits only: str.isdigit() would also accept other scripts' digits and superscripts.
_WHOLE = re.compile(r"[0-9]+")
# More digits than this (leading zeros aside) is beyond MAX_SECONDS; checked before int()
# so a huge paste never reaches Python's int-string length limit.
_MAX_DIGITS = len(str(MAX_SECONDS))

# The extra control attributes per kind of field (06-UI-SPEC Components > Form field).
# True renders the bare attribute name (Django's attrs.html).
CONTROL_ATTRS: dict[str, dict[str, str | bool]] = {
    # The seconds fields: a numeric keypad on phones.
    "seconds": {"inputmode": "numeric"},
    # The bot token: password managers neither offer to save it nor fill it in.
    "secret": {"data-1p-ignore": True, "data-lpignore": "true", "data-bwignore": True},
}


def whole_seconds(value: object) -> int | None:
    """``value`` as whole seconds within the form's bounds, or None.

    An int (an initial value) or a string of ASCII digits with optional surrounding
    whitespace (a posted value). Anything else, or a number outside 10-3600, is None.
    """
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str) and _WHOLE.fullmatch(value.strip()):
        digits = value.strip().lstrip("0") or "0"
        number = int(digits) if len(digits) <= _MAX_DIGITS else None
    else:
        number = None
    if number is None or not MIN_SECONDS <= number <= MAX_SECONDS:
        return None
    return number


@register.simple_tag
def off_after_initial(form: BaseForm) -> int | None:
    """Period + grace for the OFF-after hint, or None when either is not valid seconds."""
    period = whole_seconds(form["period_s"].value())
    grace = whole_seconds(form["grace_s"].value())
    if period is None or grace is None:
        return None
    return period + grace


@register.simple_tag
def summary_links(form: BaseForm) -> list[tuple[str, str, str]]:
    """(control id, label, first error) of each invalid field, in field order."""
    links: list[tuple[str, str, str]] = []
    for field in form:
        if field.errors:
            links.append((str(field.auto_id), str(field.label), str(field.errors[0])))
    return links


@register.simple_tag
def field_control(field: BoundField, kind: str) -> str:
    """The field's control as ``{{ field }}`` renders it, plus the attributes of ``kind``.

    An unknown kind raises, so a typo in a template fails its render instead of quietly
    dropping the attributes.
    """
    attrs = CONTROL_ATTRS.get(kind)
    if attrs is None:
        raise template.TemplateSyntaxError(f"field_control: unknown kind {kind!r}")
    return field.as_widget(attrs=dict(attrs))
