"""One location's unexpected chart-step error never stops the other charts (INV-13).

``run_step`` asks the pure ``plan`` for the one step due now. A step that raises after it
was chosen, and leaves no backoff key, is chosen again on every pass, so no other
location's chart moves (code review WR-02). So, once a step is chosen:

- a lost connection (OperationalError, InterfaceError) still ends the pass, and the next
  pass retries the step at once (MON-06);
- any other error backs off that step only, for 15 min, with exactly one ERROR line. The
  line carries the traceback, which the worker's redacting formatter scrubs (OPS-08). The
  next pass chooses another location's step;
- after an error that follows Telegram's acceptance of a post, the post is kept, so the
  next chart step records it and it is never posted twice (WR-04 analogue). An
  idempotent call (an edit) is simply made again after the wait.

Every test is ``django_db(transaction=True)``, because ``run_iteration`` calls
``close_old_connections()``. Time comes only from the ``FakeClock``. Telegram is faked at
the HTTP boundary (``fake_telegram``). Renders are real.
"""

import dataclasses
import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from chart_fixtures import KYIV, kyiv, monitor
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock
from django.db import InterfaceError, ProgrammingError

from powermon import logging_setup
from powermon.chart import lifecycle, source
from powermon.chart.model import Week
from powermon.chart.models import ChartMessage
from powermon.worker import io_loop

pytestmark = pytest.mark.django_db(transaction=True)

# Thu 2026-10-01 12:05 local is "now"; monitoring started at 08:00 local.
TODAY = date(2026, 10, 1)
NOON_05 = kyiv("2026-10-01 12:05")
SINCE = kyiv("2026-10-01 08:00")
TOKEN_B = "987654321:" + "B" * 35
CHAT_B = -1009876543210
# Which bot a request went to, by a short label (a failing assert never prints a token).
BOTS = {DEFAULT_BOT_TOKEN: "A", TOKEN_B: "B"}
LIFECYCLE_LOGGER = lifecycle.__name__
# How long a step that failed unexpectedly waits (io_loop.PERMANENT_BACKOFF).
WAIT = timedelta(minutes=15)


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _monitored(location_factory: Callable[..., Any], **kw: Any) -> Any:
    """A location on since 08:00 local, with its open on piece (a monitored location)."""
    location = location_factory(**kw)
    monitor(location, SINCE)
    return location


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    return io_loop.run_iteration(clock, state, charts=True)


def _requests(fake: Any) -> list[tuple[str, str]]:
    """(bot label, Bot API method) of every request, failed ones too, in order."""
    out = []
    for call in fake.calls:
        token, method = call.request.url.split("/bot", 1)[1].split("/", 1)
        out.append((BOTS[token], method))
    return out


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The lifecycle's ERROR records, in order."""
    return [r for r in caplog.records if r.name == LIFECYCLE_LOGGER and r.levelno >= logging.ERROR]


def test_INV13_failing_step_waits_and_other_locations_go_on(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = _monitored(location_factory, name="Broken")
    _monitored(location_factory, bot_token=TOKEN_B, chat_id=CHAT_B)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(TOKEN_B)
    real = source.load_week
    reads: list[int] = []

    def broken_week(location_id: int, **kwargs: Any) -> Week:
        if location_id == broken.pk:
            reads.append(location_id)
            # A database error on a working connection: a bug, not a lost connection.
            raise ProgrammingError('column "outage_start_at" does not exist')
        return real(location_id, **kwargs)

    monkeypatch.setattr(source, "load_week", broken_week)
    caplog.set_level(logging.INFO)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()
    key = lifecycle.chart_key(broken.pk, "post")

    # The lower id goes first. Its post fails, makes no call and waits 15 min.
    assert _pass(clock, state) is False
    assert state.not_before == {key: NOON_05 + WAIT}
    # The next passes post and pin the other location's chart. The failing step waits.
    assert _pass(clock, state) is True
    assert _pass(clock, state) is True
    assert _pass(clock, state) is False
    assert _requests(fake_telegram) == [("B", "sendPhoto"), ("B", "pinChatMessage")]
    assert reads == [broken.pk]
    [error] = _errors(caplog)
    assert error.exc_info is not None
    assert "post" in error.getMessage() and str(broken.pk) in error.getMessage()
    assert "chart step failed" not in caplog.text

    # 15 min later the step is tried once more, and fails once more ...
    clock.set(NOON_05 + WAIT)
    assert _pass(clock, state) is False
    assert reads == [broken.pk, broken.pk]
    assert len(_errors(caplog)) == 2
    assert state.not_before[key] == NOON_05 + 2 * WAIT
    # ... and the other location's refresh still goes.
    assert _pass(clock, state) is True
    assert _requests(fake_telegram)[-1] == ("B", "editMessageMedia")
    assert reads == [broken.pk, broken.pk]


def test_INV13_error_after_an_accepted_post_keeps_it_without_a_second_photo(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    real = lifecycle._record_post
    tries: list[int] = []

    def crashed_once(*args: Any, **kwargs: Any) -> None:
        tries.append(1)
        if len(tries) == 1:
            # Not a database error, so ``_record_or_keep`` does not keep the post itself.
            raise TypeError("unexpected answer")
        real(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_post", crashed_once)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    # Telegram accepted the photo, so the pass made a call and the post is kept.
    assert _pass(clock, state) is True
    assert ChartMessage.objects.count() == 0
    assert state.chart_posted == {(location.pk, TODAY): (DEFAULT_CHAT_ID, 1001, NOON_05)}
    [error] = _errors(caplog)
    assert error.exc_info is not None and "post" in error.getMessage()

    # The next pass writes the kept post first, then pins it. No second photo.
    assert _pass(clock, state) is True
    assert state.chart_posted == {}
    assert state.not_before == {}
    [row] = ChartMessage.objects.all()
    assert (row.message_id, row.pinned, row.last_rendered_at) == (1001, True, NOON_05)
    assert _requests(fake_telegram) == [("A", "sendPhoto"), ("A", "pinChatMessage")]
    assert len(_errors(caplog)) == 1


def test_INV13_error_after_an_edit_waits_and_the_edit_is_made_again(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    ChartMessage.objects.create(
        location=location,
        local_date=TODAY,
        chat_id=DEFAULT_CHAT_ID,
        message_id=1001,
        pinned=True,
        last_rendered_at=NOON_05,
        created_at=NOON_05,
    )
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)

    def crashed(*args: Any, **kwargs: Any) -> None:
        raise TypeError("unexpected answer")

    # The edit is accepted, but writing its outcome fails with a non-database error.
    monkeypatch.setattr(lifecycle, "_apply", crashed)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    at = NOON_05 + WAIT
    clock = FakeClock(at)
    state = io_loop.RelayState()
    key = lifecycle.chart_key(location.pk, "refresh")

    assert _pass(clock, state) is True
    assert state.not_before == {key: at + WAIT}
    assert len(_errors(caplog)) == 1
    # No edit on every pass while the step waits.
    clock.set(at + WAIT - timedelta(seconds=1))
    assert _pass(clock, state) is False
    # An edit is idempotent, so the same edit is simply made again after the wait.
    monkeypatch.undo()
    clock.set(at + WAIT)
    assert _pass(clock, state) is True
    assert ChartMessage.objects.get().last_rendered_at == at + WAIT
    assert _requests(fake_telegram) == [("A", "editMessageMedia"), ("A", "editMessageMedia")]
    assert state.not_before == {}
    assert len(_errors(caplog)) == 1


def test_INV13_lost_connection_still_ends_the_pass(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)

    def gone(pid: int | None) -> bool:
        raise InterfaceError("connection already closed")

    monkeypatch.setattr(lifecycle, "_lease_holds", gone)
    caplog.set_level(logging.WARNING)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    with pytest.raises(InterfaceError):
        lifecycle.run_step(clock, state)
    # The pass logs the class only. No step waits, so the next pass retries at once.
    assert _pass(clock, state) is False
    assert "chart step failed: InterfaceError" in caplog.text
    assert state.not_before == {}
    assert _errors(caplog) == []
    assert len(fake_telegram.calls) == 0
    monkeypatch.undo()
    assert _pass(clock, state) is True
    assert _requests(fake_telegram) == [("A", "sendPhoto")]


def test_INV13_step_error_log_holds_no_token(
    location_factory: Callable[..., Any],
    fake_telegram: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = _monitored(location_factory)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)

    def bad_call(*args: Any, **kwargs: Any) -> Any:
        # An exception whose text carries the token, as a request URL in it would.
        raise ValueError(f"no call for https://api.telegram.org/bot{DEFAULT_BOT_TOKEN}/x")

    monkeypatch.setattr(lifecycle, "_call", bad_call)
    caplog.set_level(logging.WARNING, logger=LIFECYCLE_LOGGER)
    clock = FakeClock(NOON_05)
    state = io_loop.RelayState()

    # Raised before any request, so no call was made, and the post waits 15 min.
    assert _pass(clock, state) is False
    assert len(fake_telegram.calls) == 0
    assert state.not_before == {lifecycle.chart_key(location.pk, "post"): NOON_05 + WAIT}
    [error] = _errors(caplog)
    secret = DEFAULT_BOT_TOKEN.split(":", 1)[1]
    assert secret not in error.getMessage()
    # The traceback is logged, and the worker's formatter scrubs the token from it.
    formatted = logging_setup.RedactingFormatter(logging_setup.FORMAT).format(error)
    assert "ValueError" in formatted and "[REDACTED-TOKEN]" in formatted
    assert secret not in formatted
