"""The sidebar location list: a lazy, anonymous-safe context processor (UI-03, R3, R4, R11;
TEST-STRATEGY §8.5).

- ``context_processors.sidebar`` returns nothing for an anonymous request (the sign-in
  page, an anonymous 404 or 403-CSRF, which Django renders with the request: R11) and for
  a bare RequestFactory request without a ``user`` attribute, and runs no query for either.
- For a signed-in request it returns a lazy object: the call runs no query, and reading it
  runs the location list's query plus at most one incident query, the same count for 1
  and for 6 locations. A response that renders no template (the status JSON) never pays
  for it.
- Its rows are frozen ``SidebarRow`` instances with exactly the display fields (pk, name,
  status, label, delivery_failing, last_heartbeat_at, outage_started_at), in list order
  (``Lower(name)``, then pk), deleted locations excluded: never a ``Location`` with its
  token or key (R3, R4). ``current_pk`` comes from the resolved URL's ``pk``; the counts
  are the fleet tiles' (UI-04).

``context_processors.CLOCK`` is the module's clock; tests replace it with a ``FakeClock``.
"""

import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import FakeClock
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils.functional import SimpleLazyObject

from powermon.alerts import delivery
from powermon.engine.models import LocationState
from powermon.locations.models import Location
from powermon.web import context_processors
from powermon.web.context_processors import SidebarData, SidebarRow, build_sidebar, sidebar

User = get_user_model()

ROW_FIELDS = {
    "pk",
    "name",
    "status",
    "label",
    "delivery_failing",
    "last_heartbeat_at",
    "outage_started_at",
}
DISTINCT_NAME = "Sidebar Probe Zhytomyr"
SIDEBAR_PROCESSOR = "powermon.web.context_processors.sidebar"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> FakeClock:
    """The processor's clock, fixed at 2026-10-01 08:00 UTC."""
    fake = FakeClock(fixed_now)
    monkeypatch.setattr(context_processors, "CLOCK", fake)
    return fake


def _signed_in(path: str = "/") -> Any:
    """A request for ``path`` from the signed-in admin, resolved as the URL would be."""
    request = RequestFactory().get(path)
    request.user = User.objects.get_or_create(username="admin")[0]
    request.resolver_match = resolve(path)
    return request


def _location_queries(queries: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in queries.captured_queries if '"location"' in q["sql"]]


# Anonymous and bare requests (R11)


@pytest.mark.django_db
def test_UI03_sidebar_processor_anonymous(django_assert_num_queries: Any) -> None:
    request = RequestFactory().get("/login/")
    request.user = AnonymousUser()

    with django_assert_num_queries(0):
        assert sidebar(request) == {}


@pytest.mark.django_db
def test_UI03_sidebar_processor_bare_request(django_assert_num_queries: Any) -> None:
    # The shape view tests render pages with: no user attribute, no resolver match.
    request = RequestFactory().get("/")
    assert not hasattr(request, "user")
    assert getattr(request, "resolver_match", None) is None

    with django_assert_num_queries(0):
        assert sidebar(request) == {}


@pytest.mark.django_db
def test_UI03_sign_in_page_has_no_sidebar(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location_factory(name=DISTINCT_NAME)

    with CaptureQueriesContext(connection) as queries:
        page = client.get("/login/")

    assert page.status_code == 200
    assert "sidebar" not in page.context
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in page.content.decode()


@pytest.mark.django_db
def test_UI03_anonymous_csrf_failure_runs_no_location_query(
    location_factory: Callable[..., Any],
) -> None:
    # CsrfViewMiddleware comes before LoginRequiredMiddleware: an anonymous POST that fails
    # the CSRF check renders 403_csrf.html with the request, so every processor runs (R11).
    location_factory(name=DISTINCT_NAME)
    browser = Client(enforce_csrf_checks=True)

    with CaptureQueriesContext(connection) as queries:
        response = browser.post("/locations/new/", {"name": "x"})

    assert response.status_code == 403
    assert _location_queries(queries) == []
    assert DISTINCT_NAME not in response.content.decode()


# Signed in: lazy, constant, frozen display rows (UI-03, R4)


@pytest.mark.django_db
def test_UI03_sidebar_processor_lazy_and_constant(
    clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    first = location_factory(name="b")
    request = _signed_in()

    with CaptureQueriesContext(connection) as calling:
        context = sidebar(request)

    assert calling.captured_queries == []
    assert set(context) == {"sidebar"}
    # Not evaluated yet: type() does not unwrap the lazy object.
    assert type(context["sidebar"]) is SimpleLazyObject

    with CaptureQueriesContext(connection) as one:
        rows = context["sidebar"].rows
    assert [row.pk for row in rows] == [first.pk]

    for i in range(5):
        location = location_factory(name=f"L{i}")
        with transaction.atomic():
            delivery.open_failing(location.pk, fixed_now - timedelta(minutes=i + 1), 403)

    with CaptureQueriesContext(connection) as six:
        six_rows = sidebar(_signed_in())["sidebar"].rows
    assert len(six_rows) == 6
    # The location list's query plus one incident query, whatever the number of rows.
    assert len(one.captured_queries) == len(six.captured_queries) == 2


@pytest.mark.django_db
def test_UI03_sidebar_rows_are_frozen_display_rows(
    clock: FakeClock, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    lower_b = location_factory(name="b")
    upper_a = location_factory(name="A")
    lower_a = location_factory(name="a")
    location_factory(name="Gone", deleted_at=fixed_now)
    off_since = fixed_now - timedelta(minutes=7)
    LocationState.objects.filter(location=lower_a).update(
        status="off", outage_started_at=off_since, last_heartbeat_at=off_since
    )
    Location.objects.filter(pk=lower_b.pk).update(maintenance=True)
    with transaction.atomic():
        delivery.open_failing(lower_a.pk, fixed_now - timedelta(minutes=3), 403)

    data = build_sidebar(_signed_in())

    assert type(data) is SidebarData
    assert {field.name for field in dataclasses.fields(SidebarRow)} == ROW_FIELDS
    assert all(type(row) is SidebarRow for row in data.rows)
    with pytest.raises(dataclasses.FrozenInstanceError):
        data.rows[0].name = "changed"  # type: ignore[misc]
    # Lower(name), then the lower id on ties; the deleted location is not listed.
    assert [row.pk for row in data.rows] == [upper_a.pk, lower_a.pk, lower_b.pk]
    a = data.rows[1]
    assert (a.status, a.label, a.delivery_failing) == ("off", "Off", True)
    assert (a.last_heartbeat_at, a.outage_started_at) == (off_since, off_since)
    b = data.rows[2]
    assert (b.status, b.label, b.delivery_failing) == ("maintenance", "Maintenance", False)
    assert (b.last_heartbeat_at, b.outage_started_at) == (None, None)
    # The fleet tiles' counts and the processor's clock.
    assert data.counts == {"on": 0, "off": 1, "maintenance": 1, "waiting": 1, "failing": 1}
    assert data.now == fixed_now
    assert data.ops_configured is False
    # The list page has no current location.
    assert data.current_pk is None


@pytest.mark.django_db
def test_UI03_sidebar_current_pk_from_the_url(
    clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    for path in (
        f"/locations/{location.pk}/",
        f"/locations/{location.pk}/edit/",
        f"/locations/{location.pk}/setup/",
        f"/locations/{location.pk}/delete/",
    ):
        assert build_sidebar(_signed_in(path)).current_pk == location.pk, path
    # No resolver match (a bare request): no current location, no exception.
    bare = RequestFactory().get(f"/locations/{location.pk}/")
    assert build_sidebar(bare).current_pk is None


@pytest.mark.django_db
def test_UI03_sidebar_ops_configured(
    clock: FakeClock, ops_settings: Any, location_factory: Callable[..., Any]
) -> None:
    location_factory()

    assert build_sidebar(_signed_in()).ops_configured is True


@pytest.mark.django_db
def test_UI03_signed_in_page_carries_the_lazy_sidebar(
    clock: FakeClock, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    client = Client()
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    page = client.get(f"/locations/{location.pk}/")

    assert page.status_code == 200
    data = page.context["sidebar"]
    assert [row.name for row in data.rows] == ["Office"]
    assert data.current_pk == location.pk


@pytest.mark.django_db
def test_UI03_json_and_png_run_no_sidebar_query(
    settings: Any, location_factory: Callable[..., Any]
) -> None:
    location_factory(name="Office")
    client = Client()
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    with CaptureQueriesContext(connection) as registered:
        assert client.get("/locations/status.json").status_code == 200

    processors = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
    assert SIDEBAR_PROCESSOR in processors
    without = [name for name in processors if name != SIDEBAR_PROCESSOR]
    settings.TEMPLATES = [
        {**settings.TEMPLATES[0], "OPTIONS": {"context_processors": without}},
    ]
    with CaptureQueriesContext(connection) as unregistered:
        assert client.get("/locations/status.json").status_code == 200

    # The JSON renders no template, so the sidebar never runs: the same queries either way,
    # and one location query (the live rows').
    assert len(registered.captured_queries) == len(unregistered.captured_queries)
    assert len(_location_queries(registered)) == 1
