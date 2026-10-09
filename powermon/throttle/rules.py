"""The login throttle's rule and the client IP it counts by (SEC-03, D-16, UI-D13).

Pure: no database, no clock, no Django. Every time is an argument, and the store
(``powermon.throttle.store``) feeds in the failures it read.

The rule: five failed sign-ins within 60 s from one client IP (an IPv6 client: its /64)
start a 5-minute cool-down, counted from the fifth failure. During it every sign-in POST
from that IP answers 429, even one with the right password. The window is inclusive (the
fifth failure exactly 60 s after the first still counts) and the cool-down end is strict
(at exactly 5 minutes after the fifth failure the next POST is checked normally).
Throttled POSTs are never recorded as failures, so an attacker cannot extend a cool-down
by trying again during it.
"""

import ipaddress
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

# Failed sign-ins within WINDOW that start a cool-down.
MAX_FAILURES = 5
WINDOW = timedelta(seconds=60)
COOL_DOWN = timedelta(minutes=5)
# The store reads only failures newer than now - LOOKBACK. A failure older than that can
# no longer take part in an active cool-down: the run it closes ended by WINDOW after it,
# and that run's cool-down ended COOL_DOWN after that.
LOOKBACK = WINDOW + COOL_DOWN
# Rows older than this are deleted on each insert, which keeps the table small.
PRUNE_AFTER = timedelta(hours=1)

# The 429 page's only callout (UI-SPEC copywriting contract, verbatim).
THROTTLE_MESSAGE = "Too many failed sign-ins. Try again in 5 minutes."
# The whole cool-down in seconds, a fixed value like the copy (RESEARCH Open Question 2).
RETRY_AFTER = "300"

# Every source that cannot be parsed shares this one bucket: fail closed. A bucket name,
# not a bind address (S104).
UNKNOWN_IP = "0.0.0.0"  # noqa: S104
# An IPv6 client counts by its /64, the usual allocation of one subscriber line, so a
# client rotating addresses inside its /64 stays in one bucket (F-14).
IPV6_BUCKET_PREFIX = 64


def cool_down_end(failures: Sequence[datetime]) -> datetime | None:
    """When the latest cool-down ends, or None if no MAX_FAILURES failures fall in a WINDOW.

    Each failure that is the MAX_FAILURES-th within WINDOW (inclusive: the span from the
    first to the last of the run may equal WINDOW) starts a cool-down that ends COOL_DOWN
    after it. The latest end wins. ``failures`` are compared exactly, to the microsecond.
    """
    ordered = sorted(failures)
    ends = [
        ordered[i] + COOL_DOWN
        for i in range(MAX_FAILURES - 1, len(ordered))
        if ordered[i] - ordered[i - (MAX_FAILURES - 1)] <= WINDOW
    ]
    return max(ends) if ends else None


def blocked(failures: Sequence[datetime], now: datetime) -> bool:
    """True while a cool-down runs at ``now``; at its exact end it is over (strict ``<``)."""
    end = cool_down_end(failures)
    return end is not None and now < end


def _parse(value: object) -> str | None:
    """``value`` as a normalised IP address string, or None if it is not one.

    An IPv4-mapped IPv6 address (``::ffff:203.0.113.9``) becomes its IPv4 form, so one
    client gets one bucket whichever way its address is written. A scoped IPv6 address
    (``fe80::1%eth0``) is refused: no client reaches the app with one, and the database's
    inet type does not take the scope. Any other IPv6 address becomes the network address
    of its /64 (``2001:db8::1`` and ``2001:db8::ffff:1`` both give ``2001:db8::``), which
    is still a valid inet value.
    """
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return None
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.IPv6Network(f"{address}/{IPV6_BUCKET_PREFIX}", strict=False)
        return str(network.network_address)
    return str(address)


def client_ip(meta: Mapping[str, object]) -> str:
    """The client IP the throttle counts by: the rightmost ``X-Forwarded-For`` value.

    In production the web app is reachable only through the reverse proxy (Caddy, or the
    host nginx on a shared VPS; README section 17). The proxy ignores the
    ``X-Forwarded-For`` a client sends and sets the address it saw as the last value, so
    the rightmost value is the one a client cannot choose. An IPv6 address counts by its
    /64 (``_parse``). Without the header (local compose, tests) the socket's
    ``REMOTE_ADDR`` is used. A value that is not an IP address falls through to the next
    source; if nothing parses, the request counts under UNKNOWN_IP.
    """
    forwarded = meta.get("HTTP_X_FORWARDED_FOR")
    if isinstance(forwarded, str) and forwarded.strip():
        parsed = _parse(forwarded.rsplit(",", 1)[-1])
        if parsed is not None:
            return parsed
    return _parse(meta.get("REMOTE_ADDR")) or UNKNOWN_IP
