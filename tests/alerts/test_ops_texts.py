"""The ops notice texts: exact English strings in the display TZ (D-10, D-11, D-12).

The expected strings are the D-11/D-12 samples (02-CONTEXT "Specific Ideas"), with times
computed by hand for Europe/Kyiv (UTC+3 on 2026-10-01). Durations come from the shared
alert formatter, which prints an exact 3-minute span as "3m". Every text that names a
location HTML-escapes the name by default and leaves it raw with ``escape=False`` (the log
form, D-09). The all-silent texts stay neutral (D-12, Pitfall 11).

``ops_texts`` is pure. The ``ops.render_text`` cases at the end read the payload's integers
and the names from the database at call time, so they carry ``django_db``.
"""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import transaction

from powermon.alerts import ops, ops_texts, outbox
from powermon.alerts.models import OutboxMessage
from powermon.locations.models import Location

KYIV = "Europe/Kyiv"
RAW = "<b>A&B</b>"
ESCAPED = "&lt;b&gt;A&amp;B&lt;/b&gt;"

GAP = (
    "⏸ Monitoring gap 01.10 10:00:12 – 10:10:40 (10m 28s). "
    "Recorded as not monitored; no subscriber alerts were sent for it."
)
DB_DOWN = (
    "🛑 Database unreachable since 10:02:05 (over 5 min). "
    "Detection is paused; the gap will be recorded as not monitored when it is back."
)
ALL_SILENT_START = (
    "⚠️ All 3 active locations silent since 14:10: an area power/ISP outage or a "
    "server/network problem. Subscriber alerts continue as normal."
)
ALL_SILENT_END = "✅ Heartbeats are back (first: Office, 14:13:00); all-silent lasted 3m."
EXPIRED = "⌛ OFF alert for Office (event 17:27) expired undelivered after 6h and will not be sent."
UNCERTAIN = (
    "❓ ON alert for Office (event 17:45) may not have been delivered "
    "(Telegram timed out after the request was sent). It will not be resent; "
    "please check the channel."
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _naive(text: str) -> datetime:
    return datetime.fromisoformat(text)


# The six texts, exactly as specified


def test_gap() -> None:
    text = ops_texts.gap(_utc("2026-10-01T07:00:12"), _utc("2026-10-01T07:10:40"), KYIV)

    assert text == GAP


def test_gap_across_local_midnight_dates_both_ends() -> None:
    text = ops_texts.gap(_utc("2026-09-30T20:58:00"), _utc("2026-09-30T21:05:00"), KYIV)

    assert text.startswith("⏸ Monitoring gap 30.09 23:58:00 – 01.10 00:05:00 (7m). ")


def test_db_down() -> None:
    since, now = _utc("2026-10-01T07:02:05"), _utc("2026-10-01T07:07:10")

    assert ops_texts.db_down(since, now, KYIV) == DB_DOWN


def test_db_down_since_the_previous_local_day_shows_its_date() -> None:
    since, now = _utc("2026-09-30T20:58:00"), _utc("2026-09-30T21:04:00")

    assert ops_texts.db_down(since, now, KYIV).startswith(
        "🛑 Database unreachable since 30.09 23:58:00 (over 5 min). "
    )


def test_all_silent_start_is_neutral() -> None:
    since, now = _utc("2026-10-01T11:10:00"), _utc("2026-10-01T11:11:05")

    text = ops_texts.all_silent_start(since, 3, now, KYIV)

    assert text == ALL_SILENT_START
    # Neither cause is stated as a fact (D-12).
    assert "an area power/ISP outage or a server/network problem" in text


def test_all_silent_end() -> None:
    since, first = _utc("2026-10-01T11:10:00"), _utc("2026-10-01T11:13:00")

    assert ops_texts.all_silent_end(since, first, "Office", KYIV) == ALL_SILENT_END


def test_expired() -> None:
    event, now = _utc("2026-10-01T14:27:00"), _utc("2026-10-01T20:30:00")

    text = ops_texts.expired("power_off", event, timedelta(hours=6), "Office", now, KYIV)

    assert text == EXPIRED


def test_expired_on_another_local_date_shows_the_date() -> None:
    # An event at 23:58 on 30.09 that expired at 05:58 on 01.10.
    event, now = _utc("2026-09-30T20:58:00"), _utc("2026-10-01T02:58:00")

    text = ops_texts.expired("power_on", event, timedelta(hours=6), "Office", now, KYIV)

    assert text.startswith("⌛ ON alert for Office (event 30.09 23:58) expired undelivered")


def test_uncertain() -> None:
    event, now = _utc("2026-10-01T14:45:00"), _utc("2026-10-01T14:50:00")

    text = ops_texts.uncertain(
        "power_on", event, "Office", interrupted=False, now=now, tz=KYIV
    )

    assert text == UNCERTAIN


def test_uncertain_after_an_interrupted_send_names_the_worker_stop() -> None:
    event, now = _utc("2026-10-01T14:45:00"), _utc("2026-10-01T14:50:00")

    text = ops_texts.uncertain("power_off", event, "Office", interrupted=True, now=now, tz=KYIV)

    assert text == (
        "❓ OFF alert for Office (event 17:45) may not have been delivered "
        "(the worker stopped while sending it). It will not be resent; please check the channel."
    )


# Names are escaped for Telegram HTML, and raw for the log (D-09, D-10)


def _named_texts(name: str, escape: bool) -> list[str]:
    at, now = _utc("2026-10-01T11:10:00"), _utc("2026-10-01T11:13:00")
    return [
        ops_texts.all_silent_end(at, now, name, KYIV, escape=escape),
        ops_texts.expired("power_off", at, timedelta(hours=6), name, now, KYIV, escape=escape),
        ops_texts.uncertain(
            "power_off", at, name, interrupted=False, now=now, tz=KYIV, escape=escape
        ),
    ]


def test_every_name_is_escaped_by_default() -> None:
    at, now = _utc("2026-10-01T11:10:00"), _utc("2026-10-01T11:13:00")
    texts = [
        ops_texts.all_silent_end(at, now, RAW, KYIV),
        ops_texts.expired("power_off", at, timedelta(hours=6), RAW, now, KYIV),
        ops_texts.uncertain("power_off", at, RAW, interrupted=False, now=now, tz=KYIV),
    ]

    for text in texts:
        assert ESCAPED in text
        assert RAW not in text
    assert texts == _named_texts(RAW, escape=True)


def test_escape_false_leaves_the_name_raw() -> None:
    for text in _named_texts(RAW, escape=False):
        assert RAW in text
        assert ESCAPED not in text


def test_a_quote_in_a_name_is_kept() -> None:
    # quote=False: a quote is harmless outside an attribute and reads better as is.
    [text, *_] = _named_texts('Office "A"', escape=True)

    assert '(first: Office "A", ' in text


# Failure cases


@pytest.mark.parametrize("kind", ["power_nope", "ops_uncertain", ""])
def test_an_unknown_alert_kind_raises(kind: str) -> None:
    at = _utc("2026-10-01T14:45:00")

    with pytest.raises(ValueError, match="unknown alert kind"):
        ops_texts.expired(kind, at, timedelta(hours=6), "Office", at, KYIV)
    with pytest.raises(ValueError, match="unknown alert kind"):
        ops_texts.uncertain(kind, at, "Office", interrupted=False, now=at, tz=KYIV)


@pytest.mark.parametrize(
    "render",
    [
        lambda a, n: ops_texts.gap(a, n, KYIV),
        lambda a, n: ops_texts.gap(n, a, KYIV),
        lambda a, n: ops_texts.db_down(a, n, KYIV),
        lambda a, n: ops_texts.all_silent_start(a, 3, n, KYIV),
        lambda a, n: ops_texts.all_silent_end(a, n, "Office", KYIV),
        lambda a, n: ops_texts.expired("power_off", a, timedelta(hours=6), "Office", n, KYIV),
        lambda a, n: ops_texts.uncertain(
            "power_off", a, "Office", interrupted=False, now=n, tz=KYIV
        ),
    ],
    ids=["gap-start", "gap-end", "db_down", "silent_start", "silent_end", "expired", "uncertain"],
)
def test_a_naive_datetime_raises(render: Callable[[datetime, datetime], str]) -> None:
    naive = _naive("2026-10-01T11:10:00")
    aware = _utc("2026-10-01T11:13:00")

    with pytest.raises(ValueError, match="naive"):
        render(naive, aware)


def test_a_gap_that_ends_before_it_starts_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        ops_texts.gap(_utc("2026-10-01T07:10:40"), _utc("2026-10-01T07:00:12"), KYIV)


# ops.render_text: integers from the payload, names read at call time


@pytest.fixture(autouse=True)
def kyiv(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def _alert(location: Any, kind: str, event_at: datetime, recorded_at: datetime) -> OutboxMessage:
    key = "was_on_us" if kind == "power_off" else "was_off_us"
    with transaction.atomic():
        return outbox.enqueue(
            kind,
            location.pk,
            event_at=event_at,
            recorded_at=recorded_at,
            payload={key: 60_000_000},
        )


def test_render_text_gap_from_integer_instants() -> None:
    payload = {
        "start_us": ops.instant_us(_utc("2026-10-01T07:00:12")),
        "end_us": ops.instant_us(_utc("2026-10-01T07:10:40")),
    }

    text = ops.render_text(outbox.KIND_OPS_GAP, payload, None, now=_utc("2026-10-01T07:10:41"))

    assert text == GAP


def test_render_text_all_silent_start() -> None:
    payload = {"since_us": ops.instant_us(_utc("2026-10-01T11:10:00")), "count": 3}

    text = ops.render_text(
        outbox.KIND_OPS_ALL_SILENT_START, payload, None, now=_utc("2026-10-01T11:11:05")
    )

    assert text == ALL_SILENT_START


@pytest.mark.django_db
def test_render_text_all_silent_end_reads_the_name_at_call_time(
    location_factory: Callable[..., Any],
) -> None:
    first = location_factory(name="Home")
    payload = {
        "since_us": ops.instant_us(_utc("2026-10-01T11:10:00")),
        "first_us": ops.instant_us(_utc("2026-10-01T11:13:00")),
    }
    # Renamed after the notice was queued: the payload holds no name.
    Location.objects.filter(pk=first.pk).update(name="Office")
    now = _utc("2026-10-01T11:13:01")

    assert ops.render_text(outbox.KIND_OPS_ALL_SILENT_END, payload, first.pk, now=now) == (
        ALL_SILENT_END
    )
    Location.objects.filter(pk=first.pk).update(name=RAW)
    escaped = ops.render_text(outbox.KIND_OPS_ALL_SILENT_END, payload, first.pk, now=now)
    raw = ops.render_text(
        outbox.KIND_OPS_ALL_SILENT_END, payload, first.pk, now=now, escape=False
    )
    assert ESCAPED in escaped and RAW not in escaped
    assert RAW in raw


@pytest.mark.django_db
def test_render_text_expired_uses_the_alert_row(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    alert = _alert(
        location, "power_off", _utc("2026-10-01T14:27:00"), _utc("2026-10-01T14:28:31")
    )

    text = ops.render_text(
        outbox.KIND_OPS_EXPIRED,
        {"message_id": alert.pk},
        location.pk,
        now=_utc("2026-10-01T20:30:00"),
    )

    # The max age is the row's own expires_at - recorded_at (6 h by default).
    assert text == EXPIRED


@pytest.mark.django_db
def test_render_text_uncertain_after_an_interrupted_send(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory(name="Office")
    alert = _alert(location, "power_on", _utc("2026-10-01T14:45:00"), _utc("2026-10-01T14:45:20"))
    OutboxMessage.objects.filter(pk=alert.pk).update(status="uncertain", last_error="interrupted")

    text = ops.render_text(
        outbox.KIND_OPS_UNCERTAIN,
        {"message_id": alert.pk},
        location.pk,
        now=_utc("2026-10-01T14:50:00"),
    )

    assert text == (
        "❓ ON alert for Office (event 17:45) may not have been delivered "
        "(the worker stopped while sending it). It will not be resent; please check the channel."
    )


@pytest.mark.django_db
def test_render_text_refuses_unknown_kinds_and_missing_rows(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory()
    now = _utc("2026-10-01T12:00:00")
    since = {"since_us": 0, "first_us": 1}
    with transaction.atomic():
        notice = outbox.enqueue_ops(outbox.KIND_OPS_GAP, payload={"start_us": 0}, recorded_at=now)

    with pytest.raises(ValueError, match="unknown ops notice kind"):
        ops.render_text("ops_nope", {}, None, now=now)
    # The referenced row must exist and be a subscriber alert.
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_EXPIRED, {"message_id": 10**9}, None, now=now)
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_EXPIRED, {"message_id": notice.pk}, None, now=now)
    # The first location of an all-silent end must exist.
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_ALL_SILENT_END, since, None, now=now)
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_ALL_SILENT_END, since, location.pk + 1000, now=now)
    # A missing key or a non-integer value is a broken payload.
    with pytest.raises(KeyError):
        ops.render_text(outbox.KIND_OPS_GAP, {"start_us": 0}, None, now=now)
    with pytest.raises(TypeError):
        ops.render_text(outbox.KIND_OPS_ALL_SILENT_START, {"since_us": 0, "count": "3"}, None, now=now)
