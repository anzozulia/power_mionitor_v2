"""The ops notice texts: exact English strings in the display TZ (D-10, D-11, D-12).

The expected strings are the D-11/D-12 samples (02-CONTEXT "Specific Ideas") and the
chart pin notices of Phase 3 D-07 (03-CONTEXT "Specific Ideas"), with times computed by
hand for Europe/Kyiv (UTC+3 on 2026-10-01). Durations come from the shared alert
formatter, which prints an exact 3-minute span as "3m". Every text that names a location
HTML-escapes the name by default and leaves it raw with ``escape=False`` (the log form,
D-09). The all-silent texts stay neutral (D-12, Pitfall 11). A pin failure shows only a
short HTTP code, never Telegram's description.

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
# The chart pin notices (Phase 3 D-07, 03-CONTEXT "Specific Ideas").
PIN_FAILED = (
    "📌 Can't pin today's chart for Office (Telegram: http_400). "
    "The chart is still posted and refreshed; pinning is retried every 15 min. "
    "Check that the bot may pin messages in the chat."
)
PIN_RESTORED = "📌 Pinning works again for Office."


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

    text = ops_texts.uncertain("power_on", event, "Office", interrupted=False, now=now, tz=KYIV)

    assert text == UNCERTAIN


def test_uncertain_after_an_interrupted_send_names_the_worker_stop() -> None:
    event, now = _utc("2026-10-01T14:45:00"), _utc("2026-10-01T14:50:00")

    text = ops_texts.uncertain("power_off", event, "Office", interrupted=True, now=now, tz=KYIV)

    assert text == (
        "❓ OFF alert for Office (event 17:45) may not have been delivered "
        "(the worker stopped while sending it). It will not be resent; please check the channel."
    )


def test_pin_failed() -> None:
    assert ops_texts.pin_failed(400, "Office") == PIN_FAILED


def test_pin_failed_shows_the_status_it_is_given() -> None:
    assert "(Telegram: http_403)" in ops_texts.pin_failed(403, "Office")
    # The ends of the accepted range.
    assert "(Telegram: http_100)" in ops_texts.pin_failed(100, "Office")
    assert "(Telegram: http_599)" in ops_texts.pin_failed(599, "Office")


def test_pin_restored() -> None:
    assert ops_texts.pin_restored("Office") == PIN_RESTORED


# Names are escaped for Telegram HTML, and raw for the log (D-09, D-10)


def _named_texts(name: str, escape: bool) -> list[str]:
    at, now = _utc("2026-10-01T11:10:00"), _utc("2026-10-01T11:13:00")
    return [
        ops_texts.all_silent_end(at, now, name, KYIV, escape=escape),
        ops_texts.expired("power_off", at, timedelta(hours=6), name, now, KYIV, escape=escape),
        ops_texts.uncertain(
            "power_off", at, name, interrupted=False, now=now, tz=KYIV, escape=escape
        ),
        ops_texts.pin_failed(400, name, escape=escape),
        ops_texts.pin_restored(name, escape=escape),
    ]


def test_every_name_is_escaped_by_default() -> None:
    at, now = _utc("2026-10-01T11:10:00"), _utc("2026-10-01T11:13:00")
    texts = [
        ops_texts.all_silent_end(at, now, RAW, KYIV),
        ops_texts.expired("power_off", at, timedelta(hours=6), RAW, now, KYIV),
        ops_texts.uncertain("power_off", at, RAW, interrupted=False, now=now, tz=KYIV),
        ops_texts.pin_failed(400, RAW),
        ops_texts.pin_restored(RAW),
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


@pytest.mark.parametrize("status", [True, False, 99, 600, 1000, -400, "400", 400.0, None], ids=repr)
def test_pin_failed_refuses_a_status_that_is_not_a_short_http_code(status: Any) -> None:
    # Only a short "http_NNN" code may reach the text (OPS-08, D-07).
    with pytest.raises(ValueError, match="HTTP status"):
        ops_texts.pin_failed(status, "Office")


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
    raw = ops.render_text(outbox.KIND_OPS_ALL_SILENT_END, payload, first.pk, now=now, escape=False)
    assert ESCAPED in escaped and RAW not in escaped
    assert RAW in raw


@pytest.mark.django_db
def test_render_text_expired_uses_the_alert_row(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    alert = _alert(location, "power_off", _utc("2026-10-01T14:27:00"), _utc("2026-10-01T14:28:31"))

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
        ops.render_text(
            outbox.KIND_OPS_ALL_SILENT_START, {"since_us": 0, "count": "3"}, None, now=now
        )


@pytest.mark.django_db
def test_render_text_reads_the_pin_location_name_at_send_time(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory(name="Home")
    now = _utc("2026-10-01T12:00:00")
    failed: dict[str, Any] = {"http_status": 400}
    # Renamed after the notices were queued: the payloads hold no name.
    Location.objects.filter(pk=location.pk).update(name="Office")

    assert ops.render_text(outbox.KIND_OPS_PIN_FAILED, failed, location.pk, now=now) == (PIN_FAILED)
    assert ops.render_text(outbox.KIND_OPS_PIN_RESTORED, {}, location.pk, now=now) == (PIN_RESTORED)
    Location.objects.filter(pk=location.pk).update(name=RAW)
    for kind, payload in ((outbox.KIND_OPS_PIN_FAILED, failed), (outbox.KIND_OPS_PIN_RESTORED, {})):
        escaped = ops.render_text(kind, payload, location.pk, now=now)
        raw = ops.render_text(kind, payload, location.pk, now=now, escape=False)
        assert ESCAPED in escaped and RAW not in escaped
        assert RAW in raw and ESCAPED not in raw


@pytest.mark.django_db
def test_render_text_refuses_a_broken_pin_notice(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    now = _utc("2026-10-01T12:00:00")

    # The chart's location must exist.
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_PIN_FAILED, {"http_status": 400}, None, now=now)
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_PIN_RESTORED, {}, location.pk + 1000, now=now)
    # A missing, non-integer or implausible status is a broken payload.
    with pytest.raises(KeyError):
        ops.render_text(outbox.KIND_OPS_PIN_FAILED, {}, location.pk, now=now)
    with pytest.raises(TypeError):
        ops.render_text(outbox.KIND_OPS_PIN_FAILED, {"http_status": True}, location.pk, now=now)
    with pytest.raises(ValueError):
        ops.render_text(outbox.KIND_OPS_PIN_FAILED, {"http_status": 1000}, location.pk, now=now)


# The delivery notices (Phase 4 D-10, 04-CONTEXT "Specific Ideas")

DELIVERY_FAILING = (
    "🚫 Alerts for Office are failing (Telegram: http_403). They stay queued and are retried "
    "every 15 min until they expire after 6h. Check that the bot is an admin of the channel, "
    "then send a test message from the admin panel."
)
SUPERGROUP = (
    " The group became a supergroup; its new chat ID is -1009999999999. "
    "Update the location's chat ID."
)
DELIVERY_RESTORED = "✅ Alerts for Office are delivered again."


def test_delivery_failing() -> None:
    text = ops_texts.delivery_failing(403, "Office", timedelta(hours=6), None)

    assert text == DELIVERY_FAILING


def test_delivery_failing_names_the_supergroup_chat_id() -> None:
    text = ops_texts.delivery_failing(400, "Office", timedelta(hours=6), -1009999999999)

    assert text == DELIVERY_FAILING.replace("http_403", "http_400") + SUPERGROUP


def test_delivery_failing_shows_the_configured_maximum_age() -> None:
    text = ops_texts.delivery_failing(403, "Office", timedelta(hours=12), None)

    assert "until they expire after 12h. " in text


def test_delivery_restored() -> None:
    assert ops_texts.delivery_restored("Office") == DELIVERY_RESTORED


def test_delivery_texts_escape_the_name_for_telegram_only() -> None:
    texts = [
        ops_texts.delivery_failing(403, RAW, timedelta(hours=6), None),
        ops_texts.delivery_restored(RAW),
    ]
    raw = [
        ops_texts.delivery_failing(403, RAW, timedelta(hours=6), None, escape=False),
        ops_texts.delivery_restored(RAW, escape=False),
    ]

    assert all(ESCAPED in text and RAW not in text for text in texts)
    assert all(RAW in text and ESCAPED not in text for text in raw)


@pytest.mark.parametrize("status", [True, False, 99, 600, -403, "403", 403.0, None], ids=repr)
def test_delivery_failing_refuses_a_status_that_is_not_a_short_http_code(status: Any) -> None:
    with pytest.raises(ValueError, match="HTTP status"):
        ops_texts.delivery_failing(status, "Office", timedelta(hours=6), None)


@pytest.mark.parametrize("migrate_to", [True, "-1009999999999", -1009999999999.0], ids=repr)
def test_delivery_failing_refuses_a_chat_id_that_is_not_an_integer(migrate_to: Any) -> None:
    with pytest.raises(ValueError, match="chat ID"):
        ops_texts.delivery_failing(400, "Office", timedelta(hours=6), migrate_to)


@pytest.mark.django_db
def test_render_text_delivery_notices_read_the_name_and_max_age_at_send_time(
    location_factory: Callable[..., Any], settings: Any
) -> None:
    location = location_factory(name="Home")
    now = _utc("2026-10-01T12:00:00")
    # Renamed after the notices were queued: the payloads hold integers only.
    Location.objects.filter(pk=location.pk).update(name="Office")
    failing = outbox.KIND_OPS_DELIVERY_FAILING
    migrated = {"http_status": 400, "migrate_to_chat_id": -1009999999999}

    assert ops.render_text(failing, {"http_status": 403}, location.pk, now=now) == (
        DELIVERY_FAILING
    )
    assert ops.render_text(failing, migrated, location.pk, now=now) == (
        DELIVERY_FAILING.replace("http_403", "http_400") + SUPERGROUP
    )
    assert ops.render_text(outbox.KIND_OPS_DELIVERY_RESTORED, {}, location.pk, now=now) == (
        DELIVERY_RESTORED
    )
    settings.CFG = dataclasses.replace(settings.CFG, alert_max_age_hours=3)
    assert "expire after 3h. " in ops.render_text(
        failing, {"http_status": 403}, location.pk, now=now
    )


@pytest.mark.django_db
def test_render_text_refuses_a_broken_delivery_notice(location_factory: Callable[..., Any]) -> None:
    location = location_factory()
    now = _utc("2026-10-01T12:00:00")
    failing = outbox.KIND_OPS_DELIVERY_FAILING

    with pytest.raises(LookupError):
        ops.render_text(failing, {"http_status": 403}, None, now=now)
    with pytest.raises(LookupError):
        ops.render_text(outbox.KIND_OPS_DELIVERY_RESTORED, {}, location.pk + 1000, now=now)
    with pytest.raises(KeyError):
        ops.render_text(failing, {"migrate_to_chat_id": -100}, location.pk, now=now)
    # The optional chat ID is still an integer, or the payload is broken.
    for bad in (True, "-100", -100.0, None):
        payload = {"http_status": 400, "migrate_to_chat_id": bad}
        with pytest.raises(TypeError):
            ops.render_text(failing, payload, location.pk, now=now)
    with pytest.raises(TypeError):
        ops.render_text(failing, ["http_status"], location.pk, now=now)
    with pytest.raises(ValueError):
        ops.render_text(failing, {"http_status": 1000}, location.pk, now=now)
