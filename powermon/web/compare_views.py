"""The admin's Compare page, ``/compare/`` (quick task 261103-lfm, D-1, D-2, D-5).

GET only, for the signed-in admin (the default-deny ``LoginRequiredMiddleware``): any
other method answers 405. The page reads the stored timeline and writes, sends and queues
nothing.

- The filters are one GET search form bound to ``request.GET`` (``CompareForm``), so a
  comparison is a bookmarkable URL. ``loc`` (repeatable) holds the selected location ids;
  no ``loc`` in the query selects every location, and the form's hidden empty ``loc``
  sentinel makes "every chip unchecked" an error instead of "all". ``range`` is a preset
  (``7d`` by default, ``14d``, ``30d``, ``90d``: n days ending today in the display TZ) or
  ``custom`` with ``from`` and ``to`` (both required, ``from <= to``, ``to <= today``, at most
  366 days, from 01.01.2026). Every bad input is a field error on a 200 page, never a 500.
- With a valid form the page reads the live rows (2 queries) and the selected locations'
  pieces and whole outages (``compare_data.load_series``, 2 queries in one snapshot), runs
  the pure ``compare.compare`` and renders the view models of ``compare_chart``.
- With no location at all the page shows the empty state and no form.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from django import forms
from django.conf import settings
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import render
from django.views import View

from powermon.chart import compare, model
from powermon.clock import Clock, SystemClock
from powermon.web import compare_chart, compare_data
from powermon.web.forms import LocationBoundField
from powermon.web.live import LiveRow, live_rows

FORM_ERROR = "Check the filters marked below."
INVALID_LINE = "Fix the filters to see the comparison."
DEFAULT_RANGE = "7d"
CUSTOM = "custom"
RANGE_CHOICES = (
    ("7d", "7 days"),
    ("14d", "14 days"),
    ("30d", "30 days"),
    ("90d", "90 days"),
    (CUSTOM, "Custom"),
)

PICK_ONE = "Pick at least one location."
PICK_FROM_LIST = "Pick locations from the list."
PICK_RANGE = "Choose a period from the list."
ENTER_FROM = "Enter a start date."
ENTER_TO = "Enter an end date."
BAD_FROM = "Enter a valid start date."
BAD_TO = "Enter a valid end date."
TO_BEFORE_FROM = "The end date must not be before the start date."
TO_IN_FUTURE = "The end date cannot be in the future."
TOO_LONG = f"Pick at most {compare.MAX_SPAN_DAYS} days."
TOO_EARLY = f"Pick a start date on or after {compare.EARLIEST:%d.%m.%Y}."


class CompareForm(forms.Form):
    """The Compare filters (D-2), bound to the query string; fields loc, range, from, to.

    ``from`` is a Python keyword, so ``from`` and ``to`` are added in ``__init__``. A valid
    form's ``cleaned_data`` also holds ``first`` and ``last``, the period's local dates.
    """

    bound_field_class = LocationBoundField
    form_error = FORM_ERROR

    loc = forms.MultipleChoiceField(
        label="Locations", required=False, error_messages={"invalid_choice": PICK_FROM_LIST}
    )
    range = forms.ChoiceField(
        label="Period",
        required=False,
        choices=RANGE_CHOICES,
        error_messages={"invalid_choice": PICK_RANGE},
    )

    def __init__(self, data: QueryDict, *, rows: Sequence[LiveRow], today: date) -> None:
        super().__init__(data)
        self.today = today
        loc = cast(forms.MultipleChoiceField, self.fields["loc"])
        loc.choices = [(str(row.pk), row.name) for row in rows]
        self.fields["from"] = forms.DateField(
            label="From", required=False, error_messages={"invalid": BAD_FROM}
        )
        self.fields["to"] = forms.DateField(
            label="To", required=False, error_messages={"invalid": BAD_TO}
        )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if "loc" in data and not data["loc"]:
            self.add_error("loc", PICK_ONE)
        key = data.get("range") or DEFAULT_RANGE
        data["range"] = key
        if "range" in self.errors:
            return data
        if key != CUSTOM:
            data["first"], data["last"] = compare.preset_dates(key, self.today)
            return data
        first, last = data.get("from"), data.get("to")
        if first is None and "from" not in self.errors:
            self.add_error("from", ENTER_FROM)
        if last is None and "to" not in self.errors:
            self.add_error("to", ENTER_TO)
        if last is not None and last > self.today:
            self.add_error("to", TO_IN_FUTURE)
        elif first is not None and last is not None and last < first:
            self.add_error("to", TO_BEFORE_FROM)
        if first is not None and first < compare.EARLIEST:
            self.add_error("from", TOO_EARLY)
        elif (
            first is not None
            and last is not None
            and last >= first
            and (last - first).days >= compare.MAX_SPAN_DAYS
        ):
            self.add_error("from", TOO_LONG)
        if first is not None and last is not None:
            data["first"], data["last"] = first, last
        return data


def _query(request: HttpRequest, rows: Sequence[LiveRow]) -> QueryDict:
    """The query with the empty ``loc`` sentinel dropped; no ``loc`` at all selects every row."""
    data = request.GET.copy()
    if "loc" in data:
        data.setlist("loc", [value for value in data.getlist("loc") if value != ""])
    else:
        data.setlist("loc", [str(row.pk) for row in rows])
    return data


def _filter_context(form: CompareForm, data: QueryDict, rows: Sequence[LiveRow]) -> dict[str, Any]:
    """What the filter card renders: the chips, the range options and the date values.

    A valid form shows the effective period in the date inputs; an invalid one shows the
    submitted text back, so the admin can correct it.
    """
    selected = set(data.getlist("loc"))
    chips = [
        {"pk": row.pk, "name": row.name, "status": row.status, "checked": str(row.pk) in selected}
        for row in rows
    ]
    current = data.get("range") or DEFAULT_RANGE
    ranges = [
        {"value": value, "label": label, "checked": value == current, "index": i}
        for i, (value, label) in enumerate(RANGE_CHOICES)
    ]
    if form.is_valid():
        from_value = form.cleaned_data["first"].isoformat()
        to_value = form.cleaned_data["last"].isoformat()
    else:
        from_value = data.get("from", "")
        to_value = data.get("to", "")
    return {
        "chips": chips,
        "ranges": ranges,
        "from_value": from_value,
        "to_value": to_value,
        "earliest": compare.EARLIEST.isoformat(),
        "today": form.today.isoformat(),
    }


class CompareView(View):
    """``/compare/``: compare locations over a period (D-1). GET only; writes nothing."""

    template_name = "web/compare.html"
    # Tests inject a FakeClock with CompareView.as_view(clock=...) or monkeypatch.
    clock: Clock = SystemClock()

    def get(self, request: HttpRequest) -> HttpResponse:
        now = self.clock.now()
        tz = settings.TIME_ZONE
        today = model.local_today(now, tz)
        rows = live_rows(now)
        if not rows:
            return render(request, self.template_name, {"rows": rows})
        data = _query(request, rows)
        form = CompareForm(data, rows=rows, today=today)
        context: dict[str, Any] = {
            "rows": rows,
            "form": form,
            "invalid_line": INVALID_LINE,
            **_filter_context(form, data, rows),
        }
        if form.is_valid():
            keys = {int(value) for value in form.cleaned_data["loc"]}
            selected = [row for row in rows if row.pk in keys]
            first, last = form.cleaned_data["first"], form.cleaned_data["last"]
            period = compare.period_of(first, last, now, tz)
            series = compare_data.load_series(selected, period, now)
            result = compare.compare(series, period, now, tz)
            statuses = {row.pk: row.status for row in selected}
            context.update(
                results=True,
                summary=compare_chart.summary_text(len(selected), first, last),
                kpis=compare_chart.kpi_tiles(result),
                scoreboard=compare_chart.scoreboard(result, statuses),
                several=len(selected) > 1,
            )
        return render(request, self.template_name, context)
