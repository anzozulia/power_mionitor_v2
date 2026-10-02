"""Chart caption and labels, snapshot-tested in uk, en and ru (CHRT-03, CHRT-04, CHRT-08).

docs/chart-spec.md section 8 fixes every string, D-13 the finished-day caption (line 1
only, the weekday and date in place of "Today"), and Phase 4 D-03 the neutral caption of a
day with no on or off time ("not monitored"). docs/v1-lessons.md section 4: snapshot
every caption and label in all three languages, so each expected string is written out
literally. The module is pure, so these tests need no database or settings.

Functions and tables are reached as ``chart_texts.<name>`` inside each test (bad-input rows
name the function as a string), so a missing name fails its own test, not the collection.
"""

import string
from datetime import UTC, date, datetime
from typing import Any

import pytest

from powermon.i18n import chart_texts
from powermon.i18n.duration import format_total_duration
from powermon.i18n.strings import LANGUAGES, resolve_language

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

# The finished-day caption for Thu 2026-10-01 (D-13): line 1 only, no "Updated" line.
FINISHED_WITH_OUTAGES = {
    "uk": "Чт 01.10 без світла: 4 год 10 хв · 2 відключення",
    "en": "Thu 01.10 off: 4h 10m · 2 outages",
    "ru": "Чт 01.10 без света: 4 ч 10 мин · 2 отключения",
}
FINISHED_WITHOUT_OUTAGES = {
    "uk": "Чт 01.10 відключень не було",
    "en": "No outages on Thu 01.10",
    "ru": "Чт 01.10 отключений не было",
}

# D-03 (chart-spec §8 amendment): a day with no on or off time at all gets the neutral form,
# the legend's "Not monitored"; the live chart keeps line 2, a finished day has line 1 only.
LIVE_UNMONITORED = {
    "uk": "Сьогодні: не відстежувалось\nОновлено о 12:05",
    "en": "Today: not monitored\nUpdated 12:05",
    "ru": "Сегодня: не отслеживалось\nОбновлено в 12:05",
}
FINISHED_UNMONITORED = {
    "uk": "Чт 01.10: не відстежувалось",
    "en": "Thu 01.10: not monitored",
    "ru": "Чт 01.10: не отслеживалось",
}

# Every image label of chart-spec section 8, per language.
LABELS: dict[str, dict[str, Any]] = {
    "uk": {
        "title": "Відключення світла",
        "weekdays": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"),
        "months": (
            "січня",
            "лютого",
            "березня",
            "квітня",
            "травня",
            "червня",
            "липня",
            "серпня",
            "вересня",
            "жовтня",
            "листопада",
            "грудня",
        ),
        "legend": ("Світло є", "Світла немає", "Не відстежувалось", "Немає даних"),
        "totals_header": "без світла · разів",
        "divider": "минулого тижня",
        "no_outages": "без відключень",
    },
    "en": {
        "title": "Power outages",
        "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "months": (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        "legend": ("Power on", "Power off", "Not monitored", "No data"),
        "totals_header": "off time · outages",
        "divider": "last week",
        "no_outages": "no outages",
    },
    "ru": {
        "title": "Отключения света",
        "weekdays": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
        "months": (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        "legend": ("Свет есть", "Света нет", "Не отслеживалось", "Нет данных"),
        "totals_header": "без света · раз",
        "divider": "прошлой недели",
        "no_outages": "без отключений",
    },
}

# Letters only one of the two Cyrillic languages has: a ru string must not carry a
# Ukrainian letter and a uk string not a Russian one (v1 mixed them up).
UK_ONLY = set("ІіЇїЄєҐґ")
RU_ONLY = set("ЫыЭэЪъЁё")


def _image_strings(lang: str) -> list[str]:
    return [
        chart_texts.TITLE[lang],
        *chart_texts.WEEKDAYS[lang],
        *chart_texts.MONTHS[lang],
        *chart_texts.LEGEND[lang],
        chart_texts.TOTALS_HEADER[lang],
        chart_texts.DIVIDER[lang],
        chart_texts.NO_OUTAGES[lang],
    ]


def _all_table_strings(lang: str) -> list[str]:
    return [
        *_image_strings(lang),
        *chart_texts.CAPTIONS[lang].values(),
        *chart_texts.OUTAGE_NOUN[lang].values(),
    ]


def _fields(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


# --- Today's caption (Task 1) -------------------------------------------------------------


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


# --- Image labels (CHRT-08) ---------------------------------------------------------------


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_labels_per_language(lang: str) -> None:
    expected = LABELS[lang]
    assert chart_texts.TITLE[lang] == expected["title"]
    assert chart_texts.WEEKDAYS[lang] == expected["weekdays"]
    assert chart_texts.MONTHS[lang] == expected["months"]
    assert chart_texts.LEGEND[lang] == expected["legend"]
    assert chart_texts.TOTALS_HEADER[lang] == expected["totals_header"]
    assert chart_texts.DIVIDER[lang] == expected["divider"]
    assert chart_texts.NO_OUTAGES[lang] == expected["no_outages"]


def test_labels_spot_checks() -> None:
    # The cells of chart-spec section 8 that differ between uk and ru, and en's short months.
    assert chart_texts.WEEKDAYS["uk"][6] == "Нд"
    assert chart_texts.WEEKDAYS["ru"][6] == "Вс"
    assert chart_texts.MONTHS["en"][8] == "Sep"
    assert chart_texts.NO_DATA_TOTAL == "—"
    assert chart_texts.DOT == " · "
    assert chart_texts.DASH == " – "


def test_tables_have_identical_shapes() -> None:
    tables: list[dict[str, Any]] = [
        chart_texts.TITLE,
        chart_texts.WEEKDAYS,
        chart_texts.MONTHS,
        chart_texts.LEGEND,
        chart_texts.TOTALS_HEADER,
        chart_texts.DIVIDER,
        chart_texts.NO_OUTAGES,
        chart_texts.CAPTIONS,
        chart_texts.OUTAGE_NOUN,
    ]
    for table in tables:
        assert set(table) == set(LANGUAGES)
    for lang in LANGUAGES:
        assert len(chart_texts.WEEKDAYS[lang]) == 7
        assert len(chart_texts.MONTHS[lang]) == 12
        assert len(chart_texts.LEGEND[lang]) == 4
    # Captions: the same keys and the same placeholders in every language.
    caption_keys = {
        "today_off",
        "today_none",
        "today_unmonitored",
        "updated",
        "day_off",
        "day_none",
        "day_unmonitored",
    }
    for lang in LANGUAGES:
        assert set(chart_texts.CAPTIONS[lang]) == caption_keys
        for key in caption_keys:
            assert _fields(chart_texts.CAPTIONS[lang][key]) == _fields(
                chart_texts.CAPTIONS["en"][key]
            )
    # Plural nouns: one/few/many for the East Slavic pair, one/other for en.
    assert set(chart_texts.OUTAGE_NOUN["uk"]) == {"one", "few", "many"}
    assert set(chart_texts.OUTAGE_NOUN["ru"]) == {"one", "few", "many"}
    assert set(chart_texts.OUTAGE_NOUN["en"]) == {"one", "other"}
    for lang in LANGUAGES:
        for n in range(200):
            assert chart_texts.plural_form(n, lang) in chart_texts.OUTAGE_NOUN[lang]


def test_no_letters_of_the_other_language() -> None:
    cyrillic = {chr(cp) for cp in range(0x0400, 0x0500)}
    assert [s for s in _all_table_strings("ru") if UK_ONLY & set(s)] == []
    assert [s for s in _all_table_strings("uk") if RU_ONLY & set(s)] == []
    assert [s for s in _all_table_strings("en") if cyrillic & set(s)] == []


# --- Dates: weekday, row date, subtitle week range ----------------------------------------


@pytest.mark.parametrize(
    ("day", "uk", "en", "ru"),
    [
        (date(2026, 10, 1), "Чт 01.10", "Thu 01.10", "Чт 01.10"),
        (date(2026, 10, 4), "Нд 04.10", "Sun 04.10", "Вс 04.10"),
        (date(2026, 9, 28), "Пн 28.09", "Mon 28.09", "Пн 28.09"),
        (date(2027, 1, 5), "Вт 05.01", "Tue 05.01", "Вт 05.01"),
    ],
    ids=["thu", "sun", "mon", "zero-padded"],
)
def test_weekday_date(day: date, uk: str, en: str, ru: str) -> None:
    assert (
        chart_texts.weekday_date(day, "uk"),
        chart_texts.weekday_date(day, "en"),
        chart_texts.weekday_date(day, "ru"),
    ) == (uk, en, ru)
    assert chart_texts.weekday(day, "en") == en.split(" ")[0]
    assert chart_texts.row_date(day) == uk.split(" ")[1]


@pytest.mark.parametrize(
    ("monday", "sunday", "uk", "en", "ru"),
    [
        (
            date(2026, 9, 28),
            date(2026, 10, 4),
            "28 вересня – 4 жовтня 2026",
            "28 Sep – 4 Oct 2026",
            "28 сентября – 4 октября 2026",
        ),
        (
            date(2025, 12, 29),
            date(2026, 1, 4),
            "29 грудня 2025 – 4 січня 2026",
            "29 Dec 2025 – 4 Jan 2026",
            "29 декабря 2025 – 4 января 2026",
        ),
        (
            date(2026, 10, 5),
            date(2026, 10, 11),
            "5 жовтня – 11 жовтня 2026",
            "5 Oct – 11 Oct 2026",
            "5 октября – 11 октября 2026",
        ),
    ],
    ids=["chart-spec-week", "spans-two-years", "one-month"],
)
def test_week_range(monday: date, sunday: date, uk: str, en: str, ru: str) -> None:
    assert (
        chart_texts.week_range(monday, sunday, "uk"),
        chart_texts.week_range(monday, sunday, "en"),
        chart_texts.week_range(monday, sunday, "ru"),
    ) == (uk, en, ru)


# --- Row totals (CHRT-03) -----------------------------------------------------------------

# (off_us, count, monitored, uk, en, ru); chart-spec section 10 totals table, plus the
# no-data day and the INV-11 row (an outage split by not-monitored time counts once).
ROW_TOTALS = [
    (0, 0, True, ("без відключень", ""), ("no outages", ""), ("без отключений", "")),
    (29 * S, 1, True, ("<1 хв", " · 1"), ("<1m", " · 1"), ("<1 мин", " · 1")),
    (30 * S, 1, True, ("1 хв", " · 1"), ("1m", " · 1"), ("1 мин", " · 1")),
    (59 * MIN + 30 * S, 1, True, ("1 год", " · 1"), ("1h", " · 1"), ("1 ч", " · 1")),
    (
        3 * H + 20 * MIN,
        2,
        True,
        ("3 год 20 хв", " · 2"),
        ("3h 20m", " · 2"),
        ("3 ч 20 мин", " · 2"),
    ),
    (4 * H, 1, True, ("4 год", " · 1"), ("4h", " · 1"), ("4 ч", " · 1")),
    (
        H + 50 * MIN,
        1,
        True,
        ("1 год 50 хв", " · 1"),
        ("1h 50m", " · 1"),
        ("1 ч 50 мин", " · 1"),
    ),
    (0, 0, False, ("—", ""), ("—", ""), ("—", "")),
]
ROW_TOTAL_IDS = ["0s", "29s", "30s", "59m30s", "3h20m", "4h", "INV-11", "no-data"]


@pytest.mark.parametrize(
    ("off_us", "count", "monitored", "uk", "en", "ru"), ROW_TOTALS, ids=ROW_TOTAL_IDS
)
def test_row_total_chart_spec_table(
    off_us: int,
    count: int,
    monitored: bool,
    uk: tuple[str, str],
    en: tuple[str, str],
    ru: tuple[str, str],
) -> None:
    assert (
        chart_texts.row_total(off_us, count, monitored, "uk"),
        chart_texts.row_total(off_us, count, monitored, "en"),
        chart_texts.row_total(off_us, count, monitored, "ru"),
    ) == (uk, en, ru)


def test_worst_total() -> None:
    assert (
        chart_texts.worst_total("uk"),
        chart_texts.worst_total("en"),
        chart_texts.worst_total("ru"),
    ) == ("23 год 59 хв · 12", "23h 59m · 12", "23 ч 59 мин · 12")


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
@pytest.mark.parametrize(
    "off_us", [29 * S, 30 * S, 4 * H + 10 * MIN + 29 * S, 4 * H + 10 * MIN + 30 * S]
)
def test_INV03_caption_and_row_total_share_the_formatter(off_us: int, lang: str) -> None:
    # One formatter (D-16): today's row total and the caption print the same off time, the
    # same text the alert formatter's family produces for that integer.
    duration, suffix = chart_texts.row_total(off_us, 2, True, lang)
    assert duration == format_total_duration(off_us, lang)
    line1 = chart_texts.live_caption(off_us, 2, "14:37", lang).split("\n")[0]
    assert line1.endswith(f": {duration} · {chart_texts.outages(2, lang)}")
    assert suffix == " · 2"


# --- Finished-day caption (D-13) ----------------------------------------------------------


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_finished_caption(lang: str) -> None:
    day = date(2026, 10, 1)
    with_outages = chart_texts.finished_caption(4 * H + 10 * MIN, 2, day, lang)
    without = chart_texts.finished_caption(0, 0, day, lang)
    assert with_outages == FINISHED_WITH_OUTAGES[lang]
    assert without == FINISHED_WITHOUT_OUTAGES[lang]
    assert "\n" not in with_outages
    assert "\n" not in without


# --- Neutral caption for a day with no monitored time (D-03, Phase 3 IN-05) ---------------


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_D03_live_caption_for_an_unmonitored_today(lang: str) -> None:
    caption = chart_texts.live_caption(0, 0, "12:05", lang, monitored=False)
    assert caption == LIVE_UNMONITORED[lang]


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_D03_finished_caption_for_an_unmonitored_day(lang: str) -> None:
    caption = chart_texts.finished_caption(0, 0, date(2026, 10, 1), lang, monitored=False)
    assert caption == FINISHED_UNMONITORED[lang]
    assert "\n" not in caption


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_D03_monitored_day_keeps_the_existing_forms(lang: str) -> None:
    day = date(2026, 10, 1)
    off = 4 * H + 10 * MIN
    assert (
        chart_texts.live_caption(off, 2, "14:37", lang, monitored=True) == (LIVE_WITH_OUTAGES[lang])
    )
    assert (
        chart_texts.live_caption(0, 0, "14:37", lang, monitored=True)
        == (LIVE_WITHOUT_OUTAGES[lang])
    )
    assert (
        chart_texts.finished_caption(off, 2, day, lang, monitored=True)
        == (FINISHED_WITH_OUTAGES[lang])
    )
    assert (
        chart_texts.finished_caption(0, 0, day, lang, monitored=True)
        == (FINISHED_WITHOUT_OUTAGES[lang])
    )


def test_D03_neutral_caption_is_the_legends_not_monitored() -> None:
    # The words are the legend's "Not monitored" in each language (D-03).
    for lang in LANGUAGES:
        words = chart_texts.LEGEND[lang][2].lower()
        assert chart_texts.CAPTIONS[lang]["today_unmonitored"].endswith(f": {words}")
        assert chart_texts.CAPTIONS[lang]["day_unmonitored"] == "{day}: " + words


@pytest.mark.parametrize("monitored", [0, 1, None], ids=["zero", "one", "none"])
def test_D03_monitored_must_be_a_bool(monitored: Any) -> None:
    with pytest.raises(TypeError, match="monitored must be a bool"):
        chart_texts.live_caption(0, 0, "12:05", "en", monitored=monitored)
    with pytest.raises(TypeError, match="monitored must be a bool"):
        chart_texts.finished_caption(0, 0, date(2026, 10, 1), "en", monitored=monitored)


# --- Plurals (CHRT-04) --------------------------------------------------------------------

# n, uk noun, ru noun, uk/ru category (CLDR: one / few / many).
PLURALS = [
    (0, "відключень", "отключений", "many"),
    (1, "відключення", "отключение", "one"),
    (2, "відключення", "отключения", "few"),
    (5, "відключень", "отключений", "many"),
    (11, "відключень", "отключений", "many"),
    (12, "відключень", "отключений", "many"),
    (14, "відключень", "отключений", "many"),
    (21, "відключення", "отключение", "one"),
    (22, "відключення", "отключения", "few"),
    (25, "відключень", "отключений", "many"),
    (111, "відключень", "отключений", "many"),
]


@pytest.mark.parametrize(("n", "uk", "ru", "category"), PLURALS, ids=[str(p[0]) for p in PLURALS])
def test_plurals(n: int, uk: str, ru: str, category: str) -> None:
    assert chart_texts.outages(n, "uk") == f"{n} {uk}"
    assert chart_texts.outages(n, "ru") == f"{n} {ru}"
    assert chart_texts.outages(n, "en") == (f"{n} outage" if n == 1 else f"{n} outages")
    assert chart_texts.plural_form(n, "uk") == category
    assert chart_texts.plural_form(n, "ru") == category
    assert chart_texts.plural_form(n, "en") == ("one" if n == 1 else "other")


@pytest.mark.parametrize("n", [1, 2, 5, 11, 21, 22])
def test_caption_plurals(n: int) -> None:
    # chart-spec section 10: the caption's plural forms for n = 1, 2, 5, 11, 21, 22.
    nouns = {row[0]: (row[1], row[2]) for row in PLURALS}
    uk, ru = nouns[n]
    assert chart_texts.live_caption(H, n, "14:37", "uk").startswith(
        f"Сьогодні без світла: 1 год · {n} {uk}\n"
    )
    assert chart_texts.live_caption(H, n, "14:37", "ru").startswith(
        f"Сегодня без света: 1 ч · {n} {ru}\n"
    )


# --- Fallback, the cmap list, bad inputs --------------------------------------------------


def test_unknown_language_falls_back_to_en() -> None:
    day = date(2026, 10, 1)
    assert chart_texts.live_caption(4 * H + 10 * MIN, 2, "14:37", "de") == LIVE_WITH_OUTAGES["en"]
    assert chart_texts.finished_caption(0, 0, day, "de") == FINISHED_WITHOUT_OUTAGES["en"]
    assert chart_texts.worst_total("de") == chart_texts.worst_total("en")
    assert chart_texts.row_total(0, 0, True, "de") == ("no outages", "")
    assert chart_texts.weekday_date(day, "de") == "Thu 01.10"
    assert chart_texts.week_range(date(2026, 9, 28), day, "de") == "28 Sep – 1 Oct 2026"
    assert chart_texts.outages(2, "de") == "2 outages"
    assert chart_texts.TITLE[resolve_language("de")] == "Power outages"


def test_all_strings_covers_every_table() -> None:
    strings = chart_texts.all_strings()
    assert all(isinstance(s, str) and s for s in strings)
    present = set(strings)
    for lang in LANGUAGES:
        missing = [s for s in _image_strings(lang) if s not in present]
        assert missing == []
        assert chart_texts.worst_total(lang) in present
        assert format_total_duration(29 * S, lang) in present  # "<1 хв"
    assert "—" in present


BAD_INPUTS = [
    ("plural_form", (-1, "uk"), ValueError),
    ("outages", (-1, "uk"), ValueError),
    ("plural_form", (True, "en"), TypeError),
    ("week_range", (date(2026, 10, 4), date(2026, 9, 28), "uk"), ValueError),
    ("week_range", ("2026-09-28", date(2026, 10, 4), "en"), TypeError),
    ("row_total", (-1, 1, True, "uk"), ValueError),
    ("row_total", (-1, 0, True, "uk"), ValueError),
    ("row_total", (0, -1, True, "en"), ValueError),
    ("row_total", (1.5, 1, True, "en"), TypeError),
    ("row_total", (60_000_000, True, True, "uk"), TypeError),
    ("row_total", (0, 0, 1, "en"), TypeError),
    ("finished_caption", (0, -1, date(2026, 10, 1), "uk"), ValueError),
    ("finished_caption", (-1, 0, date(2026, 10, 1), "uk"), ValueError),
    ("finished_caption", (0, 0, "2026-10-01", "uk"), TypeError),
    ("weekday_date", ("2026-10-01", "uk"), TypeError),
    ("weekday", (datetime(2026, 10, 1, 12, tzinfo=UTC), "uk"), TypeError),
    ("row_date", (None,), TypeError),
    ("worst_total", (None,), TypeError),
    ("live_caption", (0, 0, "14:37", None), TypeError),
    ("live_caption", (0, 0, 1437, "uk"), TypeError),
]
BAD_INPUT_IDS = [
    "plural_form-negative",
    "outages-negative",
    "plural_form-bool",
    "week_range-reversed",
    "week_range-str-day",
    "row_total-negative-off",
    "row_total-negative-off-count-0",
    "row_total-negative-count",
    "row_total-float-off",
    "row_total-bool-count",
    "row_total-int-monitored",
    "finished_caption-negative-count",
    "finished_caption-negative-off-count-0",
    "finished_caption-str-day",
    "weekday_date-str-day",
    "weekday-datetime",
    "row_date-none",
    "worst_total-none-lang",
    "live_caption-none-lang",
    "live_caption-int-time",
]


@pytest.mark.parametrize(("name", "args", "error"), BAD_INPUTS, ids=BAD_INPUT_IDS)
def test_bad_inputs(name: str, args: tuple[Any, ...], error: type[Exception]) -> None:
    with pytest.raises(error):
        getattr(chart_texts, name)(*args)
