"""The ``{% icon %}`` tag over the vendored Lucide SVGs, and the first-party favicon.

UI-12 / R1: ``{% icon "name" %}`` with its required class keyword inlines a repository SVG as a
hidden, unfocusable ``<svg>`` with stroke-width 1.75 and the caller's class escaped. Names come
from a set built at import from ``templates/icons/*.svg`` and are never joined into a path; an
unknown name, an unknown keyword or a missing class raises ``TemplateSyntaxError``.
``icons.py`` holds the one audited mark_safe call of powermon/web.

UI-13: ``powermon/web/assets/favicon.py`` regenerates ``static/web/favicon.ico`` byte for byte
(Pillow is pinned), ``favicon.svg`` is first-party art, and both have hashed static names.

The two modules are imported inside the tests, so a missing module fails these tests rather
than their collection. The tag's keyword lives in ``CLASS``, and attributes are read with the
stdlib ``html.parser``.
"""

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.template import Context, Template, TemplateSyntaxError
from PIL import Image

WEB = Path(settings.BASE_DIR) / "powermon" / "web"
ICON_DIR = WEB / "templates" / "icons"
ICONS_MODULE = WEB / "templatetags" / "icons.py"
FAVICON_SCRIPT = WEB / "assets" / "favicon.py"
FAVICON_ICO = WEB / "static" / "web" / "favicon.ico"
FAVICON_SVG = WEB / "static" / "web" / "favicon.svg"
ICON_STEMS = sorted(path.stem for path in ICON_DIR.glob("*.svg"))
# The tag's only keyword.
CLASS = "class"
# The colours of 06-UI-SPEC: the sidebar background and the brand cyan.
BACKGROUND = (0x0B, 0x12, 0x20)
BRAND = (0x22, 0xD3, 0xEE)


class _Markup(HTMLParser):
    """Every start tag with its attributes, and every comment, in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.comments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)


def _parse(markup: str) -> _Markup:
    parser = _Markup()
    parser.feed(markup)
    parser.close()
    return parser


def _render(arguments: str, **context: Any) -> str:
    return Template("{% load icons %}{% icon " + arguments + " %}").render(Context(context))


def _inner(markup: str) -> str:
    """What sits between the root ``<svg ...>`` start tag and the last ``</svg>``."""
    start = markup.index(">", markup.index("<svg")) + 1
    return markup[start : markup.rindex("</svg>")].strip()


def _mark_safe_calls(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines() if "mark_safe(" in line]


def test_icon_tag_renders_inline_svg() -> None:
    html = _render(f'"zap" {CLASS}="size-5 text-brand"')
    markup = _parse(html)

    root, attrs = markup.tags[0]
    assert root == "svg"
    # The file's own class and stroke-width are replaced, never repeated.
    assert len({name for name, _ in attrs}) == len(attrs)
    values = dict(attrs)
    assert values["aria-hidden"] == "true"
    assert values["focusable"] == "false"
    assert values["stroke-width"] == "1.75"
    assert values[CLASS] == "size-5 text-brand"
    # The inner markup is the vendored file's, minus the licence comment and the root.
    assert _inner(html) == _inner((ICON_DIR / "zap.svg").read_text(encoding="utf-8"))
    assert markup.comments == []
    assert "lucide" not in html

    # The class value is escaped: markup in it never becomes markup on the page.
    hostile = _render(f'"zap" {CLASS}=css', css='size-5 "><b>')
    assert dict(_parse(hostile).tags[0][1])[CLASS] == 'size-5 "><b>'
    assert "size-5 &quot;&gt;&lt;b&gt;" in hostile
    assert "<b>" not in hostile


@pytest.mark.parametrize("name", ICON_STEMS)
def test_icon_tag_renders_every_vendored_icon(name: str) -> None:
    source = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
    html = _render(f"name {CLASS}=css", name=name, css="size-4")

    assert _inner(html) == _inner(source)
    # The tag's root keeps every presentation attribute of the file's root.
    own = dict(_parse(source).tags[0][1])
    rendered = dict(_parse(html).tags[0][1])
    for attribute in (CLASS, "stroke-width"):
        own.pop(attribute)
    assert own.items() <= rendered.items()


@pytest.mark.parametrize(
    ("arguments", "context", "message"),
    [
        (f'"zap-on" {CLASS}="size-4"', {}, "unknown icon"),
        (f'"../zap" {CLASS}="size-4"', {}, "unknown icon"),
        (f'"icons/zap" {CLASS}="size-4"', {}, "unknown icon"),
        (f'"zap.svg" {CLASS}="size-4"', {}, "unknown icon"),
        (f'"ZAP" {CLASS}="size-4"', {}, "unknown icon"),
        (f"name {CLASS}=css", {"name": "../../settings", "css": "size-4"}, "unknown icon"),
        (f"name {CLASS}=css", {"name": 7, "css": "size-4"}, "unknown icon"),
        (f'"zap" {CLASS}="size-4" title="Power"', {}, "unknown keyword"),
        ('"zap" css="size-4"', {}, "unknown keyword"),
        ('"zap"', {}, "class is required"),
        (f'"zap" {CLASS}=""', {}, "class is required"),
        (f'"zap" {CLASS}=css', {"css": None}, "class is required"),
        ('"zap" "size-4"', {}, "too many positional"),
    ],
)
def test_icon_tag_rejects_bad_input(arguments: str, context: dict[str, Any], message: str) -> None:
    with pytest.raises(TemplateSyntaxError, match=message):
        _render(arguments, **context)


def test_icon_names_are_allowlisted() -> None:
    from powermon.web.templatetags.icons import ICON_NAME, ICONS

    assert sorted(ICONS) == ICON_STEMS
    assert len(ICONS) == 44
    for name, inner in ICONS.items():
        assert ICON_NAME.fullmatch(name), name
        assert "<!--" not in inner and "<svg" not in inner, name
    # Failure: path-like and malformed names never match.
    for bad in ("../zap", "zap.svg", "icons/zap", "Zap", "z ap", "", "zap\n"):
        assert not ICON_NAME.fullmatch(bad), bad


def test_icons_module_has_one_mark_safe() -> None:
    assert ICONS_MODULE.is_file()
    calls = _mark_safe_calls(ICONS_MODULE.read_text(encoding="utf-8"))

    assert len(calls) == 1
    assert calls[0].endswith("# noqa: S308")
    # Failure: a second call, or one without the audited noqa, is visible to the check.
    twice = "a = mark_safe(x)  # noqa: S308\nb = mark_safe(y)\n"
    assert [call.endswith("# noqa: S308") for call in _mark_safe_calls(twice)] == [True, False]


def test_favicon_is_reproducible(tmp_path: Path) -> None:
    assert FAVICON_SCRIPT.is_file()
    assert FAVICON_ICO.is_file()
    from powermon.web.assets.favicon import render_ico

    committed = FAVICON_ICO.read_bytes()
    assert render_ico() == committed
    # The command line writes the same bytes.
    out = tmp_path / "favicon.ico"
    subprocess.run([sys.executable, str(FAVICON_SCRIPT), str(out)], check=True, cwd=tmp_path)
    assert out.read_bytes() == committed

    with Image.open(FAVICON_ICO) as ico:
        assert ico.format == "ICO"
        assert sorted(ico.info["sizes"]) == [(16, 16), (32, 32)]
        ico.size = (32, 32)
        mark = ico.convert("RGBA")
    # A rounded #0B1220 square (transparent corner) with the cyan zap on it.
    assert mark.getpixel((0, 0))[3] == 0
    assert mark.getpixel((4, 16)) == (*BACKGROUND, 255)
    pixels = [mark.getpixel((x, y)) for x in range(32) for y in range(32)]
    assert any(all(abs(p[i] - BRAND[i]) <= 24 for i in range(3)) for p in pixels)

    svg = FAVICON_SVG.read_text(encoding="utf-8")
    assert "@license" not in svg and "<!--" not in svg
    assert "#0B1220" in svg and "#22D3EE" in svg


def test_favicon_cli_rejects_a_missing_output_path() -> None:
    result = subprocess.run(
        [sys.executable, str(FAVICON_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


@pytest.mark.parametrize("name", ["web/favicon.svg", "web/favicon.ico"])
def test_favicon_static_names_are_hashed(name: str) -> None:
    stem, _, suffix = name.rpartition(".")
    pattern = rf"{re.escape(stem)}\.[0-9a-f]{{12}}\.{suffix}"
    assert re.fullmatch(pattern, staticfiles_storage.stored_name(name))
