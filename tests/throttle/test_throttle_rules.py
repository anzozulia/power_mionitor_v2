"""The login throttle's pure rule and its client IP (SEC-03, INV-21 #2, D-16).

No database and no clock: every instant is an argument. The window is inclusive (the fifth
failure exactly 60 s after the first still starts a cool-down), the cool-down end is
strict (at exactly 5 minutes after the fifth failure the next POST is checked normally),
and instants are compared to the microsecond. The client IP is the rightmost
``X-Forwarded-For`` value, the one Caddy sets; a value a client could choose never wins.
"""

from datetime import UTC, datetime, timedelta

import pytest

from powermon.throttle import rules

T = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
US = timedelta(microseconds=1)


def _at(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


FIVE = [_at(0), _at(15), _at(30), _at(45), _at(60)]


def test_cool_down_starts_at_the_fifth_failure_within_a_minute() -> None:
    # Inclusive window: the fifth failure exactly 60 s after the first still counts.
    assert rules.cool_down_end(FIVE) == _at(60) + timedelta(minutes=5)
    # One microsecond later it does not.
    assert rules.cool_down_end([*FIVE[:4], _at(60) + US]) is None
    # Four failures never start a cool-down, however close together.
    assert rules.cool_down_end([T, T, T, T]) is None
    assert rules.cool_down_end([]) is None


def test_cool_down_does_not_depend_on_the_order_of_the_failures() -> None:
    assert rules.cool_down_end(list(reversed(FIVE))) == _at(60) + timedelta(minutes=5)


def test_blocked_ends_exactly_at_the_cool_down_end() -> None:
    end = _at(60) + timedelta(minutes=5)

    # From the fifth failure on, up to one microsecond before the end.
    assert rules.blocked(FIVE, _at(60))
    assert rules.blocked(FIVE, end - US)
    # Strict end: at the end itself the next POST is checked normally.
    assert not rules.blocked(FIVE, end)
    assert not rules.blocked(FIVE, end + US)
    # No qualifying run, no block.
    assert not rules.blocked(FIVE[:4], _at(60))
    assert not rules.blocked([], T)


def test_later_failure_runs_extend_the_end() -> None:
    first_run = [_at(s) for s in (0, 1, 2, 3, 4)]
    second_run = [_at(s) for s in (200, 201, 202, 203, 204)]

    # The latest qualifying run wins.
    assert rules.cool_down_end(first_run + second_run) == _at(204) + timedelta(minutes=5)
    # A sixth failure inside the window of the four before it closes a later run.
    assert rules.cool_down_end([*first_run, _at(30)]) == _at(30) + timedelta(minutes=5)
    # A failure that closes no run (too far after the four before it) changes nothing.
    assert rules.cool_down_end([*first_run, _at(65)]) == _at(4) + timedelta(minutes=5)


def test_lookback_and_retry_after_match_the_window_and_cool_down() -> None:
    assert rules.LOOKBACK == timedelta(seconds=360)
    assert int(rules.RETRY_AFTER) == rules.COOL_DOWN.total_seconds() == 300
    assert rules.PRUNE_AFTER > rules.LOOKBACK
    assert rules.THROTTLE_MESSAGE == "Too many failed sign-ins. Try again in 5 minutes."
    # The fail-closed bucket for a request with no parseable address.
    assert rules.UNKNOWN_IP == "0.0.0.0"  # noqa: S104


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        # Expected: the rightmost value, the one Caddy appended.
        (
            {"HTTP_X_FORWARDED_FOR": "198.51.100.7, 203.0.113.9", "REMOTE_ADDR": "172.18.0.5"},
            "203.0.113.9",
        ),
        ({"HTTP_X_FORWARDED_FOR": "203.0.113.9", "REMOTE_ADDR": "172.18.0.5"}, "203.0.113.9"),
        # Edge: surrounding spaces are stripped.
        ({"HTTP_X_FORWARDED_FOR": " 203.0.113.9 ", "REMOTE_ADDR": "172.18.0.5"}, "203.0.113.9"),
        # Edge: IPv6 is kept, in its normal form.
        ({"HTTP_X_FORWARDED_FOR": "2001:db8::1"}, "2001:db8::1"),
        ({"HTTP_X_FORWARDED_FOR": "2001:DB8:0:0:0:0:0:1"}, "2001:db8::1"),
        # Edge: an IPv4-mapped IPv6 address counts as its IPv4 address.
        ({"HTTP_X_FORWARDED_FOR": "::ffff:203.0.113.9"}, "203.0.113.9"),
        ({"REMOTE_ADDR": "::ffff:10.0.0.1"}, "10.0.0.1"),
        # No header (local compose): the socket address.
        ({"REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        ({"HTTP_X_FORWARDED_FOR": "", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        # Failure: an unparseable rightmost value falls back to REMOTE_ADDR, never to a
        # value further left, which the client could have chosen.
        ({"HTTP_X_FORWARDED_FOR": "not-an-ip", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        ({"HTTP_X_FORWARDED_FOR": "203.0.113.9, junk", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        ({"HTTP_X_FORWARDED_FOR": "203.0.113.9,", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        ({"HTTP_X_FORWARDED_FOR": "203.0.113.9:443", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        ({"HTTP_X_FORWARDED_FOR": "fe80::1%eth0", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        # Failure: nothing parses, so every such request shares one bucket (fail closed).
        ({"HTTP_X_FORWARDED_FOR": "junk", "REMOTE_ADDR": "junk"}, rules.UNKNOWN_IP),
        ({"REMOTE_ADDR": None}, rules.UNKNOWN_IP),
        ({}, rules.UNKNOWN_IP),
    ],
    ids=[
        "rightmost-of-two",
        "single",
        "stripped",
        "ipv6",
        "ipv6-normalised",
        "ipv4-mapped-forwarded",
        "ipv4-mapped-remote",
        "no-header",
        "empty-header",
        "unparseable-header",
        "unparseable-rightmost",
        "trailing-comma",
        "with-port",
        "scoped-ipv6",
        "nothing-parses",
        "remote-not-a-string",
        "empty-meta",
    ],
)
def test_client_ip_rightmost_forwarded_value(meta: dict[str, object], expected: str) -> None:
    assert rules.client_ip(meta) == expected
