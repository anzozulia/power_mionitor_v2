"""Chart caption and labels, snapshot-tested in uk, en and ru (CHRT-03, CHRT-04, CHRT-08).

docs/chart-spec.md section 8 fixes every string, and D-13 the finished-day caption (line 1
only, the weekday and date in place of "Today"). docs/v1-lessons.md section 4: snapshot
every caption and label in all three languages, so each expected string is written out
literally. The module is pure, so these tests need no database or settings.
"""

from typing import Any

import pytest

from powermon.i18n import chart_texts

S = 1_000_000
MIN = 60 * S
H = 60 * MIN

# 4 h 10 min of OFF time and 2 outages, rendered at 14:37 (03-CONTEXT Specific Ideas).
LIVE_WITH_OUTAGES = {
    "uk": "Сьогодні без світла: 4 год 10 хв · 2 відключення\nОновлено о 14:37",
    "en": "Today off: 4h 10m · 2 outages\nUpdated 14:37",
    "ru": "Сегодня без света: 4 ч 10 мин · 2 отключения\nОбновлено в 14:37",
}
LIVE_WITHOUT_OUTAGES = {
    "uk": "Сьогодні відключень не було\nОновлено о 14:37",
    "en": "No outages today\nUpdated 14:37",
    "ru": "Сегодня отключений не было\nОбновлено в 14:37",
}


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_live_caption_today_with_outages(lang: str) -> None:
    caption = chart_texts.live_caption(4 * H + 10 * MIN, 2, "14:37", lang)
    assert caption == LIVE_WITH_OUTAGES[lang]


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_live_caption_without_outages(lang: str) -> None:
    assert chart_texts.live_caption(0, 0, "14:37", lang) == LIVE_WITHOUT_OUTAGES[lang]


@pytest.mark.parametrize(
    ("off_us", "count", "updated_hm", "error"),
    [
        (0, -1, "14:37", ValueError),
        (0, True, "14:37", TypeError),
        (0, 1, "14:37:05", ValueError),
        (0, 1, "7:05", ValueError),
        (1.5, 1, "14:37", TypeError),
    ],
    ids=["negative-count", "bool-count", "seconds-in-time", "one-digit-hour", "float-off-time"],
)
def test_live_caption_rejects_bad_input(
    off_us: Any, count: Any, updated_hm: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        chart_texts.live_caption(off_us, count, updated_hm, "uk")
