"""The alert outbox: one durable row per alert, written in the transition's transaction.

KD2 and D-14: alerts leave only through ``outbox_message``, which the worker relay drains
(01-11). The row holds integer durations only; the text is rendered at send time, and the
bot token and chat are read from the location then (T-01-45).

C1 (wave 3 audit): the worker's claim names its lease session, and the claim succeeds
only while that session holds the worker lock, so a worker that lost the lock claims
nothing even before it notices.

WR-01 (code review): the relay's reset of a row it claimed names that claim's attempt
count (and, fenced, its lease session), so it never moves another worker's later claim.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.conf import settings
from django.db import IntegrityError, connection, transaction

from powermon import config
from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.worker.lease import Lease

EVENT_AT = datetime(2026, 10, 1, 10, 5, tzinfo=UTC)
RECORDED_AT = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)


def _rows() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.order_by("id"))


def _enqueue(location: Any, kind: str = "power_off", **payload: Any) -> OutboxMessage:
    return outbox.enqueue(
        kind,
        location.pk,
        event_at=EVENT_AT,
        recorded_at=RECORDED_AT,
        payload=payload or {"was_on_us": 300_000_000},
    )


@pytest.mark.django_db
def test_enqueue_writes_a_pending_subscriber_row(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    created = _enqueue(location)

    [row] = _rows()
    assert row.pk == created.pk
    assert (row.channel, row.location_id, row.kind) == ("subscriber", location.pk, "power_off")
    assert (row.event_at, row.recorded_at) == (EVENT_AT, RECORDED_AT)
    assert row.payload == {"was_on_us": 300_000_000}
    assert (row.status, row.attempts, row.last_error, row.sent_at) == ("pending", 0, "", None)
    # Due at once; it expires ALERT_MAX_AGE_HOURS after it was recorded (ALRT-03, D-07),
    # 6 h under the default config.
    assert row.next_attempt_at == RECORDED_AT
    assert settings.CFG.alert_max_age_hours == config.DEFAULT_ALERT_MAX_AGE_HOURS == 6
    assert row.expires_at == RECORDED_AT + timedelta(hours=6)
    assert (outbox.KIND_POWER_OFF, outbox.KIND_POWER_ON) == ("power_off", "power_on")


@pytest.mark.django_db
def test_enqueue_rolls_back_with_the_callers_transaction(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()

    # The row joins the caller's transaction: if the transition fails, no alert remains.
    with pytest.raises(RuntimeError, match="transition failed"), transaction.atomic():
        _enqueue(location, "power_on", was_off_us=3_300_000_000)
        raise RuntimeError("transition failed")

    assert _rows() == []


@pytest.mark.django_db
def test_enqueue_rejects_an_unknown_kind(location_factory: Callable[..., Any]) -> None:
    location = location_factory()

    with pytest.raises(ValueError, match="unknown alert kind"):
        _enqueue(location, "power_flicker")

    assert _rows() == []


@pytest.mark.django_db
@pytest.mark.parametrize("value", [300.0, True, "300000000", None], ids=repr)
def test_enqueue_rejects_a_payload_that_is_not_integer_durations(
    location_factory: Callable[..., Any], value: Any
) -> None:
    location = location_factory()

    with pytest.raises(TypeError, match="integer"):
        _enqueue(location, was_on_us=value)

    assert _rows() == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [("status", "lost", "outbox_status_valid"), ("channel", "email", "outbox_channel_valid")],
)
def test_db_rejects_unknown_status_and_channel(
    location_factory: Callable[..., Any], field: str, value: str, constraint: str
) -> None:
    location = location_factory()
    fields: dict[str, Any] = {
        "channel": "subscriber",
        "location": location,
        "kind": "power_off",
        "event_at": EVENT_AT,
        "recorded_at": RECORDED_AT,
        "next_attempt_at": RECORDED_AT,
        "expires_at": RECORDED_AT + timedelta(hours=6),
        field: value,
    }

    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        OutboxMessage.objects.create(**fields)


@pytest.mark.django_db
def test_open_rows_index_is_partial() -> None:
    # The relay's head-of-line query reads only pending and sending rows (01-11).
    with connection.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'outbox_open_idx'")
        (indexdef,) = cur.fetchone()

    assert "(channel, location_id, id)" in indexdef
    assert "WHERE ((status)::text = ANY" in indexdef
    assert "'pending'" in indexdef
    assert "'sending'" in indexdef


# C1 (wave 3 audit): the worker claims a row only while its lease session holds the lock


def _claim_state(row: OutboxMessage) -> tuple[str, int]:
    row.refresh_from_db()
    return row.status, row.attempts


def _my_backend_pid() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        (pid,) = cur.fetchone()
    return int(pid)


def _end_session(pid: int) -> None:
    """End one backend and wait until it is gone, so its locks are released."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
        assert cur.fetchone() == (True,)


@pytest.mark.django_db(transaction=True)
def test_C1_a_fenced_claim_needs_the_lease_session_to_hold_the_lock(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    row = _enqueue(location)
    holder = Lease(connection.settings_dict)
    standby = Lease(connection.settings_dict)
    try:
        assert holder.ensure_held().state == "held"
        assert standby.ensure_held().state == "standby"
        assert holder.pid is not None
        assert standby.pid is not None

        # A session that does not hold the worker lock claims nothing: another worker's
        # lease session, or this Django session itself.
        assert outbox.claim(row.pk, lease_pid=standby.pid) is False
        assert outbox.claim(row.pk, lease_pid=_my_backend_pid()) is False
        assert _claim_state(row) == ("pending", 0)

        # The holder's lease session claims the row, once.
        assert outbox.claim(row.pk, lease_pid=holder.pid) is True
        assert _claim_state(row) == ("sending", 1)
        assert outbox.claim(row.pk, lease_pid=holder.pid) is False

        # Once the holder's session is gone, its pid claims nothing more, even before the
        # next worker took the lock.
        later = _enqueue(location)
        lost_pid = holder.pid
        _end_session(lost_pid)
        assert outbox.claim(later.pk, lease_pid=lost_pid) is False
        assert _claim_state(later) == ("pending", 0)
        assert standby.ensure_held().state == "held"
        assert outbox.claim(later.pk, lease_pid=standby.pid) is True
    finally:
        holder.close()
        standby.close()

    # Without a lease pid (a direct call, as in the relay tests) the claim is unfenced.
    third = _enqueue(location)
    assert outbox.claim(third.pk) is True


# WR-01 (code review): the relay's reset of its own claim never reaches another claim


@pytest.mark.django_db(transaction=True)
def test_WR01_a_relay_reset_moves_only_the_attempt_of_its_claim(
    location_factory: Callable[..., Any],
) -> None:
    row = _enqueue(location_factory())
    due = RECORDED_AT + timedelta(seconds=5)
    holder = Lease(connection.settings_dict)
    standby = Lease(connection.settings_dict)
    try:
        assert holder.ensure_held().state == "held"
        assert standby.ensure_held().state == "standby"
        assert outbox.claim(row.pk, lease_pid=holder.pid) is True

        # Another attempt count, or a session that does not hold the lock: nothing moves.
        assert outbox.mark_retry(row.pk, due, "x", attempts=2) is False
        assert outbox.mark_retry(row.pk, due, "x", attempts=2, lease_pid=holder.pid) is False
        assert outbox.mark_retry(row.pk, due, "x", attempts=1, lease_pid=standby.pid) is False
        assert outbox.mark_retry(row.pk, due, "x", lease_pid=holder.pid) is False
        assert _claim_state(row) == ("sending", 1)

        # The claim's own attempt, by the holder's session: back to pending.
        assert outbox.mark_retry(row.pk, due, "x", attempts=1, lease_pid=holder.pid) is True
        assert (_claim_state(row), row.next_attempt_at, row.last_error) == (
            ("pending", 1),
            due,
            "x",
        )
    finally:
        holder.close()
        standby.close()

    # Without a lease session only the attempt count is checked.
    assert outbox.claim(row.pk) is True
    assert outbox.mark_retry(row.pk, due, "y", attempts=1) is False
    assert outbox.mark_retry(row.pk, due, "y", attempts=2) is True
    assert _claim_state(row) == ("pending", 2)


# D-08 / D-12: make a location's held subscriber alerts due at once


def _held(location: Any, until: datetime, *, status: str = "pending") -> OutboxMessage:
    """A subscriber row of ``location`` waiting until ``until`` (a backoff), in ``status``."""
    row = _enqueue(location)
    OutboxMessage.objects.filter(pk=row.pk).update(next_attempt_at=until, status=status)
    return row


def _due_at(row: OutboxMessage) -> datetime:
    return OutboxMessage.objects.get(pk=row.pk).next_attempt_at


@pytest.mark.django_db
def test_make_due_moves_only_the_locations_held_pending_subscriber_rows(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    other = location_factory()
    now = RECORDED_AT + timedelta(minutes=5)
    later = now + timedelta(minutes=10)
    held = [_held(location, later), _held(location, later)]
    already_due = _held(location, now - timedelta(seconds=1))
    at_now = _held(location, now)
    sending = _held(location, later, status="sending")
    sent = _held(location, later, status="sent")
    other_location = _held(other, later)
    with transaction.atomic():
        notice = outbox.enqueue_ops(
            outbox.KIND_OPS_GAP,
            payload={"start_us": 1, "end_us": 2},
            recorded_at=later,
            location_id=location.pk,
        )

    assert outbox.make_due(location.pk, now) == 2

    assert [_due_at(row) for row in held] == [now, now]
    # Rows already due, not pending, of another location or of the ops queue: unchanged.
    assert _due_at(already_due) == now - timedelta(seconds=1)
    assert _due_at(at_now) == now
    assert [_due_at(row) for row in (sending, sent, other_location, notice)] == [later] * 4
    # Nothing left to move: a second call changes no row.
    assert outbox.make_due(location.pk, now) == 0


@pytest.mark.django_db
def test_make_due_refuses_a_naive_time(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    later = RECORDED_AT + timedelta(minutes=15)
    row = _held(location, later)

    with pytest.raises(ValueError, match="naive"):
        outbox.make_due(location.pk, datetime(2026, 10, 1, 10, 10))  # noqa: DTZ001

    assert _due_at(row) == later
