"""The engine's pure rules: the one home of the state-to-timeline mapping (INV-03).

PURE: no Django import and no clock read. Every function takes its inputs, including
``now``, as arguments, so the detector, the heartbeat gate and later phases share one
definition and the tests need no database.
"""

STATUSES_WITH_POWER_STATE = ("on", "off")


def desired_open_state(status: str, maintenance: bool) -> str | None:
    """The timeline state a location in ``status`` should have open.

    "waiting" has no interval (no data before the first heartbeat). "on" and "off" are
    stored as themselves, or as "not_monitored" while the location is in maintenance
    (LOC-08: the chart shows maintenance as not monitored, never as an outage).
    """
    if status == "waiting":
        return None
    if status not in STATUSES_WITH_POWER_STATE:
        raise ValueError(f"unknown location status: {status!r}")
    return "not_monitored" if maintenance else status
