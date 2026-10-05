"""The ``{% icon %}`` tag: a vendored Lucide SVG inlined into the page (UI-12, R1).

``{% load icons %}`` then ``{% icon "zap" class="size-5 text-brand" %}``. ``class`` is the only
keyword and it is required, so every utility class an icon gets is written in a template, where
Tailwind's ``@source`` scan sees it, and never in Python.

The names are the stems of ``templates/icons/*.svg`` (the 44 files of the vendor manifest) that
match ``ICON_NAME``, read once at import into ``ICONS``. A name is only ever looked up in that
dict and never joined into a path, so no template value can reach the filesystem. An unknown
name, an unknown keyword or a missing class raises ``TemplateSyntaxError``.

The committed files stay byte-identical to lucide-static (the manifest test pins their sha256),
so the tag rewrites them at render: the licence comment and the file's own root element are
dropped, and the tag writes its own ``<svg>`` with stroke-width 1.75 (06-UI-SPEC),
``aria-hidden="true" focusable="false"`` and the caller's class, escaped by ``format_html``.
The inner markup (the icon's shapes) is the one audited mark_safe in powermon/web: repository
bytes chosen by an allowlisted name, never request or user data.
"""

import re
from pathlib import Path

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

ICON_DIR = Path(__file__).resolve().parent.parent / "templates" / "icons"
ICON_NAME = re.compile(r"^[a-z0-9-]+$")
# The root element of a Lucide file and everything inside it. The licence comment sits before
# the root, so it is never part of the inner markup.
_ROOT = re.compile(r"<svg\b[^>]*>(?P<inner>.*)</svg>", re.DOTALL)


def _inner_markup(path: Path) -> str:
    """The children of the file's root ``<svg>``, without the root and the licence comment."""
    match = _ROOT.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{path.name} has no <svg> root element")
    return match.group("inner").strip()


ICONS: dict[str, str] = {
    path.stem: _inner_markup(path)
    for path in sorted(ICON_DIR.glob("*.svg"))
    if ICON_NAME.fullmatch(path.stem)
}


@register.simple_tag
def icon(name: object, **kwargs: object) -> str:
    """The named icon as an inline ``<svg>`` hidden from assistive technology."""
    if not isinstance(name, str) or name not in ICONS:
        raise template.TemplateSyntaxError(f"icon: unknown icon name {name!r}")
    unknown = sorted(set(kwargs) - {"class"})
    if unknown:
        raise template.TemplateSyntaxError(
            f"icon: unknown keyword {unknown[0]!r}; class is the only keyword"
        )
    css_class = kwargs.get("class")
    if not isinstance(css_class, str) or not css_class.strip():
        raise template.TemplateSyntaxError(
            'icon: class is required, as in {% icon "zap" class="size-4" %}'
        )
    # A repository file read at import and chosen by an allowlisted name (R1).
    shapes = mark_safe(ICONS[name])  # noqa: S308
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        'stroke-linejoin="round" class="{}" aria-hidden="true" focusable="false">{}</svg>',
        css_class,
        shapes,
    )
