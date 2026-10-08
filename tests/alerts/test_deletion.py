"""A removed outage's alerts are deleted from the channel (261006-qv7, DATA-02 amended).

The removal (``history.remove_outage``) writes delete requests on the outage's sent alerts
in its own transaction and calls nothing (KD2, KD3). The worker's I/O pass carries them
out after its alerts and its ops notice: at most one ``deleteMessage`` per pass, each
location's oldest request first (so OFF before ON), in the chat stored with the alert and
with the location's current bot (D1, D4). A pass that made a delete call makes no chart
call (INV-14: one non-alert call per pass).

Outcomes (D6): ok and "message to delete not found" settle the request; a 429 holds the
delete and the bot; a 5xx or an unsent request holds both for 30 s; an ambiguous answer is
retried after 30 s (a delete is idempotent; INV-16's at-most-once rule is about sends); a
refusal, or a request older than Telegram's 48 h limit, settles it with one WARNING and,
on the OFF, cancels the rest of the removal and sends the ON alert the removal dropped,
unless a later alert went out, the chat changed or it expired (261008-vdk, F-01), so the
channel is never left showing only "power off" (owner default 3). No outcome
touches ``chat_key``, the delivery incident, an ops notice or the row's status.

Migration 0012 is expand-only (D10): the previous release's inserts still work on it.

Every test that runs ``io_loop.run_iteration`` is ``django_db(transaction=True)``: the
pass calls ``close_old_connections()``. Time comes only from the ``FakeClock``; Telegram
is faked at the HTTP boundary (``fake_telegram``).
"""

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    FakeClock,
    FakeTelegram,
)
from django.db import IntegrityError, OperationalError, connection, transaction

from powermon.alerts import ops, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart import lifecycle
from powermon.chart.models import ChartMessage
from powermon.engine import history, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.telegram.client import SendResult
from powermon.worker import detection, io_loop

DB = pytest.mark.django_db(transaction=True)
TOKEN_A = DEFAULT_BOT_TOKEN
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
KYIV = "Europe/Kyiv"
US = timedelta(microseconds=1)
RELAY_LOGGER = io_loop.__name__
BOTS = {TOKEN_A: "A", TOKEN_B: "B", OPS_BOT_TOKEN: "ops"}


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


# A false outage 09:00-09:10; its alerts were sent at once; the admin removes it at 09:20.
OUTAGE = _at(9, 0)
RESTORE = _at(9, 10)
OFF_SENT = _at(9, 1, 32)
ON_SENT = _at(9, 10, 1)
REMOVED = _at(9, 20)
GONE = {"ok": False, "error_code": 400, "description": "Bad Request: message to delete not found"}
CANNOT = {"ok": False, "error_code": 400, "description": "Bad Request: message can't be deleted"}


def _no_anchors() -> None:
    """The process anchors stay out of the way: only the location's own window counts."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _requests(fake: FakeTelegram) -> list[tuple[str, str]]:
    """Every request the fake received as (bot label, method), in order."""
    out = []
    for call in fake.calls:
        path = call.request.url.split("/bot", 1)[1]
        token, method = path.rsplit("/", 1)
        out.append((BOTS.get(token, "?"), method))
    return out


def _deletes(fake: FakeTelegram) -> list[tuple[str, int, int]]:
    """Every accepted deleteMessage as (bot label, chat id, message id), in order."""
    return [
        (BOTS[c.token], c.fields["chat_id"], c.fields["message_id"])
        for c in fake.chart_calls
        if c.method == "deleteMessage"
    ]


def _result(row: OutboxMessage) -> str | None:
    return OutboxMessage.objects.get(pk=row.pk).delete_result


def _alert(
    location: Any,
    kind: str,
    *,
    message_id: int,
    sent_at: datetime,
    requested: datetime | None = REMOVED,
    chat_id: int = DEFAULT_CHAT_ID,
) -> OutboxMessage:
    """A sent alert of the 09:00-09:10 outage with its stored ids, maybe delete-requested."""
    off = kind == outbox.KIND_POWER_OFF
    event_at = OUTAGE if off else RESTORE
    payload = {"was_on_us": 3_600_000_000} if off else {"was_off_us": (RESTORE - OUTAGE) // US}
    recorded_at = event_at + timedelta(seconds=91) if off else event_at
    return OutboxMessage.objects.create(
        channel=outbox.CHANNEL_SUBSCRIBER,
        location=location,
        kind=kind,
        event_at=event_at,
        recorded_at=recorded_at,
        payload=payload,
        status="sent",
        attempts=1,
        next_attempt_at=recorded_at,
        expires_at=recorded_at + timedelta(hours=72),
        sent_at=sent_at,
        tg_chat_id=chat_id,
        tg_message_id=message_id,
        delete_requested_at=requested,
    )


def _pair(location: Any, **kw: Any) -> tuple[OutboxMessage, OutboxMessage]:
    """The outage's OFF (message 1) and ON (message 2), both requested for deletion."""
    off = _alert(location, outbox.KIND_POWER_OFF, message_id=1, sent_at=OFF_SENT, **kw)
    on = _alert(location, outbox.KIND_POWER_ON, message_id=2, sent_at=ON_SENT, **kw)
    return off, on


def _untouched(row: OutboxMessage) -> tuple[Any, ...]:
    """What a delete outcome must never change on its row (D6)."""
    stored = OutboxMessage.objects.get(pk=row.pk)
    return stored.status, stored.attempts, stored.next_attempt_at, stored.last_error


def _delete_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == RELAY_LOGGER and r.getMessage().startswith("alert delete")
    ]


def monitor(location: Any, since: datetime) -> None:
    """On since ``since``: live state "on" and one open on piece (chart_fixtures' helper)."""
    LocationState.objects.filter(location=location).update(
        status="on", on_since=since, last_heartbeat_at=since
    )
    PowerInterval.objects.create(location=location, state="on", start_at=since, end_at=None)


def _my_backend_pid() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return int(cur.fetchone()[0])


# The tracer: relay send -> stored ids -> removal -> request -> I/O pass -> deleteMessage


@DB
def test_DATA02_removed_outage_alerts_are_deleted_off_then_on(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _no_anchors()
    location = location_factory()
    fake_telegram.accept(TOKEN_A)
    state = io_loop.RelayState()
    clock = FakeClock(_at(8, 0))
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, OUTAGE) == "plain"
    # A false outage: OFF recorded at 09:01:31 (period 60 s + grace 30 s), sent at once.
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    clock.set(OFF_SENT)
    assert io_loop.run_iteration(clock, state) is True
    assert transitions.record_heartbeat(location.pk, RESTORE) == "restored"
    clock.set(ON_SENT)
    assert io_loop.run_iteration(clock, state) is True
    off = OutboxMessage.objects.get(kind="power_off")
    on = OutboxMessage.objects.get(kind="power_on")
    assert [(r.status, r.tg_chat_id, r.tg_message_id) for r in (off, on)] == [
        ("sent", DEFAULT_CHAT_ID, 1),
        ("sent", DEFAULT_CHAT_ID, 2),
    ]
    rows, calls = OutboxMessage.objects.count(), len(fake_telegram.calls)

    assert history.remove_outage(location.pk, OUTAGE, now=REMOVED, tz=KYIV) == "removed"

    # The removal itself adds no row and calls nothing (KD2, KD3, INV-07).
    assert (OutboxMessage.objects.count(), len(fake_telegram.calls)) == (rows, calls)
    fake_telegram.accept_chart(TOKEN_A)
    clock.set(REMOVED + timedelta(seconds=1))
    assert io_loop.run_iteration(clock, state) is True
    assert _deletes(fake_telegram) == [("A", DEFAULT_CHAT_ID, 1)]
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is True
    assert _deletes(fake_telegram) == [("A", DEFAULT_CHAT_ID, 1), ("A", DEFAULT_CHAT_ID, 2)]
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is False

    assert (_result(off), _result(on)) == ("deleted", "deleted")
    assert _requests(fake_telegram)[calls:] == [("A", "deleteMessage"), ("A", "deleteMessage")]


# Expected: the stored chat, the current bot, OFF first, one call per pass


@DB
def test_DATA02_delete_uses_the_stored_chat_and_the_current_token(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, on = _pair(location)
    # The admin moved the location to another chat and bot after the alerts went out.
    Location.objects.filter(pk=location.pk).update(bot_token=TOKEN_B, chat_id=CHAT_B)
    fake_telegram.accept_chart(TOKEN_B)
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True
    assert io_loop.run_iteration(clock, state) is True
    assert io_loop.run_iteration(clock, state) is False

    assert _deletes(fake_telegram) == [("B", DEFAULT_CHAT_ID, 1), ("B", DEFAULT_CHAT_ID, 2)]
    assert (_result(off), _result(on)) == ("deleted", "deleted")


@DB
def test_DATA02_one_pass_sends_the_alert_then_the_ops_notice_then_the_delete(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram, ops_settings: Any
) -> None:
    removed = location_factory()
    _alert(removed, outbox.KIND_POWER_OFF, message_id=1, sent_at=OFF_SENT)
    other = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    now = REMOVED + timedelta(seconds=1)
    with transaction.atomic():
        outbox.enqueue(
            outbox.KIND_POWER_OFF,
            other.pk,
            event_at=now - timedelta(seconds=91),
            recorded_at=now,
            payload={"was_on_us": 300_000_000},
        )
        gap = {
            "start_us": ops.instant_us(now - timedelta(seconds=120)),
            "end_us": ops.instant_us(now - timedelta(seconds=60)),
        }
        outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload=gap, recorded_at=now)
    fake_telegram.accept(TOKEN_B)
    fake_telegram.accept(OPS_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_A)

    assert io_loop.run_iteration(FakeClock(now), io_loop.RelayState()) is True

    assert _requests(fake_telegram) == [
        ("B", "sendMessage"),
        ("ops", "sendMessage"),
        ("A", "deleteMessage"),
    ]


@DB
def test_DATA02_a_pass_that_deletes_makes_no_chart_call(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    # Monitored, with no chart for today yet: a chart post is due in every pass.
    monitor(location, _at(8, 0))
    _alert(location, outbox.KIND_POWER_OFF, message_id=1, sent_at=OFF_SENT)
    fake_telegram.accept_chart(TOKEN_A)
    clock = FakeClock(REMOVED + timedelta(seconds=1))
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state, charts=True) is True
    assert _requests(fake_telegram) == [("A", "deleteMessage")]

    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state, charts=True) is True
    assert _requests(fake_telegram) == [("A", "deleteMessage"), ("A", "sendPhoto")]


@DB
def test_DATA02_a_deleted_locations_request_is_still_tried(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    Location.objects.filter(pk=location.pk).update(deleted_at=REMOVED)
    fake_telegram.accept_chart(TOKEN_A)

    assert io_loop.run_iteration(FakeClock(REMOVED), io_loop.RelayState()) is True

    # A delete is not an alert (D8): its stored token still deletes it.
    assert _deletes(fake_telegram) == [("A", DEFAULT_CHAT_ID, 1)]
    assert _result(off) == "deleted"


# Edge: "not found" counts as done; 48 h; waits; the lease; stop


@DB
def test_DATA02_not_found_and_a_repeated_ok_both_count_as_done(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, on = _pair(location)
    # Deleted by hand already, and a delete Telegram answers ok a second time.
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=400, json_body=GONE)
    fake_telegram.accept_chart(TOKEN_A)
    clock = FakeClock(REMOVED)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True
    assert io_loop.run_iteration(clock, state) is True

    assert (_result(off), _result(on)) == ("not_found", "deleted")
    assert io_loop.delete_key(location.pk) not in state.not_before


@DB
def test_DATA02_older_than_48h_is_too_old_with_no_call_and_cancels_its_on(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    off, on = _pair(location)
    fake_telegram.accept_chart(TOKEN_A)
    caplog.set_level(logging.INFO, logger=RELAY_LOGGER)
    # The worker was down: the OFF went out 48 h ago, Telegram's limit.
    clock = FakeClock(OFF_SENT + outbox.DELETE_LIMIT)

    assert io_loop.run_iteration(clock, io_loop.RelayState()) is False

    assert len(fake_telegram.calls) == 0
    assert (_result(off), _result(on)) == ("too_old", "cancelled")
    line = f"alert delete for location {location.pk}: permanent (too_old) outbox {off.pk}"
    assert _delete_lines(caplog) == [(logging.WARNING, line)]
    assert (_untouched(off)[0], _untouched(on)[0]) == ("sent", "sent")


@DB
@pytest.mark.parametrize("hold", ["delete", "bot", "chat"])
def test_DATA02_no_call_while_its_location_bot_or_chat_waits(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram, hold: str
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    fake_telegram.accept_chart(TOKEN_A)
    keys = {
        "delete": io_loop.delete_key(location.pk),
        "bot": io_loop.bot_wide_key(TOKEN_A),
        # The chat stored with the alert, even when the location moved since.
        "chat": io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID),
    }
    Location.objects.filter(pk=location.pk).update(chat_id=CHAT_B)
    clock = FakeClock(REMOVED)
    state = io_loop.RelayState()
    state.not_before[keys[hold]] = REMOVED + timedelta(seconds=10)

    assert io_loop.run_iteration(clock, state) is False
    assert len(fake_telegram.calls) == 0

    clock.advance(seconds=10)
    assert io_loop.run_iteration(clock, state) is True
    assert _result(off) == "deleted"


@DB
def test_DATA02_no_call_while_the_lease_is_stale(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    fake_telegram.accept_chart(TOKEN_A)
    state = io_loop.RelayState()
    # This test's own session holds no worker lock: the lease is gone (C1).
    state.lease_pid = _my_backend_pid()

    assert io_loop.run_iteration(FakeClock(REMOVED), state) is False

    assert len(fake_telegram.calls) == 0
    assert _result(off) is None


@DB
def test_DATA02_no_call_once_stop_is_set(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    fake_telegram.accept_chart(TOKEN_A)
    stop = threading.Event()
    stop.set()

    assert io_loop.run_iteration(FakeClock(REMOVED), io_loop.RelayState(), stop) is False

    assert len(fake_telegram.calls) == 0
    assert _result(off) is None


# Failure: refusals, timeouts, 429, errors


@DB
def test_DATA02_refused_off_delete_cancels_its_on_which_is_never_called(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    off, on = _pair(location)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=400, json_body=CANNOT)
    fake_telegram.accept_chart(TOKEN_A)
    caplog.set_level(logging.INFO, logger=RELAY_LOGGER)
    clock = FakeClock(REMOVED)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is False

    assert fake_telegram.count(TOKEN_A, "deleteMessage") == 1
    assert (_result(off), _result(on)) == ("http_400", "cancelled")
    line = f"alert delete for location {location.pk}: permanent (http_400) outbox {off.pk}"
    assert _delete_lines(caplog) == [(logging.WARNING, line)]


@DB
def test_INV16_delete_read_timeout_is_retried(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", exc=requests.ReadTimeout("timed out"))
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=400, json_body=GONE)
    clock = FakeClock(REMOVED)
    state = io_loop.RelayState()

    # Maybe deleted: a delete is idempotent, so it is tried again after 30 s.
    assert io_loop.run_iteration(clock, state) is True
    assert _result(off) is None
    assert state.not_before == {io_loop.delete_key(location.pk): REMOVED + timedelta(seconds=30)}
    clock.advance(seconds=29)
    assert io_loop.run_iteration(clock, state) is False
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is True

    assert fake_telegram.count(TOKEN_A, "deleteMessage") == 2
    assert _result(off) == "not_found"
    assert _untouched(off) == ("sent", 1, OUTAGE + timedelta(seconds=91), "")


@DB
@pytest.mark.parametrize(
    "answer",
    [{"status": 502}, {"exc": requests.ConnectTimeout("connect timed out")}],
    ids=["5xx", "not_sent"],
)
def test_DATA02_5xx_or_unsent_holds_the_delete_and_the_bot_for_30s(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram, answer: dict[str, Any]
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", **answer)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(REMOVED), state) is True

    until = REMOVED + timedelta(seconds=30)
    assert state.not_before == {
        io_loop.delete_key(location.pk): until,
        io_loop.bot_wide_key(TOKEN_A): until,
    }
    assert _result(off) is None


@DB
def test_DATA02_429_on_a_delete_holds_the_bot_and_the_delete(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, _on = _pair(location)
    limited = {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=429, json_body=limited)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(REMOVED), state) is True

    until = REMOVED + timedelta(seconds=7)
    assert state.not_before == {
        io_loop.delete_key(location.pk): until,
        io_loop.bot_wide_key(TOKEN_A): until,
    }
    assert _result(off) is None


@DB
def test_DATA02_delete_outcomes_never_touch_delivery_or_the_row(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram, ops_settings: Any
) -> None:
    location = location_factory()
    off, on = _pair(location)
    before = (_untouched(off), _untouched(on))
    kicked = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=403, json_body=kicked)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(FakeClock(REMOVED), state) is True

    assert _result(off) == "http_403"
    # No delivery incident, no ops notice, no channel hold: a delete is not an alert.
    assert not OpsIncident.objects.exists()
    assert not OutboxMessage.objects.filter(channel="ops").exists()
    assert state.failing == {}
    assert io_loop.chat_key(TOKEN_A, DEFAULT_CHAT_ID) not in state.not_before
    assert (_untouched(off), _untouched(on)) == before


@DB
def test_DATA02_exception_in_the_step_is_logged_and_the_chart_step_still_runs(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> list[OutboxMessage]:
        raise RuntimeError(f"boom at /bot{TOKEN_A}/deleteMessage")

    charts: list[int] = []

    def chart_step(*args: Any) -> bool:
        charts.append(1)
        return False

    monkeypatch.setattr(outbox, "deletion_heads", broken)
    monkeypatch.setattr(lifecycle, "run_step", chart_step)
    caplog.set_level(logging.DEBUG)

    assert io_loop.run_iteration(FakeClock(REMOVED), io_loop.RelayState(), charts=True) is False

    assert "delete step failed: RuntimeError" in caplog.text
    assert TOKEN_A.split(":", 1)[1] not in caplog.text
    assert charts == [1]
    assert len(fake_telegram.calls) == 0


@DB
def test_DATA02_unwritten_outcome_still_skips_the_chart_and_repeats_the_delete(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    monitor(location, _at(8, 0))
    off = _alert(location, outbox.KIND_POWER_OFF, message_id=1, sent_at=OFF_SENT)
    ok = {"ok": True, "result": True}
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=200, json_body=ok)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=400, json_body=GONE)
    real = outbox.settle_delete
    failures = [OperationalError("server closed the connection unexpectedly")]

    def flaky(*args: Any) -> bool:
        if failures:
            raise failures.pop()
        return real(*args)

    monkeypatch.setattr(outbox, "settle_delete", flaky)
    caplog.set_level(logging.WARNING, logger=RELAY_LOGGER)
    clock = FakeClock(REMOVED)
    state = io_loop.RelayState()

    # Telegram deleted it, but the outcome could not be written: still no chart call.
    assert io_loop.run_iteration(clock, state, charts=True) is True
    assert _requests(fake_telegram) == [("A", "deleteMessage")]
    assert _result(off) is None
    assert "delete step failed: OperationalError" in caplog.text
    assert state.not_before[io_loop.delete_key(location.pk)] == REMOVED + timedelta(seconds=30)

    clock.advance(seconds=30)
    assert io_loop.run_iteration(clock, state, charts=True) is True

    assert _requests(fake_telegram) == [("A", "deleteMessage"), ("A", "deleteMessage")]
    assert _result(off) == "not_found"


@DB
def test_OPS08_delete_logs_carry_no_token(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = location_factory()
    _pair(location)
    url = f"https://api.telegram.org/bot{TOKEN_A}/deleteMessage"
    leaky = {"ok": False, "error_code": 403, "description": f"Forbidden: {url}"}
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=403, json_body=leaky)
    caplog.set_level(logging.DEBUG)

    assert io_loop.run_iteration(FakeClock(REMOVED), io_loop.RelayState()) is True

    secret = TOKEN_A.split(":", 1)[1]
    assert secret not in caplog.text
    assert "Forbidden" not in caplog.text
    assert all(secret not in str(record.args) for record in caplog.records)
    assert f"alert delete for location {location.pk}: permanent (http_403)" in caplog.text


@DB
def test_DATA02_refused_off_delete_sends_the_dropped_on(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _no_anchors()
    location = location_factory()
    # sendMessage: the OFF is accepted, the first ON is refused (403), the next ON accepted.
    fake_telegram.accept(TOKEN_A)
    forbidden = {"ok": False, "error_code": 403, "description": "Forbidden: kicked"}
    fake_telegram.fail(TOKEN_A, status=403, json_body=forbidden)
    fake_telegram.accept(TOKEN_A)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=403, json_body=forbidden)
    state = io_loop.RelayState()
    clock = FakeClock(_at(8, 0))
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, OUTAGE) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    clock.set(OFF_SENT)
    assert io_loop.run_iteration(clock, state) is True
    assert transitions.record_heartbeat(location.pk, RESTORE) == "restored"
    clock.set(ON_SENT)
    assert io_loop.run_iteration(clock, state) is True
    off = OutboxMessage.objects.get(kind="power_off")
    on = OutboxMessage.objects.get(kind="power_on")
    # The ON waits out its channel's 15-min hold after the refusal.
    assert (on.status, on.last_error) == ("pending", "http_403")

    assert history.remove_outage(location.pk, OUTAGE, now=REMOVED, tz=KYIV) == "removed"

    on.refresh_from_db()
    assert (on.status, on.last_error) == ("dropped", history.OUTAGE_REMOVED)
    assert OutboxMessage.objects.get(pk=off.pk).delete_requested_at == REMOVED
    # Nothing goes while the stored chat waits; after the hold the OFF delete is refused.
    clock.set(REMOVED)
    assert io_loop.run_iteration(clock, state) is False
    clock.set(ON_SENT + io_loop.PERMANENT_BACKOFF)
    assert io_loop.run_iteration(clock, state) is True
    assert _result(off) == "http_403"
    on.refresh_from_db()
    assert (on.status, on.next_attempt_at, on.last_error) == ("pending", clock.now(), "")

    # The next pass sends the ON after all: the channel shows both alerts.
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is True

    on.refresh_from_db()
    assert on.status == "sent"
    assert _requests(fake_telegram) == [
        ("A", "sendMessage"),
        ("A", "sendMessage"),
        ("A", "deleteMessage"),
        ("A", "sendMessage"),
    ]


# A refused or too-old OFF delete never sends a stale ON (quick task 261008-vdk, F-01)


def _dropped_on(location: Any, *, expires_at: datetime = RESTORE + timedelta(hours=6)) -> Any:
    """The outage's ON as ``history.remove_outage`` leaves a queued one: dropped, no ids."""
    return OutboxMessage.objects.create(
        channel=outbox.CHANNEL_SUBSCRIBER,
        location=location,
        kind=outbox.KIND_POWER_ON,
        event_at=RESTORE,
        recorded_at=RESTORE,
        payload={"was_off_us": (RESTORE - OUTAGE) // US},
        status="dropped",
        last_error=outbox.OUTAGE_REMOVED,
        attempts=1,
        next_attempt_at=RESTORE,
        expires_at=expires_at,
    )


def _removed_off(location: Any) -> OutboxMessage:
    """The removed outage's sent OFF (message 1), its delete requested at REMOVED."""
    return _alert(location, outbox.KIND_POWER_OFF, message_id=1, sent_at=OFF_SENT)


def _on_state(on: OutboxMessage) -> tuple[str, str]:
    stored = OutboxMessage.objects.get(pk=on.pk)
    return stored.status, stored.last_error


def _stale_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == outbox.__name__]


@DB
@pytest.mark.parametrize("later_status", ["sent", "uncertain", "sending", "pending"])
def test_DATA02_refused_off_delete_keeps_the_on_dropped_after_a_later_alert_went_out(
    location_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
    later_status: str,
) -> None:
    location = location_factory()
    off = _removed_off(location)
    on = _dropped_on(location)
    sent = later_status == "sent"
    # A later outage's OFF, created after the ON, so its id is greater.
    OutboxMessage.objects.create(
        channel=outbox.CHANNEL_SUBSCRIBER,
        location=location,
        kind=outbox.KIND_POWER_OFF,
        event_at=_at(9, 30),
        recorded_at=_at(9, 31, 31),
        payload={"was_on_us": 1},
        status=later_status,
        attempts=1,
        next_attempt_at=_at(9, 31, 31),
        expires_at=_at(9, 31, 31) + timedelta(hours=6),
        sent_at=_at(9, 31, 32) if sent else None,
        tg_chat_id=DEFAULT_CHAT_ID if sent else None,
        tg_message_id=3 if sent else None,
    )
    caplog.set_level(logging.INFO, logger=outbox.__name__)
    now = REMOVED + timedelta(minutes=5)

    outbox.fail_delete(off, "http_403", now)

    assert _result(off) == "http_403"
    if later_status == "pending":
        # A later row still queued is fine: the ON has the lower id and goes first.
        stored = OutboxMessage.objects.get(pk=on.pk)
        assert (stored.status, stored.next_attempt_at, stored.last_error) == ("pending", now, "")
        assert _stale_lines(caplog) == []
    else:
        assert _on_state(on) == ("dropped", outbox.OUTAGE_REMOVED)
        assert _stale_lines(caplog) == [
            (
                logging.INFO,
                f"removed outage's ON alert {on.pk} for location {location.pk} "
                "stays dropped (later_alert)",
            )
        ]


@DB
@pytest.mark.parametrize("change", ["chat", "token"])
def test_DATA02_refused_off_delete_keeps_the_on_dropped_when_the_location_moved_chat(
    location_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
    change: str,
) -> None:
    location = location_factory()
    off = _removed_off(location)
    on = _dropped_on(location)
    if change == "chat":
        Location.objects.filter(pk=location.pk).update(chat_id=CHAT_B)
    else:
        # A new bot in the same chat: the ON still belongs there.
        Location.objects.filter(pk=location.pk).update(bot_token=TOKEN_B)
    caplog.set_level(logging.INFO, logger=outbox.__name__)
    now = REMOVED + timedelta(minutes=5)

    outbox.fail_delete(off, "http_403", now)

    if change == "chat":
        assert _on_state(on) == ("dropped", outbox.OUTAGE_REMOVED)
        assert _stale_lines(caplog) == [
            (
                logging.INFO,
                f"removed outage's ON alert {on.pk} for location {location.pk} "
                "stays dropped (chat_changed)",
            )
        ]
    else:
        stored = OutboxMessage.objects.get(pk=on.pk)
        assert (stored.status, stored.next_attempt_at, stored.last_error) == ("pending", now, "")
        assert _stale_lines(caplog) == []


@DB
@pytest.mark.parametrize("expired", [True, False], ids=["at_now", "one_us_later"])
def test_DATA02_too_old_delete_keeps_an_expired_on_dropped_and_sends_no_ops_notice(
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    ops_settings: Any,
    caplog: pytest.LogCaptureFixture,
    expired: bool,
) -> None:
    location = location_factory()
    off = _removed_off(location)
    expires_at = RESTORE + timedelta(hours=6)
    on = _dropped_on(location, expires_at=expires_at)
    # Expired at exactly expires_at (expire_due's rule); one microsecond earlier it is not.
    now = expires_at if expired else expires_at - US
    caplog.set_level(logging.INFO, logger=outbox.__name__)

    outbox.fail_delete(off, outbox.DELETE_TOO_OLD, now)

    if not expired:
        stored = OutboxMessage.objects.get(pk=on.pk)
        assert (stored.status, stored.next_attempt_at, stored.last_error) == ("pending", now, "")
        assert _stale_lines(caplog) == []
        return
    assert _on_state(on) == ("dropped", outbox.OUTAGE_REMOVED)
    assert _stale_lines(caplog) == [
        (
            logging.INFO,
            f"removed outage's ON alert {on.pk} for location {location.pk} stays dropped (expired)",
        )
    ]
    # Nothing expires into an ops notice, and nothing is sent.
    io_loop.run_iteration(FakeClock(now), io_loop.RelayState())
    assert not OutboxMessage.objects.filter(kind=outbox.KIND_OPS_EXPIRED).exists()
    assert _on_state(on) == ("dropped", outbox.OUTAGE_REMOVED)
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_DATA02_stale_on_without_a_location_keeps_the_on_dropped(
    location_factory: Callable[..., Any],
) -> None:
    # fail_delete never passes such an OFF; with no location there is no chat to match.
    on = _dropped_on(location_factory())
    orphan = OutboxMessage(location_id=None, tg_chat_id=DEFAULT_CHAT_ID)

    assert outbox._stale_on(on, orphan, REMOVED) == "chat_changed"


@DB
def test_DATA02_refused_off_delete_after_a_chat_move_sends_no_stale_on(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _no_anchors()
    location = location_factory()
    forbidden = {"ok": False, "error_code": 403, "description": "Forbidden: kicked"}
    # sendMessage: OFF A accepted, ON A refused, OFF B (to chat B) accepted; delete refused.
    fake_telegram.accept(TOKEN_A)
    fake_telegram.fail(TOKEN_A, status=403, json_body=forbidden)
    fake_telegram.accept(TOKEN_A)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=403, json_body=forbidden)
    state = io_loop.RelayState()
    clock = FakeClock(_at(8, 0))
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, OUTAGE) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    clock.set(OFF_SENT)
    assert io_loop.run_iteration(clock, state) is True
    assert transitions.record_heartbeat(location.pk, RESTORE) == "restored"
    clock.set(ON_SENT)
    assert io_loop.run_iteration(clock, state) is True
    off_a = OutboxMessage.objects.get(kind="power_off")
    on_a = OutboxMessage.objects.get(kind="power_on")
    assert (on_a.status, on_a.last_error) == ("pending", "http_403")

    assert history.remove_outage(location.pk, OUTAGE, now=REMOVED, tz=KYIV) == "removed"
    assert _on_state(on_a) == ("dropped", outbox.OUTAGE_REMOVED)

    # The admin moves the location to chat B.
    data = {name: getattr(location, name) for name in actions.CONFIG_FIELDS}
    saved = actions.update_config(location.pk, data | {"chat_id": CHAT_B, "bot_token": ""}, REMOVED)
    assert saved.channel_changed
    # A new outage: OFF B is queued (the last heartbeat was the restore).
    assert detection.run_cycle(_at(9, 21)) == 1

    # After the old chat's hold: OFF B goes to chat B, then the OFF A delete is refused.
    clock.set(_at(9, 30))
    assert io_loop.run_iteration(clock, state) is True
    assert fake_telegram.sent[-1]["chat_id"] == CHAT_B
    assert _result(off_a) == "http_403"
    assert _on_state(on_a) == ("dropped", outbox.OUTAGE_REMOVED)

    # ON A never reaches chat B.
    clock.set(_at(9, 30, 1))
    assert io_loop.run_iteration(clock, state) is False
    assert _requests(fake_telegram) == [
        ("A", "sendMessage"),
        ("A", "sendMessage"),
        ("A", "sendMessage"),
        ("A", "deleteMessage"),
    ]


# A later alert that is being deleted, or is already gone, does not keep the removed
# outage's ON dropped (quick task 261008-vdk R-2, F-01 follow-up).

LATER_DELETE_STATES = {
    # name: (delete_requested, delete_result, ON sent after all)
    "pending_request": (True, None, True),
    "deleted": (True, outbox.DELETE_DELETED, True),
    "not_found": (True, outbox.DELETE_NOT_FOUND, True),
    "refused": (True, "http_403", False),
    "too_old": (True, outbox.DELETE_TOO_OLD, False),
    "cancelled": (True, outbox.DELETE_CANCELLED, False),
    "no_request": (False, None, False),
}


@DB
@pytest.mark.parametrize("later_delete", list(LATER_DELETE_STATES))
def test_DATA02_refused_off_delete_ignores_a_later_alert_that_is_being_deleted(
    location_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
    later_delete: str,
) -> None:
    requested, result, resent = LATER_DELETE_STATES[later_delete]
    location = location_factory()
    off = _removed_off(location)
    on = _dropped_on(location)
    # A later outage's sent OFF. Its delete comes from another removal, two minutes later:
    # a request made at REMOVED would be cancelled by fail_delete before _stale_on runs.
    OutboxMessage.objects.create(
        channel=outbox.CHANNEL_SUBSCRIBER,
        location=location,
        kind=outbox.KIND_POWER_OFF,
        event_at=_at(9, 30),
        recorded_at=_at(9, 31, 31),
        payload={"was_on_us": 1},
        status="sent",
        attempts=1,
        next_attempt_at=_at(9, 31, 31),
        expires_at=_at(9, 31, 31) + timedelta(hours=6),
        sent_at=_at(9, 31, 32),
        tg_chat_id=DEFAULT_CHAT_ID,
        tg_message_id=3,
        delete_requested_at=REMOVED + timedelta(minutes=2) if requested else None,
        delete_result=result,
    )
    caplog.set_level(logging.INFO, logger=outbox.__name__)
    now = REMOVED + timedelta(minutes=5)

    outbox.fail_delete(off, "http_403", now)

    assert _result(off) == "http_403"
    if resent:
        stored = OutboxMessage.objects.get(pk=on.pk)
        assert (stored.status, stored.next_attempt_at, stored.last_error) == ("pending", now, "")
        assert _stale_lines(caplog) == []
    else:
        assert _on_state(on) == ("dropped", outbox.OUTAGE_REMOVED)
        assert _stale_lines(caplog) == [
            (
                logging.INFO,
                f"removed outage's ON alert {on.pk} for location {location.pk} "
                "stays dropped (later_alert)",
            )
        ]


@DB
def test_DATA02_both_outages_removed_first_delete_refused_sends_the_dropped_on(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    _no_anchors()
    location = location_factory()
    forbidden = {"ok": False, "error_code": 403, "description": "Forbidden: kicked"}
    # sendMessage: OFF A accepted, ON A refused (403), then every later send accepted.
    fake_telegram.accept(TOKEN_A)
    fake_telegram.fail(TOKEN_A, status=403, json_body=forbidden)
    fake_telegram.accept(TOKEN_A)
    # deleteMessage: OFF A's first call 500, its second refused, then the rest accepted.
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=500)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=400, json_body=CANNOT)
    fake_telegram.accept_chart(TOKEN_A)
    state = io_loop.RelayState()
    clock = FakeClock(_at(8, 0))
    assert transitions.record_heartbeat(location.pk, _at(8, 0)) == "started"
    assert transitions.record_heartbeat(location.pk, OUTAGE) == "plain"
    assert detection.run_cycle(_at(9, 1, 31)) == 1
    clock.set(OFF_SENT)
    assert io_loop.run_iteration(clock, state) is True
    assert transitions.record_heartbeat(location.pk, RESTORE) == "restored"
    clock.set(ON_SENT)
    assert io_loop.run_iteration(clock, state) is True
    off_a = OutboxMessage.objects.get(kind="power_off")
    on_a = OutboxMessage.objects.get(kind="power_on")
    assert (on_a.status, on_a.last_error) == ("pending", "http_403")
    assert transitions.record_heartbeat(location.pk, REMOVED) == "plain"

    # Outage A is removed: ON A is dropped, OFF A's delete is requested.
    assert history.remove_outage(location.pk, OUTAGE, now=REMOVED, tz=KYIV) == "removed"
    assert _on_state(on_a) == ("dropped", outbox.OUTAGE_REMOVED)
    # Outage B starts at the last heartbeat (09:20).
    assert detection.run_cycle(_at(9, 21, 31)) == 1
    off_b = OutboxMessage.objects.get(kind="power_off", event_at=REMOVED)

    # After the channel's hold: OFF B goes out, then OFF A's first delete answers 500
    # (transient: the delete is held for 30 s).
    clock.set(ON_SENT + io_loop.PERMANENT_BACKOFF)
    assert io_loop.run_iteration(clock, state) is True
    assert OutboxMessage.objects.get(pk=off_b.pk).status == "sent"
    assert _result(off_a) is None

    # B ends and is removed too, at another moment than A (its ON B is dropped, OFF B's
    # delete is requested).
    assert transitions.record_heartbeat(location.pk, _at(9, 26)) == "restored"
    on_b = OutboxMessage.objects.get(kind="power_on", event_at=_at(9, 26))
    assert history.remove_outage(location.pk, REMOVED, now=_at(9, 27), tz=KYIV) == "removed"
    assert _on_state(on_b) == ("dropped", outbox.OUTAGE_REMOVED)
    assert OutboxMessage.objects.get(pk=off_b.pk).delete_requested_at == _at(9, 27)

    # After the hold OFF A's delete is refused: OFF B is being deleted, so ON A goes back
    # to pending.
    clock.set(_at(9, 27, 30))
    assert io_loop.run_iteration(clock, state) is True
    assert _result(off_a) == "http_400"
    stored = OutboxMessage.objects.get(pk=on_a.pk)
    assert (stored.status, stored.next_attempt_at, stored.last_error) == (
        "pending",
        clock.now(),
        "",
    )

    # Next pass: ON A goes out, then OFF B's delete succeeds. The channel ends showing
    # OFF A and ON A: never only "power off" while the power is on.
    clock.advance(seconds=1)
    assert io_loop.run_iteration(clock, state) is True
    assert OutboxMessage.objects.get(pk=on_a.pk).status == "sent"
    assert _result(off_b) == outbox.DELETE_DELETED
    assert _on_state(on_b) == ("dropped", outbox.OUTAGE_REMOVED)
    assert _requests(fake_telegram) == [
        ("A", "sendMessage"),
        ("A", "sendMessage"),
        ("A", "sendMessage"),
        ("A", "deleteMessage"),
        ("A", "deleteMessage"),
        ("A", "sendMessage"),
        ("A", "deleteMessage"),
    ]


# Migration 0012 is expand-only: the previous release's inserts still work (D10)


@pytest.mark.django_db
def test_D10_a7e984c_inserts_still_work_on_0012(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    with connection.cursor() as cur:
        # a7e984c's outbox insert: the twelve columns it knew, no new one.
        cur.execute(
            """
            INSERT INTO outbox_message (channel, kind, event_at, recorded_at, payload, status,
                                        attempts, next_attempt_at, expires_at, last_error,
                                        sent_at, location_id)
            VALUES ('subscriber', 'power_off', %(at)s, %(at)s, '{}', 'pending', 0, %(at)s,
                    %(at)s, '', NULL, %(id)s)
            RETURNING tg_chat_id, tg_message_id, delete_requested_at, delete_result
            """,
            {"at": REMOVED, "id": location.pk},
        )
        assert cur.fetchone() == (None, None, None, None)
        # a7e984c's chart insert is lifecycle.INSERT_SQL, unchanged by 0012.
        cur.execute(
            lifecycle.INSERT_SQL.replace("RETURNING id", "RETURNING redraw_requested_at"),
            {
                "location_id": location.pk,
                "local_date": REMOVED.date(),
                "chat_id": DEFAULT_CHAT_ID,
                "bot_key": io_loop.bot_key(TOKEN_A),
                "message_id": 1001,
                "answered": REMOVED,
            },
        )
        assert cur.fetchone() == (None,)
        cur.execute(
            """
            SELECT table_name, column_name, is_nullable, column_default
              FROM information_schema.columns
             WHERE (table_name, column_name) IN (
                   ('outbox_message', 'tg_chat_id'), ('outbox_message', 'tg_message_id'),
                   ('outbox_message', 'delete_requested_at'),
                   ('outbox_message', 'delete_result'),
                   ('chart_message', 'redraw_requested_at'))
             ORDER BY 1, 2
            """
        )
        columns = cur.fetchall()
        cur.execute("SELECT conname FROM pg_constraint WHERE conname = 'outbox_delete_needs_ids'")
        constraint = cur.fetchone()
        cur.execute("SELECT indexname FROM pg_indexes WHERE indexname = 'outbox_delete_due_idx'")
        index = cur.fetchone()

    assert columns == [
        ("chart_message", "redraw_requested_at", "YES", None),
        ("outbox_message", "delete_requested_at", "YES", None),
        ("outbox_message", "delete_result", "YES", None),
        ("outbox_message", "tg_chat_id", "YES", None),
        ("outbox_message", "tg_message_id", "YES", None),
    ]
    assert constraint == ("outbox_delete_needs_ids",)
    assert index == ("outbox_delete_due_idx",)
    assert ChartMessage.objects.get().redraw_requested_at is None


@pytest.mark.django_db
def test_D10_the_check_needs_a_sent_row_with_both_ids(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    off = _alert(location, outbox.KIND_POWER_OFF, message_id=1, sent_at=OFF_SENT, requested=None)

    rows = OutboxMessage.objects.filter(pk=off.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        rows.update(tg_message_id=None, delete_requested_at=REMOVED)
    with pytest.raises(IntegrityError), transaction.atomic():
        rows.update(status="pending", delete_requested_at=REMOVED)

    # A sent row with both ids may carry a request.
    assert OutboxMessage.objects.filter(pk=off.pk).update(delete_requested_at=REMOVED) == 1


@DB
def test_DATA02_fail_delete_on_an_on_alert_cancels_nothing(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    off, on = _pair(location)

    assert outbox.fail_delete(on, "http_400", REMOVED) == 0
    # Settled once only: a second outcome changes nothing.
    assert outbox.fail_delete(on, "too_old", REMOVED) == 0
    assert outbox.settle_delete(on.pk, "deleted") is False

    assert (_result(off), _result(on)) == (None, "http_400")


@DB
def test_DATA02_a_request_from_another_removal_is_not_cancelled(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory()
    off, on = _pair(location)
    # The ON was requested by a different removal (another time): only same-removal rows go.
    OutboxMessage.objects.filter(pk=on.pk).update(delete_requested_at=REMOVED + US)
    fake_telegram.fail_method(TOKEN_A, "deleteMessage", status=400, json_body=CANNOT)
    fake_telegram.accept_chart(TOKEN_A)
    clock = FakeClock(REMOVED)
    state = io_loop.RelayState()

    assert io_loop.run_iteration(clock, state) is True
    assert io_loop.run_iteration(clock, state) is True

    assert (_result(off), _result(on)) == ("http_400", "deleted")
    assert json.loads(fake_telegram.calls[1].request.body)["message_id"] == 2


def _chart_location(location: Any) -> lifecycle.ChartLocation:
    return lifecycle.ChartLocation(
        location.pk,
        location.name,
        location.language,
        location.bot_token,
        location.chat_id,
        timedelta(0),
    )


@DB
def test_delete_closes_chart_failing_without_notice(
    location_factory: Callable[..., Any], ops_settings: Any
) -> None:
    # F-04 (quick task 261008-vdk): a refused chart opens chart_failing; a delete closes it
    # with no recovery notice (D-09), like every other incident of the location.
    location = location_factory()
    other = location_factory()
    refused = SendResult("permanent", code="http_403")
    lifecycle._chart_refused(_chart_location(location), refused, REMOVED)
    lifecycle._chart_refused(_chart_location(other), refused, REMOVED)
    chart = OpsIncident.objects.filter(kind="chart_failing")
    assert chart.filter(location=location, ended_at__isnull=True).count() == 1
    notices = OutboxMessage.objects.filter(channel=outbox.CHANNEL_OPS)
    assert notices.filter(kind="ops_chart_failing", location=location).count() == 1
    later = REMOVED + timedelta(minutes=5)

    assert actions.delete_location(location.pk, later) is True

    [incident] = chart.filter(location=location)
    assert incident.ended_at == later
    assert not notices.filter(kind="ops_chart_restored").exists()
    # Another location's open incident stays open.
    assert chart.filter(location=other, ended_at__isnull=True).count() == 1
    # A refusal answered after the delete adds no incident and no notice.
    lifecycle._chart_refused(_chart_location(location), refused, later + US)
    assert chart.filter(location=location).count() == 1
    assert notices.filter(location=location).count() == 1
