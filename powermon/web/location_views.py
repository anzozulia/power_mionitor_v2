"""The Phase 4 location pages: the location page, its one-click switches, the test message,
the edit form, the delete and the key rotation (D-13, D-05, D-11, D-07, D-09, D-14).

- Every view here needs the signed-in admin: LoginRequiredMiddleware denies by default and
  none of them is ``login_not_required``.
- Every switch is a POST with CSRF, answered POST -> redirect -> GET with a flash (UI-D4),
  so a reload never repeats it. The edit save is too. Delete and Regenerate key are a GET
  confirmation, then a POST (D-17): the delete redirects to the list, the regenerate
  answers with the revealed setup page itself (D-14), guarded against a resubmit (UI-D7).
- A GET of a switch or the test-message URL never acts (F-24): it redirects to the
  location page with a warning, so a stale tab's POST that went through sign-in does not
  end on a blank 405.
- The edit save writes only the configuration columns, through ``actions.update_config``,
  never ``form.save()`` or ``Location.save()``: a form loaded earlier can never revert the
  status, a switch or the device key (D-07, INV-02 #3).
- A switch posts its target value, never "toggle" (UI-D3): the same state again writes
  nothing and gets the "already" info flash, so a double click, a second tab or a stale page
  can never flip it back. Each switch changes exactly one flag (D-05).
- The test message is the only admin action with network I/O (KD2, D-11): one silent
  ``sendMessage`` with the location's current token and chat, no retry, the client's
  (5 s, 10 s) timeouts, never through the outbox. Views run in autocommit (no
  ``ATOMIC_REQUESTS``), so no transaction is open while it waits. Only after a success
  does one transaction record it (``delivery.record_test_success``, D-12); a failure
  never opens the delivery-failing incident (D-10). Its flash holds fixed copy, the
  client's short code and integers only, never Telegram's text or the token (OPS-08).
  It is POST -> redirect -> GET too, so a reload never sends it again (UI-D4).
- Every other view here does no network I/O.
- Every location URL answers 404 for an unknown or deleted location (UI-SPEC screen H).
- The location page lists the location's recent outages (Phase 5 D-01, 05-UI-SPEC A1),
  read from the stored timeline by ``history.recent_outages``; their removal lives in
  ``powermon.web.history_views``.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from powermon.alerts import delivery, ops
from powermon.clock import Clock, SystemClock
from powermon.engine import history, maintenance
from powermon.i18n import strings
from powermon.i18n.duration import format_total_duration
from powermon.locations import actions, examples, validators
from powermon.locations.models import LANGUAGE_CHOICES, Location
from powermon.telegram.client import DEFAULT_RETRY_AFTER_S, SendResult, TelegramClient
from powermon.web import views
from powermon.web.forms import LocationEditForm
from powermon.web.fragments import confirm_response
from powermon.web.status import failing_since_text, location_status

log = logging.getLogger(__name__)

LANGUAGE_LABELS = dict(LANGUAGE_CHOICES)
SWITCH_VALUES = ("on", "off")

# UI-D7: the regenerate form's marker is a salted HMAC (SECRET_KEY) of the key it replaces.
REGENERATE_SALT = "powermon.regenerate-key"
# UI-SPEC Copywriting › Device setup and key regeneration, verbatim.
REGENERATED_MESSAGE = (
    "New key saved. The old key no longer works. Copy the new key or an example below to "
    "the device."
)
ALREADY_REGENERATED_MESSAGE = "The key was already regenerated. The key below is the current one."

# UI-SPEC Copywriting › Edit form, verbatim.
CHANGES_SAVED_MESSAGE = "Changes saved."
CHANNEL_CHANGED_MESSAGE = (
    "Changes saved. The weekly chart is posted again with the new bot or chat. The old one "
    "is unpinned if this location's bot is an admin of the old channel."
)

# UI-SPEC Copywriting › Delete, verbatim.
LOCATION_DELETED_MESSAGE = (
    "Location deleted. Its alerts have stopped. Its weekly chart is unpinned when the bot can "
    "do so; if the pin stays, unpin it by hand in Telegram."
)
ALREADY_DELETED_MESSAGE = "This location was already deleted."

# UI-SPEC Copywriting › Test message, verbatim. {code} is the client's short code (e.g.
# http_403, read_timeout): never Telegram's description text, a URL or the token (D-11).
TEST_SENT_MESSAGE = "Test message sent. Check that it arrived in the channel."
TEST_RECOVERED_MESSAGE = (
    "Test message sent. Delivery is marked OK again, and any queued alerts go out next."
)
TEST_NOT_IN_CHAT_MESSAGE = (
    "Test message not sent ({code}): the bot is not in the chat, or the chat was not found. "
    "Make the bot an admin of the channel and check the chat ID."
)
TEST_BOT_REJECTED_MESSAGE = (
    "Test message not sent ({code}): Telegram rejected the bot token. Paste the current token "
    "from @BotFather in Edit location."
)
TEST_REFUSED_MESSAGE = (
    "Test message not sent ({code}): Telegram refused it. Check the bot token and the chat ID."
)
TEST_MAYBE_SENT_MESSAGE = (
    "No answer from Telegram in time ({code}). The message may have been sent: check the "
    "channel before you try again."
)
TEST_UNREACHABLE_MESSAGE = (
    "Telegram could not be reached ({code}), so the test message was not sent. Try again in a "
    "minute."
)
# 06-UI-SPEC amendment A1, verbatim: Telegram answered with an HTTP 5xx (``transient``).
TEST_SERVER_ERROR_MESSAGE = (
    "Telegram had a server error ({code}), so the test message was not sent. Try again in a minute."
)
# {wait} is "1 second" or "{N} seconds", N an integer.
TEST_RATE_LIMITED_MESSAGE = "Telegram asks to wait before the next message. Try again in {wait}."
# F-24: a GET of an action URL (the sign-in redirect after an ended session, a stale tab)
# never acts; the location page says so.
ACTION_NOT_DONE_MESSAGE = "Nothing was changed. If you were signed out, use the button again."
# The permanent codes with their own cause (the other permanent codes get the refused copy).
NOT_IN_CHAT_CODES = ("http_400", "http_403")
BAD_TOKEN_CODES = ("http_401", "http_404")

# UI-SPEC Copywriting › List and status, verbatim: the location page's Delivery row (D-13).
DELIVERY_OK_HELP = "No alert has been refused by Telegram since the last successful send."
DELIVERY_NOT_IN_CHAT_CAUSE = (
    "Telegram did not accept the chat: the chat ID is wrong, or the bot is not in that chat. "
    "Check the chat ID in Edit location, then send a test message."
)
DELIVERY_BOT_REJECTED_CAUSE = (
    "Telegram rejected the bot token. Paste the current token from @BotFather in Edit "
    "location, then send a test message."
)
DELIVERY_CANNOT_POST_CAUSE = (
    "The bot cannot post in the channel: it was removed or is not an admin. Make the bot an "
    "admin with the right to post messages, then send a test message."
)
DELIVERY_OTHER_CAUSE = (
    "Telegram refused the alerts. Check the bot token and the chat ID in Edit location, then "
    "send a test message."
)
# The cause line for each HTTP status with its own copy; any other status gets the other.
DELIVERY_CAUSES = {
    400: DELIVERY_NOT_IN_CHAT_CAUSE,
    401: DELIVERY_BOT_REJECTED_CAUSE,
    403: DELIVERY_CANNOT_POST_CAUSE,
    404: DELIVERY_BOT_REJECTED_CAUSE,
}
# Replaces the cause line when Telegram reported the supergroup's chat ID (D-10).
DELIVERY_MIGRATE_LINE = (
    "The group became a supergroup. Its new chat ID is {new_chat_id}: put it in Edit location, "
    "then send a test message."
)
DELIVERY_RETRY_LINE = "Queued alerts are retried every 15 minutes until they expire."

# UI-SPEC Copywriting › Switches, verbatim.
MAINTENANCE_HELP = (
    "While on, OFF is not detected, so no OFF alert is sent, and the chart shows the time as "
    "not monitored. Heartbeats are still recorded: if an outage was already in progress, its "
    "ON alert is sent as usual when power returns. Turning maintenance off starts a fresh "
    "detection window."
)
MAINTENANCE_COPY = {
    "on": (
        "Maintenance is on. OFF is not detected and no OFF alert is sent; the chart shows "
        "this time as not monitored."
    ),
    "off": (
        "Maintenance is off. OFF detection starts again now; silence during maintenance does "
        "not count."
    ),
    "already_on": "Maintenance was already on. Nothing changed.",
    "already_off": "Maintenance was already off. Nothing changed.",
}
ALERTS_HELP = (
    "While off, subscribers get no new alerts, and none are saved for later. Alerts already "
    "queued still go out. The chart, its regular updates and the midnight re-pin carry on."
)
ALERTS_COPY = {
    "on": "Alerts are on. Subscribers get alerts for changes recorded from now on.",
    "off": (
        "Alerts are off. Subscribers get no new alerts; alerts already queued still go out. "
        "The chart keeps updating."
    ),
    "already_on": "Alerts were already on. Nothing changed.",
    "already_off": "Alerts were already off. Nothing changed.",
}
ROUTER_GRACE_HELP = (
    "While on, OFF waits 180 seconds longer when the last heartbeat came within 5 minutes "
    "after power returned, so a router that restarts after a blackout is not reported as a "
    "second outage. It changes only decisions made from now on."
)
# "off" names the location's plain timeout: {off_after_s} is period + grace, an integer.
ROUTER_GRACE_COPY = {
    "on": (
        "Router grace is on. From now on, OFF waits 180 seconds longer right after power returns."
    ),
    "off": (
        "Router grace is off. From now on, OFF is reported after {off_after_s} seconds "
        "without a heartbeat."
    ),
    "already_on": "Router grace was already on. Nothing changed.",
    "already_off": "Router grace was already off. Nothing changed.",
}


def location_or_404(pk: int) -> Location:
    """The location with its state row, or 404 when it is unknown or deleted."""
    return get_object_or_404(
        Location.objects.select_related("state"), pk=pk, deleted_at__isnull=True
    )


def action_not_done(request: HttpRequest, pk: int) -> HttpResponse:
    """A GET of a switch or test-message URL: change nothing, warn, back to the page (F-24).

    After a session ends, the sign-in form sends the admin back to the action URL with a
    GET (its ``next``). That GET never acts: no switch changes and no Telegram message is
    sent. An unknown or deleted location answers 404, like the POST.
    """
    location_or_404(pk)
    messages.warning(request, ACTION_NOT_DONE_MESSAGE)
    return redirect("location-detail", pk=pk)


def settings_context(location: Location) -> dict[str, Any]:
    """The values of the shared settings list (``partials/_settings_dl.html``) on S5 and S8.

    The bot token only ever goes out masked (SEC-04, D-11).
    """
    # Only the public bot id before the colon, the digits the mask shows, enters the
    # context; never the secret part (R3). A token without a colon has no public part.
    bot_id, colon, _secret = location.bot_token.partition(":")
    return {
        "language_label": LANGUAGE_LABELS[location.language],
        "period_s": location.period_s,
        "grace_s": location.grace_s,
        "off_after_s": location.period_s + location.grace_s,
        "masked_token": validators.mask_token(location.bot_token),
        "token_bot_id": bot_id if colon else "",
    }


@dataclass(frozen=True)
class SwitchRow:
    """One row of the location page's Switches list, in its current state."""

    # The URL name the row's form posts to.
    url_name: str
    # The current state, e.g. "Maintenance is off".
    heading: str
    # The switch's single effect, the same in both states.
    help: str
    # The action, e.g. "Turn maintenance on".
    button: str
    # The value the form posts: the state the switch moves to (UI-D3).
    target: str


def switch_rows(location: Location) -> list[SwitchRow]:
    """The location page's switches, in UI-SPEC order (D-05): Maintenance, Alerts, Router grace."""
    maintenance_on = location.maintenance
    alerts_on = location.alerts_enabled
    grace_on = location.router_grace
    return [
        SwitchRow(
            url_name="location-maintenance",
            heading="Maintenance is on" if maintenance_on else "Maintenance is off",
            help=MAINTENANCE_HELP,
            button="Turn maintenance off" if maintenance_on else "Turn maintenance on",
            target="off" if maintenance_on else "on",
        ),
        SwitchRow(
            url_name="location-alerts",
            heading="Alerts are on" if alerts_on else "Alerts are off",
            help=ALERTS_HELP,
            button="Turn alerts off" if alerts_on else "Turn alerts on",
            target="off" if alerts_on else "on",
        ),
        SwitchRow(
            url_name="location-router-grace",
            heading="Router grace is on" if grace_on else "Router grace is off",
            help=ROUTER_GRACE_HELP,
            button="Turn router grace off" if grace_on else "Turn router grace on",
            target="off" if grace_on else "on",
        ),
    ]


class SwitchView(View):
    """POST ``value=on|off``: set one switch of the location, then back to its page.

    A missing or unknown value answers 400 with an empty body and changes nothing (only a
    hand-made request can send one). A GET changes nothing and redirects to the location
    page with the ACTION_NOT_DONE_MESSAGE warning (F-24). Every other method answers 405.
    """

    http_method_names = ["get", "post"]
    # Tests inject a FakeClock with SomeSwitchView.as_view(clock=...).
    clock: Clock = SystemClock()
    # The flashes: "on" / "off" after a change, "already_on" / "already_off" when not.
    copy: ClassVar[dict[str, str]] = {}

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        """Set the switch; True if it changed, False if it already had that value."""
        raise NotImplementedError

    def flash(self, location: Location, key: str) -> str:
        """The flash for ``key`` ("on", "off", "already_on", "already_off")."""
        return self.copy[key]

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return action_not_done(request, pk)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        value = request.POST.get("value")
        if value not in SWITCH_VALUES:
            return HttpResponseBadRequest()
        location = location_or_404(pk)
        if self.apply(pk, value == "on", self.clock.now()):
            messages.success(request, self.flash(location, value))
        else:
            messages.info(request, self.flash(location, f"already_{value}"))
        return redirect("location-detail", pk=pk)


class MaintenanceSwitchView(SwitchView):
    """``/locations/<pk>/maintenance/``: the maintenance switch (LOC-08, D-02).

    The engine owns the timeline write: the view only calls ``maintenance.set_maintenance``.
    """

    copy = MAINTENANCE_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return maintenance.set_maintenance(pk, on, now)


class AlertsSwitchView(SwitchView):
    """``/locations/<pk>/alerts/``: the alerts switch (LOC-10, D-05, D-06).

    A configuration-only write (``actions.set_flag``). With alerts off, a transition
    recorded from then on queues no alert and nothing is held for later; alerts already
    queued still go out, and the chart carries on (INV-05).
    """

    copy = ALERTS_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return actions.set_flag(pk, "alerts_enabled", on)


class RouterGraceSwitchView(SwitchView):
    """``/locations/<pk>/router-grace/``: the router-reconnect grace switch (LOC-09, D-05).

    A configuration-only write (``actions.set_flag``): no state row lock and no
    ``state_version`` bump, so a detector snapshot read before the click keeps its
    decision, and every cycle after it reads the new setting (K-4). An OFF already
    recorded keeps its start and its totals (INV-05, INV-06).
    """

    copy = ROUTER_GRACE_COPY

    def apply(self, pk: int, on: bool, now: datetime) -> bool:
        return actions.set_flag(pk, "router_grace", on)

    def flash(self, location: Location, key: str) -> str:
        # Integers only in a flash: the location's period + grace in seconds.
        return self.copy[key].format(off_after_s=location.period_s + location.grace_s)


def flash_for_test_message(result: SendResult, recovered: bool) -> tuple[int, str]:
    """The test message's flash (level, text) for its result (D-11, UI-SPEC Test message).

    ``recovered`` is True when the success closed an open delivery-failing incident (D-12).
    - ok: success, "sent", or "sent while delivery was failing" when ``recovered``;
    - maybe_delivered (no answer in time): warning, check the channel before a retry;
    - not_sent: error, Telegram unreachable;
    - transient (HTTP 5xx): error, Telegram had a server error (06-UI-SPEC A1);
    - rate_limited: warning, with the wait in whole seconds ("1 second" when it is 1);
    - permanent: error, the cause for 400/403 (not in the chat), 401/404 (bad token) or
      any other code (refused). ``edit_target_missing``, which sendMessage never returns,
      reads as refused too.
    The text is fixed copy plus the client's short code and integers: never Telegram's
    text, a URL or the token (OPS-08).
    """
    if result.kind == "ok":
        return messages.SUCCESS, TEST_RECOVERED_MESSAGE if recovered else TEST_SENT_MESSAGE
    if result.kind == "maybe_delivered":
        return messages.WARNING, TEST_MAYBE_SENT_MESSAGE.format(code=result.code)
    if result.kind == "not_sent":
        return messages.ERROR, TEST_UNREACHABLE_MESSAGE.format(code=result.code)
    if result.kind == "transient":
        return messages.ERROR, TEST_SERVER_ERROR_MESSAGE.format(code=result.code)
    if result.kind == "rate_limited":
        seconds = result.retry_after or DEFAULT_RETRY_AFTER_S
        wait = "1 second" if seconds == 1 else f"{seconds} seconds"
        return messages.WARNING, TEST_RATE_LIMITED_MESSAGE.format(wait=wait)
    if result.code in NOT_IN_CHAT_CODES:
        return messages.ERROR, TEST_NOT_IN_CHAT_MESSAGE.format(code=result.code)
    if result.code in BAD_TOKEN_CODES:
        return messages.ERROR, TEST_BOT_REJECTED_MESSAGE.format(code=result.code)
    return messages.ERROR, TEST_REFUSED_MESSAGE.format(code=result.code)


class SendTestMessageView(View):
    """``/locations/<pk>/test-message/``: send the admin's test message (LOC-07, D-11, D-12).

    Only a POST (CSRF) sends. A GET sends nothing and redirects to the location page with
    the ACTION_NOT_DONE_MESSAGE warning (F-24). Every other method answers 405, and an
    unknown or deleted location 404. The view makes exactly one ``sendMessage`` with the
    location's current token and chat: the fixed D-11 text in the location's language,
    sent silently (``disable_notification``), whatever the switches say. It is not an
    alert: it never goes through the outbox and is never retried; the client's (5 s,
    10 s) timeouts bound the wait. Views run in autocommit, so no transaction is open
    during the call.

    After an ``ok`` only, one transaction records the success at the clock's time
    (``delivery.record_test_success``): the location's queued alerts become due, and an
    open delivery-failing incident closes with one recovery notice, so the worker lifts
    its hold of the channel in its next pass (D-12). A failure changes nothing and never
    opens the incident (D-10): its short cause is shown in the flash only. Then a
    redirect to the location page (UI-D4), so a reload never sends a second message.
    """

    http_method_names = ["get", "post"]
    # Tests inject a FakeClock with SendTestMessageView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return action_not_done(request, pk)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        result = TelegramClient(location.bot_token).send_message(
            location.chat_id,
            strings.telegram_test_text(location.language),
            disable_notification=True,
        )
        # The success is recorded once the answer is in, at that time (D-12).
        recovered = result.kind == "ok" and delivery.record_test_success(pk, self.clock.now())
        # The id, the kind and the short code only: never the token or a chat ID (OPS-08).
        log.info("test message for location %s: %s (%s)", pk, result.kind, result.code or "-")
        level, text = flash_for_test_message(result, recovered)
        messages.add_message(request, level, text)
        return redirect("location-detail", pk=pk)


@dataclass(frozen=True)
class DeliveryRow:
    """The status panel's Delivery row (D-13): "OK", or failing since a time, with help."""

    # When the open delivery_failing incident started; None while delivery is OK.
    failing_since: datetime | None
    # The short code of the refusal the incident describes, e.g. "http_403"; "" while OK.
    code: str
    # The help lines under the value, in order.
    lines: tuple[str, ...]


def delivery_row(location_id: int) -> DeliveryRow:
    """The location's delivery health for its page, from its open failing incident (D-10).

    OK: its help line. Failing: the cause line for the HTTP status the incident describes
    (the latest refusal), or the supergroup line with the new chat ID when Telegram
    reported one, then the retry line. The chat ID is only shown, never applied.
    """
    failing = delivery.failing_incidents([location_id]).get(location_id)
    if failing is None:
        return DeliveryRow(failing_since=None, code="", lines=(DELIVERY_OK_HELP,))
    if failing.migrate_to_chat_id is not None:
        cause = DELIVERY_MIGRATE_LINE.format(new_chat_id=failing.migrate_to_chat_id)
    else:
        cause = DELIVERY_CAUSES.get(failing.http_status, DELIVERY_OTHER_CAUSE)
    return DeliveryRow(
        failing_since=failing.started_at,
        code=f"http_{failing.http_status}",
        lines=(cause, DELIVERY_RETRY_LINE),
    )


# The Weekly chart card's warning while the channel's chart fails (F-04). Fixed copy: never
# the location name or Telegram's description, only the short status code and the time.
CHART_FAILING_TITLE = "The channel's chart is not being updated"
CHART_FAILING_BODY = (
    "Telegram refused to post or update it (http_{status}) since {since}. It is retried "
    "every 15 min. Check that the bot is an admin of the channel and may post photos."
)
CHART_PIN_TITLE = "Today's chart is not pinned"
CHART_PIN_BODY = (
    "Telegram refused the pin (http_{status}) since {since}. "
    "Check that the bot may pin messages in the channel."
)


def chart_trouble_alert(location_id: int, now: datetime, tz: str) -> tuple[str, str] | None:
    """The Weekly chart card's warning (title, body), or None while the chart is fine (F-04).

    The time is ``failing_since_text``'s: ``HH:MM`` today, else the date too.
    """
    trouble = delivery.chart_trouble(location_id)
    if trouble is None:
        return None
    since = failing_since_text(trouble.started_at, now, tz)
    if trouble.kind == delivery.KIND_CHART_FAILING:
        body = CHART_FAILING_BODY.format(status=trouble.http_status, since=since)
        return CHART_FAILING_TITLE, body
    return CHART_PIN_TITLE, CHART_PIN_BODY.format(status=trouble.http_status, since=since)


def local_minute(dt: datetime, tz: str) -> tuple[str, str]:
    """``("YYYY-MM-DD", "HH:MM")`` of the instant ``dt`` in the display TZ ``tz`` (UI5-D4).

    Seconds are cut off, never rounded. A naive ``dt`` has no defined instant: ValueError.
    """
    if dt.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    local = dt.astimezone(ZoneInfo(tz))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


@dataclass(frozen=True)
class OutageRow:
    """One row of the location page's Recent outages table (05-UI-SPEC A1, UI5-D2…D6)."""

    # The outage start as integer microseconds since the Unix epoch: its URL identity (UI5-D5).
    start_us: int
    # The start in the display TZ, cut to the minute (UI5-D4).
    start_date: str
    start_time: str
    # The end's date only when it falls on another local date than the start, else "";
    # end_date and end_time are both "" while the outage is in progress.
    end_date: str
    end_time: str
    in_progress: bool
    # The off time in the chart's totals format, e.g. "1h 30m", "<1m" (UI5-D3).
    off_text: str


def outage_rows(outages: Iterable[history.Outage], tz: str) -> list[OutageRow]:
    """The Recent outages rows of ``outages``, in their order, with times in ``tz``."""
    rows = []
    for outage in outages:
        start_date, start_time = local_minute(outage.start, tz)
        end_date = end_time = ""
        if outage.end is not None:
            end_date, end_time = local_minute(outage.end, tz)
            if end_date == start_date:
                end_date = ""
        rows.append(
            OutageRow(
                start_us=ops.instant_us(outage.start),
                start_date=start_date,
                start_time=start_time,
                end_date=end_date,
                end_time=end_time,
                in_progress=outage.in_progress,
                off_text=format_total_duration(outage.off_us, "en"),
            )
        )
    return rows


def outages_total_text(outages: Iterable[history.Outage]) -> str:
    """The summed off time of the listed outages in the chart's totals format, e.g.
    "1h 35m"; "" when none is listed. Templates never add durations themselves."""
    off_us = [outage.off_us for outage in outages]
    if not off_us:
        return ""
    return format_total_duration(sum(off_us), "en")


class LocationDetailView(View):
    """``/locations/<pk>/``: the location page (UI-SPEC screen B, D-13; 05-UI-SPEC A1).

    Read-only: the status with the delivery health, the switches, the test message, the
    recent outages (Phase 5 D-01), the settings and the way to the device setup. It never
    shows the device key, not even masked, and the bot token only masked (SEC-04).
    """

    template_name = "web/location_detail.html"
    # Tests inject a FakeClock with LocationDetailView.as_view(clock=...) or monkeypatch it:
    # "now" bounds the recent outages, counts an open off piece's time and is the page's
    # "now" for its relative times (UI-11).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        tz = settings.TIME_ZONE
        # One reading of the clock for the whole page.
        now = self.clock.now()
        recent = history.recent_outages(location.pk, now, tz)
        rows = outage_rows(recent.outages, tz)
        context = {
            "location": location,
            "status": location_status(location),
            "delivery": delivery_row(location.pk),
            # The Weekly chart card's warning while the chart fails (F-04).
            "chart_trouble": chart_trouble_alert(location.pk, now, tz),
            "switch_rows": switch_rows(location),
            "outage_rows": rows,
            "outages_total_text": outages_total_text(recent.outages),
            "has_history": recent.has_history,
            "outage_in_progress": any(row.in_progress for row in rows),
            # The {% relative_time %} tags read it (UI-11).
            "now": now,
            # The device-setup card's URL: the configured base URL only, never the
            # request's Host header (R13), the same value as on the setup page.
            "heartbeat_url": examples.heartbeat_url(settings.PUBLIC_BASE_URL),
            **settings_context(location),
        }
        return render(request, self.template_name, context)


def stored_settings(location: Location) -> dict[str, Any]:
    """The edit form's initial values: the stored configuration, never the bot token."""
    return {
        "name": location.name,
        "period_s": location.period_s,
        "grace_s": location.grace_s,
        "chat_id": location.chat_id,
        "language": location.language,
        "chart_refresh_min": location.chart_refresh_min,
    }


class LocationEditView(View):
    """``/locations/<pk>/edit/``: edit the location's settings (LOC-04, D-07, D-08; screen C).

    GET shows the form with the stored values; "New bot token" is always empty and its
    help shows the current token masked (SEC-04). An invalid POST answers 200 with the
    form, every value kept except the token; the title and the breadcrumbs keep the stored
    name. A valid POST is one ``actions.update_config``, the view's only write, then a
    redirect to the location page with "Changes saved." (UI-D4), extended by the channel
    sentence when the chat ID or the token changed (D-08): the save made that location's
    queued alerts due, and the worker moves the chart. No network I/O (KD2).
    """

    template_name = "web/location_edit.html"
    # Tests inject a FakeClock with LocationEditView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        form = LocationEditForm(initial=stored_settings(location), current_token=location.bot_token)
        return self._render(request, location, form)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        form = LocationEditForm(request.POST, current_token=location.bot_token)
        if not form.is_valid():
            return self._render(request, location, form)
        result = actions.update_config(pk, form.cleaned_data, self.clock.now())
        if not result.found:
            # Deleted between the lookup and the save: nothing was written.
            raise Http404
        # The id and a flag only: never a token or a chat ID (OPS-08).
        log.info("settings saved for location %s (channel changed: %s)", pk, result.channel_changed)
        # A chat or token change also moves the chart: the flash says so (D-08), and it
        # stays until dismissed (UI-09: an instructive success is sticky).
        if result.channel_changed:
            messages.success(request, CHANNEL_CHANGED_MESSAGE, extra_tags="sticky")
        else:
            messages.success(request, CHANGES_SAVED_MESSAGE)
        return redirect("location-detail", pk=pk)

    def _render(
        self, request: HttpRequest, location: Location, form: LocationEditForm
    ) -> HttpResponse:
        return render(request, self.template_name, {"location": location, "form": form})


class LocationDeleteView(View):
    """``/locations/<pk>/delete/``: delete the location (LOC-04, D-09, D-17; screen D).

    GET is the confirmation page: it changes nothing and holds one form, the destructive
    POST, next to "Keep location"; 404 for an unknown or deleted location. POST runs
    ``actions.delete_location`` (one transaction under the row lock, no network I/O, KD2)
    and redirects to the list: with the success flash, or, for a location that is already
    deleted (a double click, a second tab), with the UI-D8 info flash and nothing changed.
    A POST for an id that never existed answers 404. The worker then unpins the location's
    charts in their stored chats; the flash says so without promising a time.
    """

    template_name = "web/location_delete.html"
    # Tests inject a FakeClock with LocationDeleteView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """The confirmation page, or its partial alone for the modal (UI-07, never_cache)."""
        location = location_or_404(pk)
        return confirm_response(
            request, self.template_name, "web/_confirm_delete.html", {"location": location}
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        # Deleted or not: only an id that never existed is 404 (UI-D8).
        get_object_or_404(Location.objects.only("pk"), pk=pk)
        if actions.delete_location(pk, self.clock.now()):
            # The id only (OPS-08).
            log.info("location %s deleted", pk)
            # Instructive, so sticky (UI-09): it says what to do if the pin stays.
            messages.success(request, LOCATION_DELETED_MESSAGE, extra_tags="sticky")
        else:
            messages.info(request, ALREADY_DELETED_MESSAGE)
        return redirect("location-list")


def regenerate_marker(key: str) -> str:
    """The UI-D7 marker of ``key``: a salted HMAC-SHA256 (hex), with no key characters.

    The confirmation form carries it, and the POST regenerates only while it is still the
    marker of the location's current key. It is exact (unlike the last 4 characters, which
    two keys can share) and reveals nothing about the key (T-04-18).
    """
    return salted_hmac(REGENERATE_SALT, key, algorithm="sha256").hexdigest()


def regenerate_block(location: Location) -> str:
    """The one D-15 state block of the regenerate confirmation (UI-SPEC screen F).

    "maintenance" whenever the flag is on; else by the stored status: "warning" (on: an
    OFF can be recorded while the device has the old key), "off" or "waiting".
    """
    if location.maintenance:
        return "maintenance"
    power_key = location_status(location).power_key
    return {"on": "warning", "off": "off"}.get(power_key, "waiting")


@method_decorator(never_cache, name="dispatch")
class RegenerateKeyView(View):
    """``/locations/<pk>/setup/regenerate/``: rotate the device key (LOC-06, D-14, D-15, D-17).

    GET is the confirmation page (UI-SPEC screen F): it changes nothing, shows exactly one
    D-15 state block and one form, the destructive POST, and never the key, not even masked.
    The form's hidden marker is ``regenerate_marker`` of the key it replaces (UI-D7).

    POST regenerates only while the posted marker is the current key's
    (``constant_time_compare``) and the conditional UPDATE still finds that key
    (``actions.regenerate_key``). Either way it answers with the setup page revealed:
    200, ``Cache-Control: no-store``, with the new key and the success flash, or, for a
    resubmitted, stale or raced POST, with the current key and the UI-D7 info flash. So a
    reload or a double click never replaces the key a second time, and the response is
    the only one besides Reveal that carries the full key (SEC-04). No network I/O (KD2).
    """

    template_name = "web/location_regenerate.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """The confirmation page, or its partial alone for the modal (UI-07): no key (R4)."""
        location = location_or_404(pk)
        context = {
            "location": location,
            # Not "block": inside {% block %} the template engine binds that name itself.
            "state_block": regenerate_block(location),
            "off_after_s": location.period_s + location.grace_s,
            "marker": regenerate_marker(location.device_key),
        }
        return confirm_response(
            request, self.template_name, "web/_confirm_regenerate.html", context
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        current = location_or_404(pk).device_key
        posted = request.POST.get("marker", "")
        if constant_time_compare(posted, regenerate_marker(current)) and actions.regenerate_key(
            pk, current
        ):
            # The id only: never a key (OPS-08).
            log.info("device key regenerated for location %s", pk)
            # Instructive, so sticky (UI-09): the device needs the new key.
            messages.success(request, REGENERATED_MESSAGE, extra_tags="sticky")
        else:
            messages.info(request, ALREADY_REGENERATED_MESSAGE)
        return views.render_setup(request, pk, revealed=True)
