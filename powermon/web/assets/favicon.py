"""First-party favicon.ico for the admin: the brand mark (06-UI-SPEC "Iconography").

A 32x32 rounded square in #0B1220 with the Lucide ``zap`` outline stroked in #22D3EE, the same
art as ``static/web/favicon.svg``. Pillow cannot rasterise SVG, so the zap is drawn as a closed
polygon through the corner points of its path in ``templates/icons/zap.svg`` (the path's small
rounded corners become short straight cuts), with round joins, at 8x and then reduced, which
anti-aliases the edges. The ICO holds a 16 px and a 32 px image.

Pillow is pinned in uv.lock, so the bytes are reproducible: tests/web/test_icons.py
regenerates the committed ``static/web/favicon.ico`` and compares them. After a deliberate
change to the art, regenerate the file inside the app image:

    python powermon/web/assets/favicon.py powermon/web/static/web/favicon.ico
"""

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 32
# Drawn at SIZE * SCALE, then reduced to SIZE and SIZE / 2.
SCALE = 8
RADIUS = 6
BACKGROUND = "#0B1220"
STROKE = "#22D3EE"
STROKE_WIDTH = 2
# The 24-unit Lucide viewBox centred in the 32-unit square.
OFFSET = 4
# The corner points of the Lucide zap path, in path order (viewBox 0 0 24 24).
ZAP = (
    (15.914, 4.0),
    (13.44, 2.439),
    (4.44, 11.439),
    (5.5, 14.0),
    (9.502, 14.0),
    (9.973, 14.666),
    (8.086, 20.0),
    (10.561, 21.56),
    (19.561, 12.56),
    (18.5, 10.0),
    (14.503, 10.0),
    (14.031, 9.333),
)


def _mark() -> Image.Image:
    """The brand mark at SIZE * SCALE pixels, transparent outside the rounded square."""
    side = SIZE * SCALE
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, side - 1, side - 1), radius=RADIUS * SCALE, fill=BACKGROUND)
    points = [((x + OFFSET) * SCALE, (y + OFFSET) * SCALE) for x, y in ZAP]
    # A closed outline with round joins (stroke-linejoin="round"): the first two points are
    # repeated so the first corner gets its join too.
    draw.line([*points, *points[:2]], fill=STROKE, width=STROKE_WIDTH * SCALE, joint="curve")
    return image


def render_ico() -> bytes:
    """The favicon.ico bytes: the mark at 32 and 16 px."""
    mark = _mark()
    large = mark.reduce(SCALE)
    small = mark.reduce(SCALE * 2)
    buffer = io.BytesIO()
    large.save(buffer, format="ICO", sizes=[(16, 16), (32, 32)], append_images=[small])
    return buffer.getvalue()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python powermon/web/assets/favicon.py <out.ico>", file=sys.stderr)
        return 2
    Path(argv[1]).write_bytes(render_ico())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
