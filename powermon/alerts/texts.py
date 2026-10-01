"""Subscriber alert text (ALRT-01, ALRT-02, ALRT-04).

Telegram HTML: the status emoji and the bold status, then the measured duration of the
previous state, in the location's language. The text holds only fixed strings from
``powermon.i18n.strings`` and a formatted number, so there is nothing to escape
(docs/v1-lessons.md section 2). It states only the observed status and a measured
duration: no prediction, no location name, no user-typed text. The relay renders it at
send time; it is never stored.

A late alert (sent more than 120 s after it was recorded, D-07) states when the event
happened: the relay passes the event's local time as ``prefix``, which goes before the
bold status with no preposition, so all three languages share one format (D-05):
``🔴 17:27 <b>СВІТЛО ЗНИКЛО</b>``. When the event's local date is not the delivery's, the
prefix carries the date too: ``🔴 30.09 23:58 <b>POWER OFF</b>`` (D-06). The prefix may
hold only ASCII digits, "." and ":" and one space, so it needs no escaping either. With no
prefix the text is exactly Phase 1's. (The later ALRT-07 only has to pass a prefix on
every alert.)
"""

import re

from powermon.i18n.duration import format_alert_duration
from powermon.i18n.strings import ALERTS, resolve_language

# kind -> (emoji, status key, previous-state label key)
_KINDS: dict[str, tuple[str, str, str]] = {
    "power_off": ("🔴", "off_status", "was_on"),
    "power_on": ("🟢", "on_status", "was_off"),
}
# "HH:MM", or "DD.MM HH:MM" (powermon.i18n.times.event_prefix); explicit ASCII digits.
_PREFIX_RE = re.compile(r"(?:[0-9]{2}[.][0-9]{2} )?[0-9]{2}:[0-9]{2}")


def render_alert(kind: str, lang: str, duration_us: int, prefix: str | None = None) -> str:
    """``🔴 <b>POWER OFF</b>`` + newline + ``⚡ Power was ON for: <b>5h 12m</b>``.

    ``kind`` is "power_off" or "power_on"; any other kind raises ValueError. An unknown
    language falls back to en. ``duration_us`` is the previous state's length in integer
    microseconds. ``prefix`` is a late alert's local event time, ``HH:MM`` or
    ``DD.MM HH:MM``, put before the bold status (D-05, D-06); anything else raises
    ValueError.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown alert kind: {kind!r}")
    if prefix is not None and not _PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"an alert prefix must be HH:MM or DD.MM HH:MM, not {prefix!r}")
    emoji, status_key, label_key = _KINDS[kind]
    lang = resolve_language(lang)
    strings = ALERTS[lang]
    duration = format_alert_duration(duration_us, lang)
    head = emoji if prefix is None else f"{emoji} {prefix}"
    return f"{head} <b>{strings[status_key]}</b>\n⚡ {strings[label_key]}: <b>{duration}</b>"
