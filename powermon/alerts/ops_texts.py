"""English texts of the admin's ops notices (D-10, D-11).

Fixed English templates (the admin UI is English-only, D-10) in Telegram HTML. Times are
shown in the display TZ the caller passes in (``powermon.i18n.times``), and durations use
the shared alert formatter (``format_alert_duration``, docs/chart-spec.md section 8). A
location name is admin-typed text, so every text HTML-escapes it (``&``, ``<``, ``>``);
``escape=False`` gives the plain text for a log line (D-09). Nothing here is stored: an ops
outbox row holds integers only, and its text is rendered at send time (OPS-08).

The all-silent texts stay neutral (D-12, Pitfall 11). Ukrainian queue blackouts really do
take several locations off the grid at once, so silence everywhere is named as either an
area power/ISP outage or a server/network problem, never as a confirmed cause, and the
notice says that subscriber alerts go on as normal.

The chart pin texts (Phase 3 D-07) name the HTTP status of Telegram's refusal as a short
``http_NNN`` code only, never Telegram's description.

Pure: imports nothing from Django.
"""

import html
from datetime import datetime, timedelta

from powermon.i18n.duration import format_alert_duration
from powermon.i18n.times import event_prefix, hms, span, when_s

# Subscriber alert kind -> how a notice names it.
_LABELS = {"power_off": "OFF", "power_on": "ON"}
_ONE_US = timedelta(microseconds=1)


def _name(name: str, escape: bool) -> str:
    return html.escape(name, quote=False) if escape else name


def _label(kind: str) -> str:
    try:
        return _LABELS[kind]
    except KeyError:
        raise ValueError(f"unknown alert kind: {kind!r}") from None


def _duration(delta: timedelta) -> str:
    """The shared alert format (``10m 28s``, ``6h``); a negative span raises ValueError."""
    return format_alert_duration(delta // _ONE_US, "en")


def _elapsed(start: datetime, end: datetime) -> str:
    """``end - start`` in the shared alert format; a naive time raises ValueError."""
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    return _duration(end - start)


def gap(start: datetime, end: datetime, tz: str) -> str:
    """D-11 #1: monitoring resumed after a gap recorded as not monitored."""
    return (
        f"⏸ Monitoring gap {span(start, end, tz)} ({_elapsed(start, end)}). "
        "Recorded as not monitored; no subscriber alerts were sent for it."
    )


def db_down(since: datetime, now: datetime, tz: str) -> str:
    """D-11 #2: the database has been unreachable for more than 5 minutes."""
    return (
        f"🛑 Database unreachable since {when_s(since, now, tz)} (over 5 min). "
        "Detection is paused; the gap will be recorded as not monitored when it is back."
    )


def all_silent_start(since: datetime, count: int, now: datetime, tz: str) -> str:
    """D-12: every active location went silent; both causes named, neither confirmed."""
    return (
        f"⚠️ All {count} active locations silent since {event_prefix(since, now, tz)}: "
        "an area power/ISP outage or a server/network problem. "
        "Subscriber alerts continue as normal."
    )


def all_silent_end(
    since: datetime, first_at: datetime, first_name: str, tz: str, *, escape: bool = True
) -> str:
    """D-12: the first heartbeat after an all-silent period, and how long it lasted."""
    return (
        f"✅ Heartbeats are back (first: {_name(first_name, escape)}, {hms(first_at, tz)}); "
        f"all-silent lasted {_elapsed(since, first_at)}."
    )


def expired(
    kind: str,
    event_at: datetime,
    max_age: timedelta,
    name: str,
    now: datetime,
    tz: str,
    *,
    escape: bool = True,
) -> str:
    """D-08: a subscriber alert reached its maximum age undelivered and is dropped."""
    return (
        f"⌛ {_label(kind)} alert for {_name(name, escape)} "
        f"(event {event_prefix(event_at, now, tz)}) expired undelivered after "
        f"{_duration(max_age)} and will not be sent."
    )


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


def pin_failed(status: int, name: str, *, escape: bool = True) -> str:
    """D-07: a location's bot posted today's chart but cannot pin it.

    ``status`` is the HTTP status of Telegram's refusal. Anything but an integer (a bool is
    not one) from 100 to 599 raises ValueError, so the text only ever shows a short code
    and never Telegram's description.
    """
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise ValueError("a pin failure needs an HTTP status from 100 to 599")
    return (
        f"📌 Can't pin today's chart for {_name(name, escape)} (Telegram: http_{status}). "
        "The chart is still posted and refreshed; pinning is retried every 15 min. "
        "Check that the bot may pin messages in the chat."
    )


def pin_restored(name: str, *, escape: bool = True) -> str:
    """D-07: a location's bot could pin today's chart again after a pin failure."""
    return f"📌 Pinning works again for {_name(name, escape)}."
