"""Subscriber-facing chart strings in uk, en and ru (CHRT-03, CHRT-04, CHRT-08).

The labels of the chart image and the caption under it, verbatim from docs/chart-spec.md
section 8. Both captions are one line; the finished-day one (D-13) puts the weekday and date
in place of "Today". A day with no on or off time at all (its row total is "—") gets the
neutral caption form, the legend's "Not monitored", instead of a claim of no outages
(Phase 4 D-03). Every table has the same keys and shape in every language, and an unknown
language falls back to en (docs/v1-lessons.md section 2). The renderer and the chart
lifecycle only call into this module, so each string has one tested source.

Text is built from integers and dates only: integer microseconds of OFF time, an outage
count and a local date. Durations come from the one formatter, ``format_total_duration``
(D-16), so a row total, the caption and the alert's "was OFF for" agree once rounded to
minutes (INV-03). No user-typed text and no markup ever reach a caption, which is sent as
plain text. (The location name, the image's only user-typed text, is the renderer's
business.)

Inputs are checked up front, so a function fails the same way whichever branch it would
take: a count or a duration that is not an int (a bool is not) raises TypeError, a negative
one ValueError; a day that is not a ``datetime.date`` raises TypeError, and so does a
``datetime`` (a stored instant's date is the UTC date, not the local one); a language that
is not a str raises TypeError, and so does a ``monitored`` flag that is not a bool.

Pure: imports nothing from Django. Like ``duration.py`` it never divides with "/" and never
calls the built-in rounding (a test scans every file in this package); the plural rules use
"%" only.
"""

from datetime import date, datetime

from powermon.i18n.duration import MIN_US, format_total_duration
from powermon.i18n.strings import LANGUAGES, resolve_language

NO_DATA_TOTAL = "—"  # "—": the total of a day with no on or off time
DOT = " · "  # " · ": between a duration and its count
DASH = " – "  # " – ": between the two dates of the week range

TITLE: dict[str, str] = {
    "uk": "Відключення світла",
    "en": "Power outages",
    "ru": "Отключения света",
}

# Monday first, in date.weekday() order.
WEEKDAYS: dict[str, tuple[str, ...]] = {
    "uk": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "ru": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
}

# January first. Genitive in uk and ru ("28 вересня"), abbreviated in en ("28 Sep").
MONTHS: dict[str, tuple[str, ...]] = {
    "uk": (
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
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ru": (
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
}

# The four legend items in their fixed order: on, off, not monitored, no data.
LEGEND: dict[str, tuple[str, str, str, str]] = {
    "uk": ("Світло є", "Світла немає", "Не відстежувалось", "Немає даних"),
    "en": ("Power on", "Power off", "Not monitored", "No data"),
    "ru": ("Свет есть", "Света нет", "Не отслеживалось", "Нет данных"),
}

TOTALS_HEADER: dict[str, str] = {
    "uk": "без світла · разів",
    "en": "off time · outages",
    "ru": "без света · раз",
}

# The caption of the "last week" divider above the previous-week rows.
DIVIDER: dict[str, str] = {
    "uk": "минулого тижня",
    "en": "last week",
    "ru": "прошлой недели",
}

# The total of a monitored day without an outage.
NO_OUTAGES: dict[str, str] = {
    "uk": "без відключень",
    "en": "no outages",
    "ru": "без отключений",
}

# Captions (chart-spec section 8, D-13). ``{count}`` is the plural phrase from ``outages``
# ("2 відключення"), ``{duration}`` the shared total formatter's text and ``{day}`` the
# weekday and date ("Чт 01.10"). The ``*_unmonitored`` forms are for a day with no on or off
# time at all, in the legend's "Not monitored" words (D-03).
CAPTIONS: dict[str, dict[str, str]] = {
    "uk": {
        "today_off": "Сьогодні без світла: {duration} · {count}",
        "today_none": "Сьогодні відключень не було",
        "today_unmonitored": "Сьогодні: не відстежувалось",
        "day_off": "{day} без світла: {duration} · {count}",
        "day_none": "{day} відключень не було",
        "day_unmonitored": "{day}: не відстежувалось",
    },
    "en": {
        "today_off": "Today off: {duration} · {count}",
        "today_none": "No outages today",
        "today_unmonitored": "Today: not monitored",
        "day_off": "{day} off: {duration} · {count}",
        "day_none": "No outages on {day}",
        "day_unmonitored": "{day}: not monitored",
    },
    "ru": {
        "today_off": "Сегодня без света: {duration} · {count}",
        "today_none": "Сегодня отключений не было",
        "today_unmonitored": "Сегодня: не отслеживалось",
        "day_off": "{day} без света: {duration} · {count}",
        "day_none": "{day} отключений не было",
        "day_unmonitored": "{day}: не отслеживалось",
    },
}

# The outage noun per CLDR plural category (chart-spec section 8): uk and ru have one, few
# and many; en has one and other.
OUTAGE_NOUN: dict[str, dict[str, str]] = {
    "uk": {"one": "відключення", "few": "відключення", "many": "відключень"},
    "en": {"one": "outage", "other": "outages"},
    "ru": {"one": "отключение", "few": "отключения", "many": "отключений"},
}

# The totals column's width probe (chart-spec section 3): the longest possible row total.
_WORST_OFF_US = (23 * 60 + 59) * MIN_US
_WORST_COUNT = 12


def _lang(lang: str) -> str:
    """``lang`` if it has string tables, else en; TypeError when it is not a str."""
    if not isinstance(lang, str):
        raise TypeError(f"a language must be a str, not {type(lang).__name__}")
    return resolve_language(lang)


def _check_count(value: int, name: str) -> None:
    """TypeError unless ``value`` is an int (a bool is not), ValueError if negative."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must not be negative: {value}")


def _check_day(day: date) -> None:
    """TypeError unless ``day`` is a local ``datetime.date`` (a ``datetime`` is not)."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError(f"a day must be a datetime.date, not {type(day).__name__}")


def _check_monitored(monitored: bool) -> None:
    """TypeError unless ``monitored`` is a bool (0, 1 and None are not)."""
    if not isinstance(monitored, bool):
        raise TypeError(f"monitored must be a bool, not {type(monitored).__name__}")


def plural_form(n: int, lang: str) -> str:
    """The CLDR plural category of ``n``: "one", "few" or "many" (uk, ru), "one" or "other" (en).

    uk and ru: one if n % 10 == 1 and n % 100 != 11 (1, 21, 101); few if n % 10 is 2-4 and
    n % 100 is not 12-14 (2, 22, 104); many otherwise (0, 5-20, 25, 111).
    """
    _check_count(n, "n")
    lang = _lang(lang)
    if lang == "en":
        return "one" if n == 1 else "other"
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def outages(n: int, lang: str) -> str:
    """``n`` and the plural noun: ``2 outages``, ``1 відключення``, ``5 отключений``."""
    lang = _lang(lang)
    return f"{n} {OUTAGE_NOUN[lang][plural_form(n, lang)]}"


def weekday(day: date, lang: str) -> str:
    """The short weekday name: ``Чт``, ``Thu``; Sunday is ``Нд`` in uk and ``Вс`` in ru."""
    _check_day(day)
    return WEEKDAYS[_lang(lang)][day.weekday()]


def row_date(day: date) -> str:
    """The row date, ``DD.MM`` in every language: ``01.10``."""
    _check_day(day)
    return day.strftime("%d.%m")


def weekday_date(day: date, lang: str) -> str:
    """The weekday and the row date: ``Чт 01.10``, ``Thu 01.10``."""
    return f"{weekday(day, lang)} {row_date(day)}"


def week_range(monday: date, sunday: date, lang: str) -> str:
    """The subtitle's week: ``28 вересня – 4 жовтня 2026``, ``28 Sep – 4 Oct 2026``.

    The year is printed once, at the end, or on both dates when the week spans two years
    (``29 грудня 2025 – 4 січня 2026``). ValueError if ``sunday`` is before ``monday``.
    """
    _check_day(monday)
    _check_day(sunday)
    if sunday < monday:
        raise ValueError(f"the week ends before it starts: {monday} - {sunday}")
    months = MONTHS[_lang(lang)]
    start = f"{monday.day} {months[monday.month - 1]}"
    if monday.year != sunday.year:
        start = f"{start} {monday.year}"
    return f"{start}{DASH}{sunday.day} {months[sunday.month - 1]} {sunday.year}"


def row_total(off_us: int, count: int, monitored: bool, lang: str) -> tuple[str, str]:
    """A day row's total as (main text, count suffix) for the two colours of the image.

    chart-spec section 8 "Daily total": a day with no on or off time (``monitored`` False)
    gives ``("—", "")``; a monitored day without an outage gives the zero-outages text and
    ``""``; otherwise ``(format_total_duration(off_us), " · {count}")``, e.g.
    ``("3h 20m", " · 2")``. ``off_us`` is the day's OFF time in integer microseconds.
    """
    _check_count(off_us, "off_us")
    _check_count(count, "count")
    _check_monitored(monitored)
    lang = _lang(lang)
    if not monitored:
        return NO_DATA_TOTAL, ""
    if count == 0:
        return NO_OUTAGES[lang], ""
    return format_total_duration(off_us, lang), f"{DOT}{count}"


def worst_total(lang: str) -> str:
    """The widest row total, ``23h 59m · 12`` (chart-spec section 3 totals column probe)."""
    duration, suffix = row_total(_WORST_OFF_US, _WORST_COUNT, True, lang)
    return f"{duration}{suffix}"


def _summary(
    kind: str, off_us: int, count: int, lang: str, *, monitored: bool = True, **fields: str
) -> str:
    """The caption for ``kind`` "today" or "day": the off time and outages, or none.

    A day with no on or off time at all (``monitored`` False) gets the neutral form, "not
    monitored", whatever the count: the chart shows it as unknown, so the caption must not
    claim "no outages" (D-03).
    """
    captions = CAPTIONS[lang]
    if not monitored:
        return captions[f"{kind}_unmonitored"].format(**fields)
    if count == 0:
        return captions[f"{kind}_none"].format(**fields)
    return captions[f"{kind}_off"].format(
        duration=format_total_duration(off_us, lang), count=outages(count, lang), **fields
    )


def live_caption(off_us: int, count: int, lang: str, *, monitored: bool = True) -> str:
    """Today's caption, one line: the day so far, with no update time (CHRT-04).

    ``Today off: 4h 10m · 2 outages``; with no outage today, ``No outages today``; with no
    on or off time today at all (``monitored`` False, the row total "—"), ``Today: not
    monitored`` (D-03). The image's now pill shows the render time, so the caption carries
    none. ``off_us`` is today's OFF time so far in integer microseconds (the same integer
    as today's row total), ``count`` the number of outages, ``monitored`` the row's flag (a
    bool; the default True is the monitored day).
    """
    _check_count(off_us, "off_us")
    _check_count(count, "count")
    _check_monitored(monitored)
    lang = _lang(lang)
    return _summary("today", off_us, count, lang, monitored=monitored)


def finished_caption(
    off_us: int, count: int, day: date, lang: str, *, monitored: bool = True
) -> str:
    """A finished day's caption, one line like the live caption, with no update time (D-13).

    ``Thu 01.10 off: 4h 10m · 2 outages``, or ``No outages on Thu 01.10``; uk and ru put
    the weekday and date first in both forms (``Чт 01.10 відключень не було``). A day with
    no on or off time at all (``monitored`` False) gets ``Thu 01.10: not monitored``
    (D-03). ``day`` is the local date of the finished day.
    """
    _check_count(off_us, "off_us")
    _check_count(count, "count")
    _check_monitored(monitored)
    lang = _lang(lang)
    return _summary("day", off_us, count, lang, monitored=monitored, day=weekday_date(day, lang))


def all_strings() -> list[str]:
    """Every fixed string the chart image can draw, in every language, without duplicates.

    For the chart-spec section 10 cmap check: the title, weekdays, months, legend, totals
    header, divider and zero-outages text of each language, its widest total and its
    ``<1`` total, and the "—", " · " and " – " marks. The caption is not in the list: it is
    Telegram text, drawn with the reader's own fonts.
    """
    strings: list[str] = []
    for lang in LANGUAGES:
        strings.append(TITLE[lang])
        strings.extend(WEEKDAYS[lang])
        strings.extend(MONTHS[lang])
        strings.extend(LEGEND[lang])
        strings.extend((TOTALS_HEADER[lang], DIVIDER[lang], NO_OUTAGES[lang]))
        strings.append(worst_total(lang))
        strings.append(format_total_duration(0, lang))
    strings.extend((NO_DATA_TOTAL, DOT, DASH))
    return list(dict.fromkeys(strings))
