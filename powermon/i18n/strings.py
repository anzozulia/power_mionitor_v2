"""Subscriber-facing strings: alert texts and duration units in uk, en and ru (D-15).

The alert strings are verbatim from .planning/PROJECT.md (Alert format), the units from
docs/chart-spec.md section 8, with the day unit settled by D-16. Every language has the
same keys, and an unknown language falls back to en (docs/v1-lessons.md section 2).
The admin's test message (Phase 4 D-11) is a fixed text per language, too.

Pure: imports nothing from Django.
"""

LANGUAGES: tuple[str, ...] = ("uk", "en", "ru")
FALLBACK_LANGUAGE = "en"

ALERTS: dict[str, dict[str, str]] = {
    "uk": {
        "off_status": "СВІТЛО ЗНИКЛО",
        "on_status": "СВІТЛО ПОВЕРНУЛОСЯ",
        "was_on": "Світло було",
        "was_off": "Світла не було",
    },
    "en": {
        "off_status": "POWER OFF",
        "on_status": "POWER ON",
        "was_on": "Power was ON for",
        "was_off": "Power was OFF for",
    },
    "ru": {
        "off_status": "СВЕТ ВЫКЛЮЧИЛСЯ",
        "on_status": "СВЕТ ВЕРНУЛСЯ",
        "was_on": "Свет был",
        "was_off": "Света не было",
    },
}

# Duration units as (day, hour, minute, second).
UNITS: dict[str, tuple[str, str, str, str]] = {
    "uk": ("д", "год", "хв", "с"),
    "en": ("d", "h", "m", "s"),
    "ru": ("д", "ч", "мин", "с"),
}

# Between a number and its unit: a space in uk and ru ("5 год 12 хв"), none in en ("5h 12m").
SEP: dict[str, str] = {"uk": " ", "en": "", "ru": " "}

# The admin's test message (D-11), verbatim: one silent post that checks the location's bot
# token and chat. It is not an alert and holds no user-typed text. It goes out with
# parse_mode HTML like every message, so it contains no "<", ">" or "&".
TEST_MESSAGE: dict[str, str] = {
    "uk": "🔧 Тестове повідомлення Power Monitor: бот може публікувати тут.",
    "en": "🔧 Power Monitor test message: the bot can post here.",
    "ru": "🔧 Тестовое сообщение Power Monitor: бот может публиковать здесь.",
}


def resolve_language(lang: str) -> str:
    """Return ``lang`` if it has string tables, else the en fallback."""
    return lang if lang in ALERTS else FALLBACK_LANGUAGE


def telegram_test_text(lang: str) -> str:
    """The admin's test message (D-11) in ``lang``; an unknown language gets the en text."""
    return TEST_MESSAGE[resolve_language(lang)]
