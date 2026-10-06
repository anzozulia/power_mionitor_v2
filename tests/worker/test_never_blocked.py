"""Telegram trouble never reaches heartbeats or detection (INV-14 #2, HB-03).

The worker runs for real (``run_worker.serve``: both loops, the lease on the test
database), and Telegram is made slow or unreachable for the Telegram I/O thread:

- a send that hangs inside the request until the test lets it go;
- a run of sends that each block for a while and then fail with a connect timeout.

While the I/O thread is stuck inside those sends, device heartbeats through
``HeartbeatView`` each answer 200 in under 1 s, and the detection loop keeps its cadence:
``detection.run_detection`` (spied on as the module attribute run_worker calls) runs at
least 10 times in 2 s with a 0.1 s interval. A heartbeat does no network I/O at all
(KD2), and the I/O thread is the only thread that ever waits on Telegram.

A delete of a removed outage's alert (261006-qv7) is a non-alert call like a chart call:
a 429 on it holds only its own bot, so another bot's alert goes in the next pass at the
same clock (INV-14 #1), checked on the I/O pass itself (``io_loop.run_iteration``).

Time: the worker gets a ``FakeClock`` that never moves, so no location times out and no
backoff elapses; real ``time.monotonic()`` only measures latency and cadence. The
watchdog checks only every 30 s here, so it never runs during a test (its own tests are
in test_worker.py), and every hung send is released and every thread joined in
``finally``.
"""

import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, wait_for
from django.db import connection, transaction
from django.http import HttpResponse
from django.test import RequestFactory

from powermon.alerts import outbox
from powermon.alerts.models import OutboxMessage
from powermon.engine.models import SystemState
from powermon.web.views import HeartbeatView
from powermon.worker import detection, io_loop, supervision
from powermon.worker.lease import Lease
from powermon.worker.management.commands import run_worker

T0 = datetime(2026, 10, 1, 10, 6, 31, tzinfo=UTC)
OFF_EN = "🔴 <b>POWER OFF</b>\n⚡ Power was ON for: <b>5m</b>"
# Worker intervals: detection every 0.1 s; the watchdog never checks during a test.
INTERVALS = {"detection_interval": 0.1, "io_idle_wait": 0.05, "check_interval": 30.0}
HEARTBEATS = 10
HEARTBEAT_LIMIT_S = 1.0
CYCLES = 10
CADENCE_WINDOW_S = 2.0
# How long each failing send blocks before its connect timeout is raised.
CONNECT_BLOCK_S = 1.5


class _Serve:
    """``run_worker.serve`` in a thread with a FakeClock and an injected stall action."""

    def __init__(self, lease: Lease, clock: FakeClock, health_path: Path) -> None:
        self.stop = threading.Event()
        self.code: int | None = None
        self.stalls: list[str] = []
        health = supervision.HealthFile(health_path)

        def target() -> None:
            self.code = run_worker.serve(
                self.stop, clock, lease, health=health, on_stall=self.stalls.append, **INTERVALS
            )

        self.thread = threading.Thread(target=target, name="serve-under-test")
        self.thread.start()

    def finish(self) -> int | None:
        self.stop.set()
        # A send in flight ends within its own wait; the joins inside serve wait for it.
        self.thread.join(30)
        return self.code


@pytest.fixture
def lease() -> Iterator[Lease]:
    made = Lease(connection.settings_dict)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def cycles(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The real time of every ``detection.run_detection`` call (the real one still runs)."""
    calls: list[float] = []
    real = detection.run_detection

    def spy(*args: Any, **kwargs: Any) -> int:
        calls.append(time.monotonic())
        return real(*args, **kwargs)

    monkeypatch.setattr(detection, "run_detection", spy)
    return calls


def _queue_off(location: Any) -> OutboxMessage:
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=T0 - timedelta(seconds=91),
            recorded_at=T0,
            payload={"was_on_us": 300_000_000},
        )


def _heartbeats_answer_fast(location: Any, clock: FakeClock) -> None:
    """HEARTBEATS heartbeats through HeartbeatView, each 200 in under HEARTBEAT_LIMIT_S."""
    view = HeartbeatView.as_view(clock=clock)
    factory = RequestFactory()
    for n in range(HEARTBEATS):
        request = factory.get("/hb", headers={"authorization": f"Bearer {location.device_key}"})
        started = time.monotonic()
        response: HttpResponse = view(request)
        latency = time.monotonic() - started
        assert (response.status_code, response.content) == (200, b"ok")
        assert latency < HEARTBEAT_LIMIT_S, f"heartbeat {n} took {latency:.2f} s"


def _cycles_within(calls: list[float], count: int, seconds: float) -> bool:
    """True once ``count`` more detection cycles ran within ``seconds`` real seconds."""
    seen = len(calls)
    return wait_for(lambda: len(calls) >= seen + count, seconds)


def _resume() -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


@pytest.mark.django_db(transaction=True)
def test_INV14_telegram_unreachable_heartbeats_fast_cycles_on_cadence(
    lease: Lease,
    cycles: list[float],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    tmp_path: Path,
) -> None:
    _resume()
    row = _queue_off(location_factory())
    # The device's location uses the same bot: a heartbeat that waited on that bot hangs.
    device = location_factory()
    inside, release = threading.Event(), threading.Event()

    def hang() -> None:
        inside.set()
        release.wait(10)  # the send hangs until the test lets it go

    fake_telegram.answer(DEFAULT_BOT_TOKEN, hang)
    clock = FakeClock(T0 + timedelta(minutes=1))

    serving = _Serve(lease, clock, tmp_path / "health")
    try:
        assert inside.wait(10)  # the I/O thread is inside the send
        _heartbeats_answer_fast(device, clock)
        assert _cycles_within(cycles, CYCLES, CADENCE_WINDOW_S)
        # All of it happened while the send was still hanging.
        assert not release.is_set()
        assert fake_telegram.sent == []
    finally:
        release.set()
        code = serving.finish()

    assert code == 0
    assert serving.stalls == []
    assert fake_telegram.sent == [
        {"chat_id": DEFAULT_CHAT_ID, "text": OFF_EN, "parse_mode": "HTML"}
    ]
    sent = OutboxMessage.objects.get(pk=row.pk)
    assert (sent.status, sent.attempts) == ("sent", 1)


@pytest.mark.django_db(transaction=True)
def test_INV14_connect_timeouts_never_slow_detection(
    lease: Lease,
    cycles: list[float],
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    tmp_path: Path,
) -> None:
    _resume()
    # Three bots, each send blocking 1.5 s before its connect timeout: 4.5 s of I/O. Any
    # wait on one of them in a heartbeat or a cycle would blow its budget.
    bots = [f"{700000000 + n}:" + "C" * 35 for n in range(3)]
    rows = [
        _queue_off(location_factory(bot_token=token, chat_id=-1007000000000 - n))
        for n, token in enumerate(bots)
    ]
    device = location_factory(bot_token=bots[0], chat_id=-1007000000009)
    started = threading.Event()

    def slow_connect() -> None:
        started.set()
        time.sleep(CONNECT_BLOCK_S)

    for token in bots:
        fake_telegram.answer(token, slow_connect, exc=requests.ConnectTimeout("connect timed out"))
    clock = FakeClock(T0 + timedelta(minutes=1))

    serving = _Serve(lease, clock, tmp_path / "health")
    try:
        assert started.wait(10)
        window_start = time.monotonic()
        _heartbeats_answer_fast(device, clock)
        assert _cycles_within(cycles, CYCLES, CADENCE_WINDOW_S)
        window = time.monotonic() - window_start
        # Every bot's send was made once, and the I/O thread was busy failing throughout.
        assert wait_for(lambda: len(fake_telegram.calls) == len(bots), 10)
    finally:
        code = serving.finish()

    assert window < len(bots) * CONNECT_BLOCK_S
    assert code == 0
    assert serving.stalls == []
    assert len(fake_telegram.calls) == len(bots)
    for message in rows:
        failed = OutboxMessage.objects.get(pk=message.pk)
        assert (failed.status, failed.attempts, failed.last_error) == (
            "pending",
            1,
            "connect_timeout",
        )


TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210


def _sent_off_requested(location: Any, at: datetime) -> OutboxMessage:
    """A sent OFF alert of ``location`` with its stored ids, its deletion requested at ``at``."""
    return OutboxMessage.objects.create(
        channel=outbox.CHANNEL_SUBSCRIBER,
        location=location,
        kind=outbox.KIND_POWER_OFF,
        event_at=at - timedelta(minutes=20),
        recorded_at=at - timedelta(minutes=19),
        payload={"was_on_us": 300_000_000},
        status="sent",
        attempts=1,
        next_attempt_at=at - timedelta(minutes=19),
        expires_at=at + timedelta(hours=24),
        sent_at=at - timedelta(minutes=19),
        tg_chat_id=DEFAULT_CHAT_ID,
        tg_message_id=7,
        delete_requested_at=at,
    )


def _queue_at(location: Any, at: datetime) -> OutboxMessage:
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=at - timedelta(seconds=91),
            recorded_at=at,
            payload={"was_on_us": 300_000_000},
        )


@pytest.mark.django_db(transaction=True)
def test_INV14_429_on_a_delete_never_delays_another_bot(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    bot_a = location_factory()
    bot_b = location_factory(bot_token=TOKEN_B, chat_id=CHAT_B)
    request = _sent_off_requested(bot_a, T0)
    limited = {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}
    fake_telegram.fail_method(DEFAULT_BOT_TOKEN, "deleteMessage", status=429, json_body=limited)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(TOKEN_B)
    clock = FakeClock(T0)
    state = io_loop.RelayState()

    # Pass 1: bot A's delete is rate limited for 30 s.
    assert io_loop.run_iteration(clock, state) is True
    alert_a, alert_b = _queue_at(bot_a, T0), _queue_at(bot_b, T0)

    # Pass 2, same clock: bot B's new alert goes at once; nothing is called for bot A.
    assert io_loop.run_iteration(clock, state) is True
    assert _urls(fake_telegram) == [("A", "deleteMessage"), ("B", "sendMessage")]
    assert OutboxMessage.objects.get(pk=alert_a.pk).status == "pending"

    # At +30 s bot A's alert goes first, then its delete.
    clock.advance(seconds=30)
    assert io_loop.run_iteration(clock, state) is True

    assert _urls(fake_telegram) == [
        ("A", "deleteMessage"),
        ("B", "sendMessage"),
        ("A", "sendMessage"),
        ("A", "deleteMessage"),
    ]
    assert OutboxMessage.objects.get(pk=alert_b.pk).status == "sent"
    assert OutboxMessage.objects.get(pk=alert_a.pk).status == "sent"
    assert OutboxMessage.objects.get(pk=request.pk).delete_result == "deleted"


def _urls(fake: Any) -> list[tuple[str, str]]:
    """Every request as (bot, method): "A" is the default bot, "B" the other one."""
    labels = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B"}
    out = []
    for call in fake.calls:
        token, method = call.request.url.split("/bot", 1)[1].rsplit("/", 1)
        out.append((labels[token], method))
    return out
