"""The weekly chart preview: ``GET /locations/<pk>/chart.png`` (UI-06, D6-04).

The admin sees the exact chart the channel shows: the view renders today's live chart
through the channel's own path (``lifecycle.chart_content(..., live=True)``, the call the
worker makes), from the stored timeline only. It never sends, edits or pins a Telegram
message, and the worker and the chart lifecycle are untouched. The preview can be up to
one refresh newer than the pinned copy.

- Lazy imports: ``powermon.chart.lifecycle`` (and through it the Pillow renderer) is
  imported inside ``get()``, so importing ``powermon.urls`` loads neither, and a gunicorn
  worker loads Pillow only when a preview is first requested (Pitfall 6).
- 404 before the render: an unknown or soft-deleted location answers 404 without a
  render, so a deleted location's chart is never served.
- Rendered per request (about 0.16 s and 25 MB for a moment; one admin, a lazy image):
  there is no server cache, which a second gunicorn worker would not see anyway. The
  response is ``Cache-Control: no-cache, private`` with ``Vary: Cookie``, never public,
  so the browser asks again on every view and a removal or an edit shows at once
  (F-25, quick task 261008-vdk).
- The real bot token never enters this path: ``ChartLocation`` gets an empty one, and the
  PNG carries no text chunk (INV-23 #2).
"""

from datetime import timedelta

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.views import View

# The chart model is stdlib only (no Pillow, no Django model): safe at import time.
from powermon.chart import model
from powermon.clock import Clock, SystemClock
from powermon.web.location_views import location_or_404


class LocationChartView(View):
    """``GET /locations/<pk>/chart.png``: the location's weekly chart as the channel shows it.

    200 ``image/png`` for a non-deleted location (a location without stored history gets
    the all-no-data week), 404 otherwise. Login-required; every other method, HEAD
    included, answers 405. Writes nothing.
    """

    http_method_names = ["get"]
    # Tests inject a FakeClock with LocationChartView.as_view(clock=...).
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        # The 404 check comes first: a deleted location is never rendered.
        location = location_or_404(pk)
        # Here, not at module level: importing the URLconf must load neither the chart
        # lifecycle nor Pillow (tests/chart/test_lifecycle.py).
        from powermon.chart import lifecycle

        now = self.clock.now()
        tz = settings.TIME_ZONE
        chart = lifecycle.ChartLocation(
            location_id=location.pk,
            name=location.name,
            language=location.language,
            # chart_content reads only the id, the name and the language.
            bot_token="",
            chat_id=location.chat_id,
            settle=timedelta(0),
        )
        png, _caption = lifecycle.chart_content(
            chart, model.local_today(now, tz), now, live=True, tz=tz
        )
        response = HttpResponse(png, content_type="image/png")
        patch_cache_control(response, private=True, no_cache=True)
        patch_vary_headers(response, ("Cookie",))
        return response
