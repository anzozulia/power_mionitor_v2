"""Subscriber alert text (ALRT-01, ALRT-02).

Telegram HTML: the status emoji and the bold status, then the measured duration of the
previous state, in the location's language. The text holds only fixed strings from
``powermon.i18n.strings`` and a formatted number, so there is nothing to escape
(docs/v1-lessons.md section 2). It states only the observed status and a measured
duration: no prediction, no location name, no user-typed text. The relay renders it at
send time; it is never stored.
"""

from powermon.i18n.duration import format_alert_duration
from powermon.i18n.strings import ALERTS, resolve_language

# kind -> (emoji, status key, previous-state label key)
_KINDS: dict[str, tuple[str, str, str]] = {
    "power_off": ("🔴", "off_status", "was_on"),
    "power_on": ("🟢", "on_status", "was_off"),
}


def render_alert(kind: str, lang: str, duration_us: int) -> str:
    """``🔴 <b>POWER OFF</b>`` + newline + ``⚡ Power was ON for: <b>5h 12m</b>``.

    ``kind`` is "power_off" or "power_on"; any other kind raises ValueError. An unknown
    language falls back to en. ``duration_us`` is the previous state's length in integer
    microseconds.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown alert kind: {kind!r}")
    emoji, status_key, label_key = _KINDS[kind]
    lang = resolve_language(lang)
    strings = ALERTS[lang]
    duration = format_alert_duration(duration_us, lang)
    return f"{emoji} <b>{strings[status_key]}</b>\n⚡ {strings[label_key]}: <b>{duration}</b>"
