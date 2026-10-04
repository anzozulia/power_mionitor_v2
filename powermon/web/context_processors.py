"""Template context for every page: the theme and the sidebar location list (UI-02, UI-03,
R11).

- ``theme``: the ``theme`` cookie's value when it is exactly ``light``, ``dark`` or
  ``system``, else ``system`` (D6-03). It runs no query, and the raw cookie value never
  reaches a template (R1): the page's ``data-theme`` is always one of the three words.
- ``sidebar``: for a signed-in request, a lazy object (``SidebarData``) whose rows come
  from the location list's two queries (``live.live_rows``). Nothing runs until a template
  reads it, so a response that renders no template (the status JSON, the chart PNG, a
  redirect) never pays for it.

Both are anonymous-safe because Django renders the 404 and 403-CSRF pages with the
request, so every processor runs there too, also for a signed-out visitor
(CsrfViewMiddleware comes before LoginRequiredMiddleware; R11). An anonymous request, and
a bare RequestFactory request with no ``user`` attribute, get no sidebar and cost no
query. The rows are small frozen display rows, never a ``Location``: a template can never
reach a bot token or a device key through them (R3, R4).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from powermon.clock import Clock, SystemClock
from powermon.web.live import fleet_counts, live_rows

# The theme choices (D6-03). The theme POST (``powermon.web.theme``) uses the same set.
THEMES = frozenset({"light", "dark", "system"})
THEME_COOKIE = "theme"
DEFAULT_THEME = "system"
# The sidebar's clock: "now" for its relative times and the delivery text's "today".
# Tests replace it with a FakeClock (monkeypatch).
CLOCK: Clock = SystemClock()


def theme(request: HttpRequest) -> dict[str, str]:
    """``{"theme": ...}``: the allowlisted cookie value, else ``system``. No query."""
    value = request.COOKIES.get(THEME_COOKIE, DEFAULT_THEME)
    return {"theme": value if value in THEMES else DEFAULT_THEME}


@dataclass(frozen=True)
class SidebarRow:
    """One sidebar item: what the dot, the name and the compact cell show (UI-03)."""

    pk: int
    name: str
    # The status key ("on", "off", "waiting", "maintenance") and its label.
    status: str
    label: str
    delivery_failing: bool
    last_heartbeat_at: datetime | None
    outage_started_at: datetime | None


@dataclass(frozen=True)
class SidebarData:
    """The sidebar's location list, its fleet counts and what the current page is."""

    rows: tuple[SidebarRow, ...]
    # The fleet tiles' counts (``live.fleet_counts``).
    counts: dict[str, int]
    # The location the current page is about (``aria-current``), None elsewhere.
    current_pk: int | None
    now: datetime
    ops_configured: bool


def build_sidebar(request: HttpRequest) -> SidebarData:
    """The sidebar of ``request``'s page: two queries, whatever the number of locations."""
    now = CLOCK.now()
    rows = live_rows(now)
    match = getattr(request, "resolver_match", None)
    current_pk = None if match is None else match.kwargs.get("pk")
    return SidebarData(
        rows=tuple(
            SidebarRow(
                pk=row.pk,
                name=row.name,
                status=row.status,
                label=row.status_label,
                delivery_failing=row.delivery_failing,
                last_heartbeat_at=row.last_heartbeat_at,
                outage_started_at=row.outage_started_at,
            )
            for row in rows
        ),
        counts=fleet_counts(rows),
        current_pk=current_pk,
        now=now,
        ops_configured=settings.CFG.ops_configured,
    )


def sidebar(request: HttpRequest) -> dict[str, Any]:
    """``{"sidebar": <lazy SidebarData>}`` when signed in, else ``{}``; no query either way."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"sidebar": SimpleLazyObject(lambda: build_sidebar(request))}
