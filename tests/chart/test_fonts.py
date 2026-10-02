"""The bundled Inter 4.1 fonts and the locked Pillow (D-09, D-12; docs/chart-spec.md 4 and 9).

Rendering is deterministic only with exactly these font files and this Pillow: the renderer
loads the fonts by file path with the BASIC layout, so no system font, fontconfig or RAQM
shaping can change a render. The SHA-256 values below are the ones the maintainer approved at
the 03-01 legitimacy checkpoint (Inter 4.1 release asset, extras/ttf/ and LICENSE.txt). A
swapped or corrupted file fails here before it can reach a render or a golden image.
"""

import hashlib
import pathlib

import PIL
import pytest
from PIL import ImageFont, features

FONT_DIR = pathlib.Path(__file__).resolve().parents[2] / "powermon" / "chart" / "fonts"

PINNED_SHA256 = {
    "Inter-Regular.ttf": "40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82",
    "Inter-Medium.ttf": "97ad806f526e41546d46365bb3a393145f75b7b1568913db74549ad8b8dba872",
    "Inter-SemiBold.ttf": "78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3",
    "LICENSE.txt": "262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a",
}
TTF_NAMES = sorted(name for name in PINNED_SHA256 if name.endswith(".ttf"))

# The uk and en chart titles (chart-spec section 8) and the Cyrillic letters chart-spec
# section 4 names, upper and lower case.
SAMPLES = ["Відключення світла", "Power outages", "ї є ґ і ё Ё Ї Є Ґ"]

ROW_LABEL_SIZE = 34


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fonts_are_the_pinned_inter_4_1() -> None:
    assert {name: _sha256(FONT_DIR / name) for name in PINNED_SHA256} == PINNED_SHA256
    # Edge: no other font file sits next to them, so a renderer can never pick up an
    # unpinned one from this directory.
    font_files = sorted(
        path.name for path in FONT_DIR.iterdir() if path.suffix.lower() in {".ttf", ".otf"}
    )
    assert font_files == TTF_NAMES


def test_pillow_is_the_locked_version_without_raqm() -> None:
    assert PIL.__version__ == "12.3.0"
    assert features.version("freetype2") == "2.14.3"
    # The image installs no FriBiDi, so RAQM is unavailable and BASIC is the only layout.
    assert features.check("raqm") is False


@pytest.mark.parametrize("name", TTF_NAMES)
def test_each_font_loads_by_path_with_basic_layout(name: str) -> None:
    font = ImageFont.truetype(
        str(FONT_DIR / name), ROW_LABEL_SIZE, layout_engine=ImageFont.Layout.BASIC
    )
    assert font.layout_engine == ImageFont.Layout.BASIC
    assert font.getname()[0] == "Inter"
    for text in SAMPLES:
        assert font.getlength(text) > 0, text


def test_a_missing_font_path_raises() -> None:
    with pytest.raises(OSError):
        ImageFont.truetype(
            str(FONT_DIR / "Missing.ttf"), ROW_LABEL_SIZE, layout_engine=ImageFont.Layout.BASIC
        )
