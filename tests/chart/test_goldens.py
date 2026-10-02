"""Golden images of the chart (D-10, D-11, chart-spec §10 image-level check, CHRT-08).

Each case renders a fixture week and compares it with a committed PNG under
``tests/chart/goldens/``. A render passes when at most 0.5 % of its pixels differ from
the golden by more than 8/255 in any channel; the sizes must match exactly (D-11). The
mocks in ``docs/assets/`` are the visual target, not pixel references: the goldens are
the renderer's own output, approved by eye against the mocks (D-10), and the maintainer
gives the binding approval at ``/gsd-verify-work 3``.

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
from chart_fixtures import KYIV, SAMPLE_NAMES, SAMPLE_NOW, SAMPLE_TODAY, sample_pieces
from PIL import Image, ImageChops

from powermon.chart import render
from powermon.chart.model import Week, build_week

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


# Golden name -> (week, language, location name).
CASES: dict[str, tuple[Callable[[], Week], str, str]] = {
    "sample-uk": (_sample_week, "uk", SAMPLE_NAMES["uk"]),
    "sample-en": (_sample_week, "en", SAMPLE_NAMES["en"]),
}


def _render(name: str) -> bytes:
    make_week, lang, place = CASES[name]
    return render.render_png(make_week(), lang=lang, name=place)


def _check_golden(name: str) -> None:
    """Compare the ``name`` render with its golden, or write it in regeneration mode."""
    png = _render(name)
    out = os.environ.get(OUT_ENV)
    if out:
        Path(out, f"{name}.png").write_bytes(png)
        return
    path = GOLDENS_DIR / f"{name}.png"
    if not path.is_file():
        pytest.fail(
            f"golden {path.name} is missing. Goldens are regenerated only on purpose, after "
            f"the D-10 eye check, inside the Linux image:\n{REGENERATE}",
            pytrace=False,
        )
    with Image.open(path) as golden:
        golden.load()
        assert golden.size == (render.W, render.H), golden.size
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
