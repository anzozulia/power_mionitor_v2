"""A second worker process started by mistake never adds a transition or an alert (INV-02 #2).

The child is a real ``python manage.py run_worker`` process on the test database, started
next to a test that holds the worker lock and acts as the active worker (MON-04, ALRT-05,
RESEARCH spike 4):

- While the test holds the lock, the child stays in standby: it logs "standby: waiting for
  the worker lock" and writes and sends nothing, while the test's detection cycle records
  the location's one OFF transition and its one OFF alert.
- Once the test lets go, the child takes the lock ("worker lock held (generation 1)") and
  becomes the active worker. It adds no transition and no alert of its own: there is still
  exactly 1 power_off row and 1 off interval. SIGTERM stops it with exit code 0.
- INV-23 #3 (OPS-08, D-16): at LOG_LEVEL=DEBUG its whole stdout holds no bot token and
  no token secret, and every log line starts with a UTC ISO 8601 timestamp.

pytest-socket guards only this process, not the child (RESEARCH Pitfall 9). So the child
gets ``HTTPS_PROXY=http://127.0.0.1:9``: every HTTPS request it makes goes to a refused
local port, and it never even looks up api.telegram.org. Telegram's client classifies a
refused proxy as "maybe delivered", so the rows the child touched are counted, never
checked for a status. The child gets no ops chat, so its ops notices go to its log.

Cleanup, even when an assertion fails: the test's own lease is closed, the child gets
SIGTERM and 30 s to exit (then SIGKILL), and its output reader is joined, so no child
session outlives the test and pytest-django can flush and drop the test database.

The test takes about 20 s: the child's start, its standby poll every 5 s, and two 6 s
observation windows.
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import IO, Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, wait_for
from django.conf import settings
from django.db import connection

from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.clock import SystemClock
from powermon.engine import transitions
from powermon.engine.models import PowerInterval, SystemState
from powermon.worker import detection
from powermon.worker.lease import Lease

STANDBY = "standby: waiting for the worker lock"
TAKEOVER = "worker lock held (generation 1)"
# Every HTTPS request of the child goes to this refused port (no DNS for Telegram).
REFUSED_PROXY = "http://127.0.0.1:9"
# How long the child gets to show standby or the takeover (its start, its 5 s poll).
CHILD_WAIT_S = 20.0
# How long the test watches the child for anything it should not do.
OBSERVE_S = 6.0
# A line of our log format, and the UTC ISO 8601 timestamp it must start with (D-16).
LOG_LINE = re.compile(r"(?:^|\s)(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) [A-Za-z_][\w.]* ")
UTC_PREFIX = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{12}[+]00:00 ")


class _Output:
    """The child's stdout and stderr, read line by line in a daemon thread."""

    def __init__(self, stream: IO[str]) -> None:
        self.lines: list[str] = []
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: IO[str]) -> None:
        for line in stream:
            self.lines.append(line.rstrip("\n"))

    def has(self, text: str) -> bool:
        return any(text in line for line in list(self.lines))

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)


def _child_env() -> dict[str, str]:
    """The child's environment: the test database, no way to Telegram, DEBUG logs."""
    return {
        **os.environ,
        "POSTGRES_DB": connection.settings_dict["NAME"],
        # Both spellings: a lowercase variable would win over the uppercase one.
        "HTTPS_PROXY": REFUSED_PROXY,
        "https_proxy": REFUSED_PROXY,
        # No host may bypass the proxy.
        "NO_PROXY": "",
        "no_proxy": "",
        "PYTHONUNBUFFERED": "1",
        "LOG_LEVEL": "DEBUG",
        # No ops chat: the child's ops notices go to its log (D-09).
        "OPS_BOT_TOKEN": "",
        "OPS_CHAT_ID": "",
    }


def _start_child() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "manage.py", "run_worker"],
        cwd=settings.BASE_DIR,
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_child(child: subprocess.Popen[str]) -> int | None:
    """SIGTERM, then up to 30 s; SIGKILL on a timeout. The exit code, None if killed."""
    if child.poll() is not None:
        return child.returncode
    child.send_signal(signal.SIGTERM)
    try:
        return child.wait(30)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(10)
        return None


def _counts(location: Any) -> tuple[int, int]:
    """(power_off alerts, off intervals) of ``location``."""
    offs = OutboxMessage.objects.filter(location=location, kind="power_off").count()
    intervals = PowerInterval.objects.filter(location=location, state="off").count()
    return offs, intervals


def _watch(seconds: float, check: Callable[[], None]) -> None:
    """Run ``check`` every 0.5 s for ``seconds`` real seconds, and once at the end."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check()
        time.sleep(0.5)
    check()


# About 20 s: a real child process, its 5 s standby poll and two 6 s windows.
@pytest.mark.django_db(transaction=True)
def test_INV02_second_worker_process_stays_standby_one_off(
    location_factory: Callable[..., Any],
) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )
    clock = SystemClock()
    location = location_factory()
    # On, with its last heartbeat 10 min ago: the next detection cycle times it out.
    first = transitions.record_heartbeat(location.pk, clock.now() - timedelta(minutes=10))
    assert first == "started"
    # The test is the active worker: it holds the lock before the child starts.
    lease = Lease(connection.settings_dict, clock)
    assert lease.ensure_held().state == "held"

    child = _start_child()
    assert child.stdout is not None
    output = _Output(child.stdout)
    code: int | None = None
    try:
        assert wait_for(lambda: output.has(STANDBY), CHILD_WAIT_S)

        # The active worker's cycle records the one OFF; the standby child does nothing.
        assert detection.run_cycle(clock.now()) == 1
        [off] = OutboxMessage.objects.filter(location=location, kind="power_off")

        def standby_did_nothing() -> None:
            assert _counts(location) == (1, 1)
            assert OpsIncident.objects.count() == 0
            row = OutboxMessage.objects.get(pk=off.pk)
            assert (row.status, row.attempts) == ("pending", 0)

        _watch(OBSERVE_S, standby_did_nothing)
        assert child.poll() is None
        assert not output.has(TAKEOVER)

        # The first worker lets go: the child takes over within its 5 s poll.
        lease.close()
        assert wait_for(lambda: output.has(TAKEOVER), CHILD_WAIT_S)
        # Its relay sends the queued OFF (to the refused proxy) and adds nothing.
        assert wait_for(lambda: OutboxMessage.objects.get(pk=off.pk).attempts >= 1, CHILD_WAIT_S)

        def active_added_nothing() -> None:
            assert _counts(location) == (1, 1)

        _watch(OBSERVE_S, active_added_nothing)
        assert OutboxMessage.objects.filter(location=location, kind="power_on").count() == 0
    finally:
        lease.close()
        code = _stop_child(child)
        output.join(10)

    assert code == 0, "\n".join(output.lines)
    text = "\n".join(output.lines)
    secret = DEFAULT_BOT_TOKEN.split(":", 1)[1]
    assert DEFAULT_BOT_TOKEN not in text
    assert secret not in text
    log_lines = [line for line in output.lines if LOG_LINE.search(line)]
    assert any(STANDBY in line for line in log_lines)
    assert any(TAKEOVER in line for line in log_lines)
    assert [line for line in log_lines if not UTC_PREFIX.match(line)] == []
