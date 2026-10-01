"""English texts of the admin's ops notices (D-10, D-11).

Fixed English templates (the admin UI is English-only, D-10) in Telegram HTML. Times are
shown in the display TZ the caller passes in (``powermon.i18n.times``), and durations use
the shared alert formatter (``format_alert_duration``, docs/chart-spec.md section 8). A
location name is admin-typed text, so every text HTML-escapes it (``&``, ``<``, ``>``);
``escape=False`` gives the plain text for a log line (D-09). Nothing here is stored: an ops
outbox row holds integers only, and its text is rendered at send time (OPS-08).

Pure: imports nothing from Django.
"""

import html
from datetime import datetime

from powermon.i18n.times import event_prefix

# Subscriber alert kind -> how a notice names it.
_LABELS = {"power_off": "OFF", "power_on": "ON"}


def _name(name: str, escape: bool) -> str:
    return html.escape(name, quote=False) if escape else name


def _label(kind: str) -> str:
    try:
        return _LABELS[kind]
    except KeyError:
        raise ValueError(f"unknown alert kind: {kind!r}") from None


def uncertain(
    kind: str,
    event_at: datetime,
    name: str,
    *,
    interrupted: bool,
    now: datetime,
    tz: str,
    escape: bool = True,
) -> str:
    """D-11 #5: an alert that may have reached Telegram is not resent; check the channel."""
    reason = (
        "the worker stopped while sending it"
        if interrupted
        else "Telegram timed out after the request was sent"
    )
    return (
        f"❓ {_label(kind)} alert for {_name(name, escape)} "
        f"(event {event_prefix(event_at, now, tz)}) may not have been delivered ({reason}). "
        "It will not be resent; please check the channel."
    )
