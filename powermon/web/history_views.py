"""The Phase 5 history pages: the removal of a false outage (DATA-02) and the reset of a
location's history (DATA-03; 05-UI-SPEC B, C, D, E).

- Every view here needs the signed-in admin: LoginRequiredMiddleware denies by default and
  none of them is ``login_not_required``.
- Each action is a GET confirmation, then a POST with CSRF (Phase 4 D-17). A GET never
  writes: when a check fails it redirects to the location page with the flash its POST
  would give, so no page offers a button the admin may not press (UI5-D7).
- The POST re-checks everything in the engine, under the location's row lock
  (``history.remove_outage``, ``history.reset_history``): a stale tab or a hand-made
  request can never remove an outage in progress (INV-07 #3) or reset a location during
  an outage (D-06).
- Every result is POST -> redirect -> GET to the location page, a fixed route and never a
  value from the request, so a reload never repeats an action (05-UI-SPEC D). A double
  submit gets an honest "nothing changed" flash: "already gone" for a removal (UI5-D8),
  "nothing to reset" for a reset (UI5-D9).
- An unknown or deleted location, or a start that is not a valid instant, answers 404,
  never 500 (05-UI-SPEC E).
- No network I/O (KD2): the worker's regular 15-minute chart refresh shows a removal
  (D-03), and its next I/O pass unpins a reset location's old chart in its own chat
  (D-08). The web never retires a chart record itself.
- Flashes hold fixed copy plus at most one time formatted from a stored instant (UI5-D15),
  never a value echoed from the request, a token or a key (SEC-04, OPS-08).
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from powermon.alerts import ops
from powermon.clock import Clock, SystemClock
from powermon.engine import history
from powermon.i18n.duration import format_total_duration
from powermon.web.fragments import confirm_response, refusal_redirect
from powermon.web.location_views import local_minute, location_or_404
from powermon.web.status import location_status

ONE_US = timedelta(microseconds=1)

# 06-UI-SPEC amendment A7, verbatim. {start} is "YYYY-MM-DD HH:MM" in the display TZ,
# formatted from the stored outage start (UI5-D15).
OUTAGE_REMOVED_MESSAGE = (
    "Outage from {start} removed: its time now counts as power on. The removal sent no message. "
    "If it is within the last 7 days, the pinned chart shows the change within 15 minutes."
)
REMOVAL_REFUSED_MESSAGE = "This outage is still in progress. It can be removed after power returns."
OUTAGE_GONE_MESSAGE = (
    "This outage is no longer in the history: it was already removed, or the history was "
    "reset. Nothing changed."
)
# 06-UI-SPEC amendment A4, verbatim (05-UI-SPEC's Removal deferred flash of W1-A1, now
# opening with the outcome): the outage's OFF alert is being sent; POST only.
REMOVAL_DEFERRED_MESSAGE = "Not removed: an alert about this outage is being sent to the channel right now. Try again in a minute."  # noqa: E501
# 05-UI-SPEC Copywriting › Flashes, verbatim.
HISTORY_RESET_MESSAGE = (
    "History reset. The location waits for its next heartbeat, which restarts monitoring "
    "without an alert. The old weekly chart is unpinned when the bot can do so; if the pin "
    "stays, unpin it by hand in Telegram."
)
RESET_REFUSED_MESSAGE = (
    "An outage is in progress. Reset the history after power returns, or delete the location."
)
NOTHING_TO_RESET_MESSAGE = "There is no power history to reset. Nothing changed."


def instant_or_404(start_us: int) -> datetime:
    """The outage start ``start_us`` microseconds after the Unix epoch, or 404.

    The URL converter already turns a value that is not digits, or has more digits than
    ``int()`` accepts, into no match (404); an integer out of the datetime range answers
    404 here instead of a 500 (05-UI-SPEC E).
    """
    try:
        return ops.from_instant_us(start_us)
    except ValueError, TypeError:
        raise Http404 from None


class OutageRemoveView(View):
    """``/locations/<pk>/outages/<start_us>/remove/``: remove a false outage (DATA-02).

    GET is the confirmation page (05-UI-SPEC screen B): the outage's Start, End and Off
    time, what the removal does, then one form, the destructive POST, next to "Keep
    outage". It writes nothing; an outage that is gone or in progress redirects to the
    location page with the POST's flash (UI5-D7). POST runs ``history.remove_outage`` (one
    transaction under the row lock, no network I/O) and redirects to the location page
    with the success, info or error flash for its result. While the outage's OFF alert is
    being sent the POST writes nothing and says so (info, "Removal deferred"); the GET does
    not check this, because the attempt settles within seconds (W1-A1). The 14-day window
    limits the list only: any ended outage of the location can be removed (UI5-D14).
    """

    template_name = "web/outage_remove.html"
    # Tests inject a FakeClock with OutageRemoveView.as_view(clock=...) or monkeypatch it.
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int, start_us: int) -> HttpResponse:
        """The confirmation page, or its partial alone for the modal (UI-07).

        A refusal is the same redirect in both variants; only the full page queues its flash.
        """
        location = location_or_404(pk)
        start = instant_or_404(start_us)
        outage = history.find_outage(pk, start, self.clock.now())
        if outage is None:
            return refusal_redirect(
                request, messages.INFO, OUTAGE_GONE_MESSAGE, "location-detail", pk=pk
            )
        if outage.in_progress or outage.end is None:
            return refusal_redirect(
                request, messages.ERROR, REMOVAL_REFUSED_MESSAGE, "location-detail", pk=pk
            )
        span_us = (outage.end - outage.start) // ONE_US
        context = {
            "location": location,
            # From the stored outage, never the request's value (05-UI-SPEC rule 5).
            "start_us": ops.instant_us(outage.start),
            "outage": outage,
            "off_text": format_total_duration(outage.off_us, "en"),
            # Not-monitored time lies inside the outage (05-UI-SPEC B, Consequence 2).
            "shows_unmonitored": outage.off_us < span_us,
        }
        return confirm_response(
            request, self.template_name, "web/_confirm_remove_outage.html", context
        )

    def post(self, request: HttpRequest, pk: int, start_us: int) -> HttpResponse:
        location_or_404(pk)
        start = instant_or_404(start_us)
        result = history.remove_outage(pk, start)
        if result == "removed":
            day, minute = local_minute(start, settings.TIME_ZONE)
            messages.success(request, OUTAGE_REMOVED_MESSAGE.format(start=f"{day} {minute}"))
        elif result == "in_progress":
            messages.error(request, REMOVAL_REFUSED_MESSAGE)
        elif result == "sending":
            messages.info(request, REMOVAL_DEFERRED_MESSAGE)
        else:
            # Deleted between the lookup and the row lock: 404, as for any deleted
            # location, never a flash (05-UI-SPEC E, as HistoryResetView.post).
            location_or_404(pk)
            messages.info(request, OUTAGE_GONE_MESSAGE)
        return redirect("location-detail", pk=pk)


class HistoryResetView(View):
    """``/locations/<pk>/reset/``: reset the location's history (DATA-03).

    GET is the confirmation page (05-UI-SPEC screen C): what the reset does, then one form,
    the destructive POST, next to "Keep history". It writes nothing; while the stored
    status is off (D-06), or when the location has no stored interval (UI5-D9), it
    redirects to the location page with the POST's flash (UI5-D7). POST runs
    ``history.reset_history`` (one transaction under the row lock, no network I/O) and
    redirects to the location page with the success, error or info flash for its result.
    """

    template_name = "web/history_reset.html"
    # Tests inject a FakeClock with HistoryResetView.as_view(clock=...) or monkeypatch it.
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        location = location_or_404(pk)
        if location_status(location).power_key == "off":
            messages.error(request, RESET_REFUSED_MESSAGE)
            return redirect("location-detail", pk=pk)
        if not history.has_history(pk):
            messages.info(request, NOTHING_TO_RESET_MESSAGE)
            return redirect("location-detail", pk=pk)
        return render(request, self.template_name, {"location": location})

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        location_or_404(pk)
        result = history.reset_history(pk, self.clock.now())
        if result == "gone":
            # Deleted between the lookup and the lock: as for any deleted location.
            raise Http404
        if result == "reset":
            # Instructive, so sticky (UI-09): it says what to do if the old pin stays.
            messages.success(request, HISTORY_RESET_MESSAGE, extra_tags="sticky")
        elif result == "in_progress":
            messages.error(request, RESET_REFUSED_MESSAGE)
        else:
            messages.info(request, NOTHING_TO_RESET_MESSAGE)
        return redirect("location-detail", pk=pk)
