"""Golden images of the chart (D-10, D-11, chart-spec §10 image-level check, CHRT-08).

Each case renders a fixture week and compares it with a committed PNG under
``tests/chart/goldens/``. A render passes when at most 0.5 % of its pixels differ from
the golden by more than 8/255 in any channel; the sizes must match exactly (D-11). The
mocks in ``docs/assets/`` are the visual target, not pixel references: the goldens are
the renderer's own output, approved by eye against the mocks (D-10), and the maintainer
gives the binding approval at ``/gsd-verify-work 3``.

The set: the chart-spec §10 sample week (live, Thu 01.10 14:37) in uk, en and ru, and
the finished-day renders of the DST Sundays 2026-10-25 (25 h) and 2027-03-28 (23 h) in
en (D-11, DoD 6, CHRT-06). A position probe checks the DST rows on the goldens
themselves, and the tolerance function has its own test.

Goldens are generated and compared only inside the Linux project image, never on a dev
host (Pitfall 6). They are regenerated only on purpose, after the D-10 eye check, by
setting ``CHART_GOLDENS_OUT`` to a mounted directory; the tests then write their renders
there instead of comparing. The image has no source mount, so the mount is the only way
out:

    docker compose -f docker-compose.local.yml run --build --rm --no-deps --user 501:20 \\
      -e CHART_GOLDENS_OUT=/goldens \\
      -v <absolute checkout path>/tests/chart/goldens:/goldens \\
      web pytest -q tests/chart/test_goldens.py

Pure: no database. Without ``CHART_GOLDENS_OUT`` a missing golden fails its test with
this command in the message, so a golden can never be skipped silently.
"""

import io
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from chart_fixtures import (
    DST_FALL_TODAY,
    DST_SPRING_TODAY,
    KYIV,
    SAMPLE_NAMES,
    SAMPLE_NOW,
    SAMPLE_TODAY,
    dst_fall_week,
    dst_spring_week,
    sample_pieces,
)
from PIL import Image, ImageChops

from powermon.chart import render
from powermon.chart.model import HOUR_US, Week, build_week, next_midnight

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"
OUT_ENV = "CHART_GOLDENS_OUT"
# D-11: at most 0.5 % of the pixels may differ by more than 8/255 in any channel.
THRESHOLD = 8
MAX_FRACTION = 0.005
REGENERATE = (
    "docker compose -f docker-compose.local.yml run --build --rm --no-deps --user 501:20 "
    f"-e {OUT_ENV}=/goldens -v <absolute checkout path>/tests/chart/goldens:/goldens "
    "web pytest -q tests/chart/test_goldens.py"
)


def diff_fraction(actual: Image.Image, golden: Image.Image, threshold: int = THRESHOLD) -> float:
    """The share of pixels whose largest channel difference is above ``threshold``.

    Compared in RGB with ImageChops (no numpy). ValueError when the sizes differ.
    """
    if actual.size != golden.size:
        raise ValueError(f"image size {actual.size} differs from the golden's {golden.size}")
    r, g, b = ImageChops.difference(actual.convert("RGB"), golden.convert("RGB")).split()
    worst = ImageChops.lighter(ImageChops.lighter(r, g), b)
    over = worst.point([255 if v > threshold else 0 for v in range(256)])
    return over.histogram()[255] / (actual.width * actual.height)


def _sample_week() -> Week:
    """The chart-spec §10 sample week, live at Thu 01.10 14:37."""
    return build_week(sample_pieces(), today=SAMPLE_TODAY, now=SAMPLE_NOW, tz=KYIV, live=True)


def _dst_fall_week() -> Week:
    """Sun 2026-10-25 (25 h) as its finished-day render: now is the next local midnight."""
    now = next_midnight(DST_FALL_TODAY, KYIV)
    return build_week(dst_fall_week(), today=DST_FALL_TODAY, now=now, tz=KYIV, live=False)


def _dst_spring_week() -> Week:
    """Sun 2027-03-28 (23 h) as its finished-day render: now is the next local midnight."""
    now = next_midnight(DST_SPRING_TODAY, KYIV)
    return build_week(dst_spring_week(), today=DST_SPRING_TODAY, now=now, tz=KYIV, live=False)


# Golden name -> (week, language, location name). D-11's set (the sample week in uk and
# en, the two DST Sundays) plus the ru sample week (03-RESEARCH open question 3).
CASES: dict[str, tuple[Callable[[], Week], str, str]] = {
    "sample-uk": (_sample_week, "uk", SAMPLE_NAMES["uk"]),
    "sample-en": (_sample_week, "en", SAMPLE_NAMES["en"]),
    "sample-ru": (_sample_week, "ru", SAMPLE_NAMES["ru"]),
    "dst-2026-10-25-en": (_dst_fall_week, "en", SAMPLE_NAMES["en"]),
    "dst-2027-03-28-en": (_dst_spring_week, "en", SAMPLE_NAMES["en"]),
}


def _render(name: str) -> bytes:
    make_week, lang, place = CASES[name]
    return render.render_png(make_week(), lang=lang, name=place)


def _golden(name: str) -> Image.Image:
    """The committed golden ``name`` as RGB; a missing one fails with the regeneration command."""
    path = GOLDENS_DIR / f"{name}.png"
    if not path.is_file():
        pytest.fail(
            f"golden {path.name} is missing. Goldens are regenerated only on purpose, after "
            f"the D-10 eye check, inside the Linux image:\n{REGENERATE}",
            pytrace=False,
        )
    with Image.open(path) as img:
        assert img.size == (render.W, render.H), img.size
        return img.convert("RGB")


def _check_golden(name: str) -> None:
    """Compare the ``name`` render with its golden, or write it in regeneration mode."""
    png = _render(name)
    out = os.environ.get(OUT_ENV)
    if out:
        Path(out, f"{name}.png").write_bytes(png)
        return
    golden = _golden(name)
    with Image.open(io.BytesIO(png)) as actual:
        fraction = diff_fraction(actual, golden)
    assert fraction <= MAX_FRACTION, (
        f"{name}: {fraction:.3%} of the pixels differ by more than {THRESHOLD}/255 "
        f"(limit {MAX_FRACTION:.1%})"
    )


def test_golden_sample_uk() -> None:
    _check_golden("sample-uk")


def test_golden_sample_en() -> None:
    _check_golden("sample-en")


def test_golden_sample_ru() -> None:
    _check_golden("sample-ru")


def test_golden_dst_2026_10_25_en() -> None:
    # DoD 6, CHRT-06: the 25 h fall-back Sunday as its finished-day render.
    _check_golden("dst-2026-10-25-en")


def test_golden_dst_2027_03_28_en() -> None:
    # DoD 6, CHRT-06: the 23 h spring-forward Sunday as its finished-day render.
    _check_golden("dst-2027-03-28-en")


def _skip_in_regeneration_mode() -> None:
    if os.environ.get(OUT_ENV):
        pytest.skip(f"{OUT_ENV} is set: the committed goldens are being replaced")


# Wall-clock hour -> the colour the Sunday bar must have there. Probes sit between hour
# separators, inside solid areas.
DST_POSITIONS: dict[str, list[tuple[float, render.RGB]]] = {
    # 03:30 EEST -> 03:30 EET is one real hour of OFF; only the later occurrence of the
    # repeated hour is drawn, so the bar is OFF at 03:00-03:30 and ON at 03:30-04:00.
    "dst-2026-10-25-en": [
        (2.5, render.ON),
        (3.25, render.OFF),
        (3.75, render.ON),
        (10.5, render.OFF),
        (11.5, render.OFF),
        (12.5, render.ON),
        (23.25, render.ON),
        (23 + 40 / 60, render.OFF),
    ],
    # 03:00-04:00 does not exist: it stays no data between ON on both sides.
    "dst-2027-03-28-en": [
        (2.5, render.ON),
        (3.5, render.NO_DATA),
        (4.5, render.ON),
        (10.5, render.OFF),
        (11.5, render.OFF),
        (12.5, render.ON),
    ],
}


def test_dst_goldens_show_the_spec_positions() -> None:
    # chart-spec §9, INV-08, DoD 6: on both DST Sundays positions follow the wall clock.
    _skip_in_regeneration_mode()
    for name, probes in DST_POSITIONS.items():
        make_week, lang, _ = CASES[name]
        week = make_week()
        lay = render.layout(week, lang)
        y = lay.bar_ys[week.today.weekday()] + 22
        golden = _golden(name)
        for hour, colour in probes:
            pixel = golden.getpixel((int(lay.hx(round(hour * HOUR_US))), y))
            assert pixel == colour, (name, hour, pixel)


def _solid(size: tuple[int, int] = (1280, 1000)) -> Image.Image:
    return Image.new("RGB", size, (100, 120, 140))


def test_diff_fraction_tolerance() -> None:
    base = _solid()
    # Expected: identical images differ nowhere.
    assert diff_fraction(base, base.copy()) == 0.0
    # One pixel off by 9 in one channel counts; by exactly 8 it does not (D-11: "more than").
    one = base.copy()
    one.putpixel((640, 500), (100, 129, 140))
    assert diff_fraction(one, base) == 1 / 1_280_000
    eight = base.copy()
    eight.putpixel((640, 500), (100, 112, 140))
    assert diff_fraction(eight, base) == 0.0
    # Edge: 6 400 pixels (0.5 %) off by 9 still pass the golden bound; 6 401 do not.
    limit = base.copy()
    limit.paste((91, 120, 140), (0, 0, 80, 80))
    assert diff_fraction(limit, base) == MAX_FRACTION
    over = limit.copy()
    over.putpixel((1279, 999), (100, 120, 149))
    assert diff_fraction(over, base) > MAX_FRACTION
    # Failure: different sizes never compare.
    with pytest.raises(ValueError, match="differs from the golden"):
        diff_fraction(_solid((1280, 999)), base)


def test_golden_set_is_complete() -> None:
    # Edge: the folder holds exactly the five goldens, each a 1280x1000 RGB PNG, and every
    # case has its own named test.
    _skip_in_regeneration_mode()
    names = {f"{name}.png" for name in CASES}
    assert names == {
        "sample-uk.png",
        "sample-en.png",
        "sample-ru.png",
        "dst-2026-10-25-en.png",
        "dst-2027-03-28-en.png",
    }
    assert {p.name for p in GOLDENS_DIR.iterdir()} == names
    for name in names:
        with Image.open(GOLDENS_DIR / name) as img:
            assert (img.format, img.size, img.mode) == ("PNG", (1280, 1000), "RGB"), name
    module = sys.modules[__name__]
    for name in CASES:
        assert callable(getattr(module, f"test_golden_{name.replace('-', '_')}", None)), name


def test_regeneration_mode_writes_the_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With CHART_GOLDENS_OUT set the test writes its render there instead of comparing.
    monkeypatch.setenv(OUT_ENV, str(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "GOLDENS_DIR", tmp_path / "absent")
    _check_golden("sample-en")
    written = tmp_path / "sample-en.png"
    assert written.read_bytes() == _render("sample-en")
    with Image.open(written) as img:
        assert (img.format, img.size, img.mode) == ("PNG", (1280, 1000), "RGB")


def test_a_missing_golden_fails_with_the_regeneration_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Failure: no golden and no CHART_GOLDENS_OUT is a failed test, never a skip.
    monkeypatch.delenv(OUT_ENV, raising=False)
    monkeypatch.setattr(sys.modules[__name__], "GOLDENS_DIR", tmp_path)
    with pytest.raises(pytest.fail.Exception, match=OUT_ENV) as failure:
        _check_golden("sample-uk")
    assert "sample-uk.png is missing" in str(failure.value)
    assert "--no-deps" in str(failure.value)
