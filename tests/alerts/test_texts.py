"""Subscriber alert text (ALRT-01, ALRT-02), snapshot-tested in uk, en and ru.

docs/v1-lessons.md section 4: snapshot every alert string in all three languages
(v1 shipped "Света не было : 3ч", with a stray space before the colon).
"""

import pytest

from powermon.alerts.texts import render_alert
from powermon.i18n.strings import ALERTS, LANGUAGES, SEP, UNITS

S = 1_000_000
MIN = 60 * S
H = 60 * MIN

SNAPSHOTS = [
    # The worked examples in .planning/PROJECT.md (Alert format).
    (
        "power_off",
        "uk",
        5 * H + 12 * MIN,
        "🔴 <b>СВІТЛО ЗНИКЛО</b>\n⚡ Світло було: <b>5 год 12 хв</b>",
    ),
    (
        "power_on",
        "en",
        3 * H + 15 * MIN,
        "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>3h 15m</b>",
    ),
    (
        "power_on",
        "uk",
        3 * H + 15 * MIN,
        "🟢 <b>СВІТЛО ПОВЕРНУЛОСЯ</b>\n⚡ Світла не було: <b>3 год 15 хв</b>",
    ),
    (
        "power_off",
        "en",
        5 * MIN,
        "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>",
    ),
    (
        "power_off",
        "ru",
        5 * H + 12 * MIN,
        "🔴 <b>СВЕТ ВЫКЛЮЧИЛСЯ</b>\n⚡ Свет был: <b>5 ч 12 мин</b>",
    ),
    (
        "power_on",
        "ru",
        3 * H + 15 * MIN,
        "🟢 <b>СВЕТ ВЕРНУЛСЯ</b>\n⚡ Света не было: <b>3 ч 15 мин</b>",
    ),
    (
        "power_on",
        "ru",
        29 * H,
        "🟢 <b>СВЕТ ВЕРНУЛСЯ</b>\n⚡ Света не было: <b>1 д 5 ч</b>",
    ),
    (
        "power_off",
        "uk",
        45 * S,
        "🔴 <b>СВІТЛО ЗНИКЛО</b>\n⚡ Світло було: <b>45 с</b>",
    ),
]


@pytest.mark.parametrize(
    ("kind", "lang", "duration_us", "expected"),
    SNAPSHOTS,
    ids=[f"{kind}-{lang}-{us}" for kind, lang, us, _ in SNAPSHOTS],
)
def test_alert_snapshots(kind: str, lang: str, duration_us: int, expected: str) -> None:
    assert render_alert(kind, lang, duration_us) == expected


def test_K2_off_alert_states_the_on_time() -> None:
    # K-2: heartbeats until 10:05:00 after power came on at 10:00:00 -> "was ON for" 5 min.
    assert render_alert("power_off", "en", 5 * MIN) == (
        "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
    )


def test_K3_on_alert_says_power_was_off_for_55m() -> None:
    # K-3: outage from 10:05:00, heartbeat at 11:00:00.
    assert render_alert("power_on", "en", 55 * MIN) == (
        "🟢 <b>POWER ON</b>\n⚡ Power was OFF for: <b>55m</b>"
    )


@pytest.mark.parametrize("lang", ["uk", "en", "ru"])
def test_no_space_before_the_colon(lang: str) -> None:
    for kind in ("power_off", "power_on"):
        text = render_alert(kind, lang, 3 * H)
        assert " :" not in text
        assert ": <b>" in text


def test_string_tables_have_identical_keys() -> None:
    assert LANGUAGES == ("uk", "en", "ru")
    assert set(ALERTS) == set(LANGUAGES)
    assert set(UNITS) == set(LANGUAGES)
    assert set(SEP) == set(LANGUAGES)
    assert ALERTS["uk"].keys() == ALERTS["en"].keys() == ALERTS["ru"].keys()
    assert set(ALERTS["en"]) == {"off_status", "on_status", "was_on", "was_off"}
    assert all(len(units) == 4 and all(units) for units in UNITS.values())


def test_unknown_language_falls_back_to_en() -> None:
    assert render_alert("power_off", "de", 300_000_000) == (
        "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
    )


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="power_flicker"):
        render_alert("power_flicker", "en", 0)


def test_negative_duration_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        render_alert("power_on", "en", -1)
