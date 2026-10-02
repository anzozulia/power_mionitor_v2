"""The pure OFF timing rule (MON-02; K-2 boundary, K-4 router grace, D-14 fresh window).

No database: each test builds Snapshot/Anchors values with aware UTC datetimes and asks
the rules module. Defaults follow docs/v1-lessons.md section 1: period 60 s, grace 30 s,
router-reconnect grace off.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from powermon.engine import rules

NO_ANCHORS: dict[str, Any] = {"detection_resumed_at": None}


def _at(hour: int, minute: int, second: int = 0, microsecond: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, microsecond, tzinfo=UTC)


def _snap(last: datetime, on_since: datetime, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "location_id": 1,
        "state_version": 7,
        "last_heartbeat_at": last,
        "on_since": on_since,
        "window_start_at": None,
        "period_s": 60,
        "grace_s": 30,
        "router_grace": False,
        **overrides,
    }
    return rules.Snapshot(**fields)


def _decide(snap: Any, now: datetime, **anchors: Any) -> Any:
    return rules.decide(snap, rules.Anchors(**{**NO_ANCHORS, **anchors}), now)


# Router-reconnect grace (K-4, D-10): on at 12:00:00, period 60 s, grace 30 s


def test_K4_last_heartbeat_1204_no_off_before_0830() -> None:
    # 240 s after on: within the 300 s window, so 90 s + 180 s = 270 s.
    snap = _snap(_at(12, 4), _at(12, 0), router_grace=True)

    assert rules.effective_timeout(snap) == timedelta(seconds=270)
    assert _decide(snap, _at(12, 8, 30)).off is False
    decision = _decide(snap, _at(12, 8, 31))
    assert decision.off is True
    assert decision.outage_start == _at(12, 4)
    assert decision.was_on == timedelta(minutes=4)


def test_K4_last_heartbeat_1205_30_off_after_0700() -> None:
    # 330 s after on: outside the window, so the plain 90 s applies.
    snap = _snap(_at(12, 5, 30), _at(12, 0), router_grace=True)

    assert rules.effective_timeout(snap) == timedelta(seconds=90)
    assert _decide(snap, _at(12, 7)).off is False
    decision = _decide(snap, _at(12, 7, 1))
    assert decision.off is True
    assert decision.outage_start == _at(12, 5, 30)


def test_K4_last_heartbeat_exactly_300s_keeps_grace() -> None:
    # The window is inclusive: exactly 300 s after on still gets the 180 s.
    snap = _snap(_at(12, 5), _at(12, 0), router_grace=True)

    assert rules.effective_timeout(snap) == timedelta(seconds=270)
    assert _decide(snap, _at(12, 9, 30)).off is False
    assert _decide(snap, _at(12, 9, 31)).off is True


def test_router_grace_off_ignores_the_reconnect_window() -> None:
    snap = _snap(_at(12, 4), _at(12, 0), router_grace=False)

    assert rules.effective_timeout(snap) == timedelta(seconds=90)
    assert _decide(snap, _at(12, 5, 31)).off is True


# The strict timeout (K-2, MON-02)


def test_K2_off_is_strictly_after_period_plus_grace() -> None:
    snap = _snap(_at(10, 5), _at(10, 0))

    # At exactly 10:06:30 the location is still on: the rule is ">", not ">=".
    at_boundary = _decide(snap, _at(10, 6, 30))
    assert at_boundary == rules.Decision(off=False)
    assert (at_boundary.outage_start, at_boundary.was_on) == (None, None)

    decision = _decide(snap, _at(10, 6, 30, microsecond=1))
    assert decision.off is True
    # The outage is backdated to the last heartbeat; "was ON for" = last - on.
    assert decision.outage_start == _at(10, 5)
    assert decision.was_on == timedelta(minutes=5)


def test_effective_timeout_defaults() -> None:
    snap = _snap(_at(10, 5), _at(10, 0))

    assert rules.effective_timeout(snap) == timedelta(seconds=90)
    assert rules.effective_timeout(_snap(_at(10, 5), _at(10, 0), period_s=10, grace_s=10)) == (
        timedelta(seconds=20)
    )


def test_longest_timeout_is_period_plus_grace() -> None:
    assert rules.longest_timeout(60, 30, False) == timedelta(seconds=90)
    assert rules.longest_timeout(10, 10, False) == timedelta(seconds=20)


def test_longest_timeout_adds_the_router_grace_when_it_is_on() -> None:
    assert rules.longest_timeout(60, 30, True) == timedelta(seconds=90) + rules.ROUTER_GRACE
    # It is what effective_timeout gives while the reconnect window applies...
    in_window = _snap(_at(12, 4), _at(12, 0), router_grace=True)
    assert rules.effective_timeout(in_window) == rules.longest_timeout(60, 30, True)


def test_longest_timeout_is_never_shorter_than_the_effective_timeout() -> None:
    # ...and more than it once the window has passed: a final chart edit that waits for
    # it never comes before an OFF that detection may still record (INV-03).
    late = _snap(_at(12, 5, 30), _at(12, 0), router_grace=True)
    assert rules.effective_timeout(late) == timedelta(seconds=90)
    assert rules.longest_timeout(60, 30, True) > rules.effective_timeout(late)


def test_was_on_is_zero_when_on_since_equals_last_heartbeat() -> None:
    # Only the first heartbeat ever arrived.
    snap = _snap(_at(10, 0), _at(10, 0))

    decision = _decide(snap, _at(10, 1, 31))

    assert decision.off is True
    assert decision.outage_start == _at(10, 0)
    assert decision.was_on == timedelta(0)


def test_decide_rejects_naive_now() -> None:
    snap = _snap(_at(10, 5), _at(10, 0))

    with pytest.raises(TypeError):
        _decide(snap, datetime(2026, 10, 1, 10, 7))  # noqa: DTZ001


# The detection window anchor (D-14 fresh window, INV-04/INV-11 semantics)


def test_fresh_window_after_worker_start() -> None:
    # The worker started at 10:06:00, after the last heartbeat at 10:05:00.
    snap = _snap(_at(10, 5), _at(10, 0))
    resumed = {"detection_resumed_at": _at(10, 6)}

    assert _decide(snap, _at(10, 7, 30), **resumed).off is False
    decision = _decide(snap, _at(10, 7, 31), **resumed)
    assert decision.off is True
    # The outage starts at the worker start; "was ON for" keeps the literal rule.
    assert decision.outage_start == _at(10, 6)
    assert decision.was_on == timedelta(minutes=5)


def test_anchor_ignores_missing_anchors() -> None:
    snap = _snap(_at(10, 5), _at(10, 0))

    assert rules.anchor(snap, rules.Anchors(detection_resumed_at=None)) == _at(10, 5)
    assert rules.Anchors(detection_resumed_at=None).web_started_at is None


def test_anchor_is_the_latest_of_all_anchors() -> None:
    snap = _snap(_at(10, 5), _at(10, 0), window_start_at=_at(10, 6))
    anchors = rules.Anchors(detection_resumed_at=_at(10, 7), web_started_at=_at(10, 8))

    assert rules.anchor(snap, anchors) == _at(10, 8)
    # Anchors older than the last heartbeat never pull the window back.
    old = rules.Anchors(detection_resumed_at=_at(9, 0), web_started_at=_at(9, 30))
    assert rules.anchor(_snap(_at(10, 5), _at(10, 0)), old) == _at(10, 5)
    assert rules.anchor(snap, old) == _at(10, 6)


def test_rule_values_are_immutable() -> None:
    snap = _snap(_at(10, 5), _at(10, 0))

    with pytest.raises(AttributeError):
        snap.last_heartbeat_at = _at(11, 0)
    assert (rules.ROUTER_GRACE, rules.ROUTER_WINDOW) == (
        timedelta(seconds=180),
        timedelta(seconds=300),
    )


# The timeline state for a location status (shared by the heartbeat and detection gates)


@pytest.mark.parametrize(
    ("status", "maintenance", "expected"),
    [
        ("waiting", False, None),
        ("waiting", True, None),
        ("on", False, "on"),
        ("on", True, "not_monitored"),
        ("off", False, "off"),
        ("off", True, "not_monitored"),
    ],
)
def test_desired_open_state(status: str, maintenance: bool, expected: str | None) -> None:
    assert rules.desired_open_state(status, maintenance) == expected


@pytest.mark.parametrize("status", ["bogus", "", "ON", "not_monitored"])
def test_desired_open_state_rejects_unknown_status(status: str) -> None:
    with pytest.raises(ValueError, match="unknown location status"):
        rules.desired_open_state(status, False)
