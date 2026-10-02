"""Subscriber-facing chart strings in uk, en and ru (CHRT-03, CHRT-04, CHRT-08).

The caption under the chart photo, verbatim from docs/chart-spec.md section 8 and, for the
finished-day render, D-13: line 1 only, with the weekday and date in place of "Today".
Every table has the same keys in every language, and an unknown language falls back to en
(docs/v1-lessons.md section 2).

Text is built from integers and dates only: integer microseconds of OFF time, an outage
count and the caller's local ``HH:MM``. Durations come from the one formatter,
``format_total_duration`` (D-16), so a row total, the caption and the alert's "was OFF for"
agree once rounded to minutes (INV-03). No user-typed text and no markup ever reach a
caption, which is sent as plain text.

Pure: imports nothing from Django. Like ``duration.py`` it never divides with "/" and never
calls the built-in rounding (a test scans every file in this package); the plural rules use
"%" only.
"""

import re

from powermon.i18n.duration import format_total_duration
from powermon.i18n.strings import resolve_language

# Captions (chart-spec section 8, D-13). ``{count}`` is the plural phrase from ``outages``
# ("2 відключення"), ``{duration}`` the shared total formatter's text, ``{time}`` HH:MM and
# ``{day}`` the weekday and date ("Чт 01.10").
CAPTIONS: dict[str, dict[str, str]] = {
    "uk": {
        "today_off": "Сьогодні без світла: {duration} · {count}",
        "today_none": "Сьогодні відключень не було",
        "updated": "Оновлено о {time}",
        "day_off": "{day} без світла: {duration} · {count}",
        "day_none": "{day} відключень не було",
    },
    "en": {
        "today_off": "Today off: {duration} · {count}",
        "today_none": "No outages today",
        "updated": "Updated {time}",
        "day_off": "{day} off: {duration} · {count}",
        "day_none": "No outages on {day}",
    },
    "ru": {
        "today_off": "Сегодня без света: {duration} · {count}",
        "today_none": "Сегодня отключений не было",
        "updated": "Обновлено в {time}",
        "day_off": "{day} без света: {duration} · {count}",
        "day_none": "{day} отключений не было",
    },
}

# The outage noun per CLDR plural category (chart-spec section 8): uk and ru have one, few
# and many; en has one and other.
OUTAGE_NOUN: dict[str, dict[str, str]] = {
    "uk": {"one": "відключення", "few": "відключення", "many": "відключень"},
    "en": {"one": "outage", "other": "outages"},
    "ru": {"one": "отключение", "few": "отключения", "many": "отключений"},
}

# The update time as the caller formats it with strftime("%H:%M"); explicit ASCII digits.
_HM_RE = re.compile(r"[0-9]{2}:[0-9]{2}")


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


def live_caption(off_us: int, count: int, updated_hm: str, lang: str) -> str:
    """Today's caption, two lines: the day so far, then the update time (CHRT-04).

    ``Today off: 4h 10m · 2 outages`` + newline + ``Updated 14:37``; with no outage today,
    ``No outages today`` + newline + ``Updated 14:37``. ``off_us`` is today's OFF time so
    far in integer microseconds (the same integer as today's row total), ``count`` the
    number of outages, ``updated_hm`` the render time as local ``HH:MM``.
    """
    _check_count(off_us, "off_us")
    _check_count(count, "count")
    if not isinstance(updated_hm, str):
        raise TypeError(f"the update time must be a str, not {type(updated_hm).__name__}")
    if not _HM_RE.fullmatch(updated_hm):
        raise ValueError(f"the update time must be HH:MM, not {updated_hm!r}")
    lang = _lang(lang)
    captions = CAPTIONS[lang]
    if count == 0:
        line1 = captions["today_none"]
    else:
        line1 = captions["today_off"].format(
            duration=format_total_duration(off_us, lang), count=outages(count, lang)
        )
    return f"{line1}\n{captions['updated'].format(time=updated_hm)}"
