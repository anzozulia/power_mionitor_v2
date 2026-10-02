"""The bundled font's code points: the chart-spec §10 cmap check and the D-15 name filter.

- ``glyphs.INTER_RANGES`` is the committed copy of the cmap the three Inter 4.1 TTFs
  share. It is checked here against the fonts themselves with fontTools, a dev tool that
  powermon/ never imports; tests/chart/test_fonts.py pins the font bytes by SHA-256.
- Every character of every localized chart string, the digits and the marks the image
  draws are in the cmap of each bundled TTF, so the chart never shows a tofu box.
- ``printable_name`` (D-15): the location name, the only user-typed text in the image,
  keeps only printable characters the bundled font covers, after NFC normalization.
"""

import pathlib
from itertools import pairwise

import pytest
from fontTools.ttLib import TTFont

from powermon.chart import glyphs
from powermon.i18n import chart_texts

FONT_DIR = pathlib.Path(__file__).resolve().parents[2] / "powermon" / "chart" / "fonts"
TTF_NAMES = ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf")
# Drawn besides the chart_texts strings: row dates (DD.MM), the axis and the pill (HH:MM),
# the separators and the subtitle's truncation mark (chart-spec §10).
EXTRA_CHARS = "0123456789.:·–—<…"


@pytest.fixture(scope="module")
def cmaps() -> dict[str, set[int]]:
    return {name: set(TTFont(str(FONT_DIR / name)).getBestCmap()) for name in TTF_NAMES}


def _expand(ranges: tuple[tuple[int, int], ...]) -> set[int]:
    return {cp for first, last in ranges for cp in range(first, last + 1)}


def test_glyph_table_equals_the_fonts_cmap(cmaps: dict[str, set[int]]) -> None:
    common = set.intersection(*cmaps.values())
    # The scan saw the real fonts: Inter 4.1 maps thousands of code points.
    assert len(common) > 2000
    assert _expand(glyphs.INTER_RANGES) == common
    # Sorted, inclusive and maximal: no range is empty, overlaps or touches the next one.
    for first, last in glyphs.INTER_RANGES:
        assert first <= last
    for (_, last), (first, _) in pairwise(glyphs.INTER_RANGES):
        assert last + 1 < first


def test_every_chart_string_is_in_the_cmap(cmaps: dict[str, set[int]]) -> None:
    strings = chart_texts.all_strings()
    # The scan saw the real tables: titles, weekdays, months and more in three languages.
    assert len(strings) > 50
    chars = set("".join(strings)) | set(EXTRA_CHARS)
    offenders = sorted(
        f"{name}: U+{ord(ch):04X} {ch!r}"
        for name, cmap in cmaps.items()
        for ch in chars
        if ord(ch) not in cmap
    )
    assert offenders == []
    # The committed table agrees, so the D-15 filter never drops a chart character.
    assert [ch for ch in sorted(chars) if not glyphs.covered(ord(ch))] == []


@pytest.mark.parametrize(
    ("cp", "expected"),
    [
        (ord("A"), True),
        (ord(" "), True),
        (ord("~"), True),
        (ord("й"), True),
        (ord("ї"), True),
        (0x1F, False),  # a control character just below the first printable range
        (0x7F, False),  # DEL, just after "~"
        (ord("家"), False),
        (ord("🏠"), False),
        (0x1F852, True),  # the last code point of the table
        (0x1F853, False),
        (0x10FFFF, False),
        (-1, False),
    ],
)
def test_covered(cp: int, expected: bool) -> None:
    assert glyphs.covered(cp) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("🏠 Дім", "Дім"),
        ("Дім, Оболонь", "Дім, Оболонь"),
        ("Home, Obolon", "Home, Obolon"),
        ("A\x00B", "AB"),  # a control character, although U+0000 is in Inter's cmap
        ("A​B", "AB"),  # zero-width space: in the cmap, not printable
        ("﻿Home", "Home"),  # byte-order mark
        ("Office", "Office"),  # private use: in the cmap, not printable
        ("Home\nOffice", "Home Office"),  # Pillow would break the line
        ("Home Office", "Home Office"),  # no-break space
        ("  a   b  ", "a b"),
        ("й", "й"),  # decomposed й: NFC composes it, BASIC layout cannot
        ("家", ""),
        ("🏠", ""),
        ("", ""),
    ],
)
def test_printable_name(name: str, expected: str) -> None:
    assert glyphs.printable_name(name) == expected


def test_printable_name_rejects_a_non_string() -> None:
    with pytest.raises(TypeError):
        glyphs.printable_name(None)  # type: ignore[arg-type]
