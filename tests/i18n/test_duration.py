"""The one duration formatter (docs/chart-spec.md section 8, D-16).

Alerts use format_alert_duration; the Phase 3 row totals and caption use
format_total_duration. Inputs are integer microseconds, the unit every stored duration
uses, so the table below spells each instant out in microseconds.
"""

import ast
import pathlib

import pytest

from powermon.i18n.duration import format_alert_duration, format_total_duration

S = 1_000_000
MIN = 60 * S
H = 60 * MIN
D = 24 * H

# (microseconds, en, uk, ru). Bands: seconds below 1 min, minutes and seconds below 1 h,
# hours and minutes below 24 h, days, hours and minutes from 24 h. Half up to the smallest
# unit shown, carried into the next band; zero parts left out; space before the unit in
# uk and ru only.
ALERT_CASES = [
    (0, "0s", "0 с", "0 с"),
    (400_000, "0s", "0 с", "0 с"),
    (500_000, "1s", "1 с", "1 с"),
    (45 * S, "45s", "45 с", "45 с"),
    (59 * S + 400_000, "59s", "59 с", "59 с"),
    (59 * S + 500_000, "1m", "1 хв", "1 мин"),
    (61 * S, "1m 1s", "1 хв 1 с", "1 мин 1 с"),
    (12 * MIN + 5 * S, "12m 5s", "12 хв 5 с", "12 мин 5 с"),
    (55 * MIN, "55m", "55 хв", "55 мин"),
    (59 * MIN + 59 * S + 400_000, "59m 59s", "59 хв 59 с", "59 мин 59 с"),
    (59 * MIN + 59 * S + 600_000, "1h", "1 год", "1 ч"),
    (H + 29 * S + 900_000, "1h", "1 год", "1 ч"),
    (H + 30 * S, "1h 1m", "1 год 1 хв", "1 ч 1 мин"),
    (5 * H + 12 * MIN, "5h 12m", "5 год 12 хв", "5 ч 12 мин"),
    (3 * H + 15 * MIN, "3h 15m", "3 год 15 хв", "3 ч 15 мин"),
    (23 * H + 59 * MIN + 29 * S, "23h 59m", "23 год 59 хв", "23 ч 59 мин"),
    (23 * H + 59 * MIN + 30 * S, "1d", "1 д", "1 д"),
    (29 * H, "1d 5h", "1 д 5 год", "1 д 5 ч"),
    (D + 5 * MIN, "1d 5m", "1 д 5 хв", "1 д 5 мин"),
    (10 * D + 3 * H + 7 * MIN, "10d 3h 7m", "10 д 3 год 7 хв", "10 д 3 ч 7 мин"),
]
IDS = [str(case[0]) for case in ALERT_CASES]

I18N_DIR = pathlib.Path(__file__).resolve().parents[2] / "powermon" / "i18n"


def test_table_spells_the_band_carry_instant_in_microseconds() -> None:
    # 59 min 59.6 s, the chart-spec section 8 carry example.
    assert 59 * MIN + 59 * S + 600_000 == 3_599_600_000


@pytest.mark.parametrize(("us", "en", "uk", "ru"), ALERT_CASES, ids=IDS)
def test_duration_table_en(us: int, en: str, uk: str, ru: str) -> None:
    assert format_alert_duration(us, "en") == en


@pytest.mark.parametrize(("us", "en", "uk", "ru"), ALERT_CASES, ids=IDS)
def test_duration_table_uk(us: int, en: str, uk: str, ru: str) -> None:
    assert format_alert_duration(us, "uk") == uk


@pytest.mark.parametrize(("us", "en", "uk", "ru"), ALERT_CASES, ids=IDS)
def test_duration_table_ru(us: int, en: str, uk: str, ru: str) -> None:
    assert format_alert_duration(us, "ru") == ru


def test_negative_duration_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_alert_duration(-1, "en")


def test_non_integer_duration_raises() -> None:
    # Floats drift and Python's built-in rounding is half-even (Pitfall 4).
    with pytest.raises(TypeError, match="integer microseconds"):
        format_alert_duration(1.5, "en")  # type: ignore[arg-type]


def test_unknown_language_formats_as_en() -> None:
    assert format_alert_duration(5 * H + 12 * MIN, "de") == "5h 12m"


# Row totals and the caption: hours and minutes only, minutes half up, "<1m" when some
# OFF time rounds to 0 minutes, never a day unit (chart-spec section 8).
TOTAL_CASES = [
    (29 * S, "<1m", "<1 хв", "<1 мин"),
    (30 * S, "1m", "1 хв", "1 мин"),
    (45 * MIN, "45m", "45 хв", "45 мин"),
    (59 * MIN + 30 * S, "1h", "1 год", "1 ч"),
    (3 * H + 20 * MIN, "3h 20m", "3 год 20 хв", "3 ч 20 мин"),
    (4 * H, "4h", "4 год", "4 ч"),
    (25 * H, "25h", "25 год", "25 ч"),
]


@pytest.mark.parametrize(
    ("us", "en", "uk", "ru"), TOTAL_CASES, ids=[str(case[0]) for case in TOTAL_CASES]
)
def test_total_duration_cases(us: int, en: str, uk: str, ru: str) -> None:
    assert (
        format_total_duration(us, "en"),
        format_total_duration(us, "uk"),
        format_total_duration(us, "ru"),
    ) == (en, uk, ru)


def test_total_duration_negative_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_total_duration(-1, "en")


def _i18n_sources() -> list[pathlib.Path]:
    sources = sorted(I18N_DIR.glob("*.py"))
    assert sources, f"no Python files to scan in {I18N_DIR}"
    return sources


def test_formatter_source_never_calls_builtin_rounding() -> None:
    # Pitfall 4: the built-in is half-even (2.5 -> 2), not the half-up chart-spec section 8
    # asks for. Any reference to the name counts, so an alias cannot slip through.
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in _i18n_sources()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Name) and node.id == "round"
    ]
    assert offenders == []


def test_formatter_source_has_no_true_division() -> None:
    # "/" produces a float; durations stay integer microseconds end to end.
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in _i18n_sources()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div)
    ]
    assert offenders == []
