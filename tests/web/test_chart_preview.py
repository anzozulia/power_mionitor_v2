"""The weekly chart preview: ``GET /locations/<pk>/chart.png`` (UI-06, D6-04; TEST-STRATEGY
§8.2).

- The body is the channel's own chart: under the view's injected clock it equals
  ``render.render_png(source.load_week(pk, today=..., now=..., tz=..., live=True),
  lang=..., name=...)``, the call the worker makes for today's live chart, a PNG of the
  renderer's width x height.
- Headers: ``image/png``; ``Cache-Control`` private, no-cache, never max-age, public or
  s-maxage; ``Vary: Cookie``.
- Rendered per request (F-25, quick task 261008-vdk): every GET renders again, so a
  change shows on the next view, and two locations never get each other's bytes.
- 404 for an unknown or soft-deleted location, decided before any render.
- A location without stored history answers 200 with the all-no-data week, never 500.
- Login-required, GET only, writes nothing and never reaches Telegram.
- INV-23 #2: the bytes hold no token, secret part, mask, device key, key mask or key tail,
  and the PNG carries no text chunk.

The chart helpers are copied from tests/chart/chart_fixtures.py (tests have no
``__init__.py``, so tests/web cannot import them when it runs alone).
"""

import io
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import FakeClock, FakeTelegram
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import F
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from PIL import Image
from secret_fixtures import MASKED, SECRETS, TOKEN

from powermon.chart import model, render, source
from powermon.chart.model import Piece
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations import keys
from powermon.locations.models import Location
from powermon.web.chart_preview import LocationChartView

User = get_user_model()

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WRITES = ("INSERT", "UPDATE", "DELETE")
# A fixed device key, so the key-tail scan is deterministic.
DEVICE_KEY = "Hb7Yt2Kq9Wm4Xz6Rn1Vc8Lp3Jd5Gs0Fa"


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """Pin the display TZ, so the chart's days do not depend on the env file."""
    settings.TIME_ZONE = "Europe/Kyiv"
    return settings


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record the language and name of every render; the real renderer still runs.

    From tests/chart/test_lifecycle.py ``_spy_renders``: ``chart_content`` calls
    ``render.render_png`` as a module attribute.
    """
    seen: list[tuple[str, str]] = []
    real = render.render_png

    def spy(week: model.Week, *, lang: str, name: str) -> bytes:
        seen.append((lang, name))
        return real(week, lang=lang, name=name)

    monkeypatch.setattr(render, "render_png", spy)
    return seen


# From tests/chart/chart_fixtures.py (insert_pieces, set_status, monitor).


def insert_pieces(location: Any, pieces: list[Piece]) -> None:
    """Store ``pieces`` as the location's ``power_interval`` rows."""
    for p in pieces:
        PowerInterval.objects.create(
            location=location,
            state=p.state,
            start_at=p.start,
            end_at=p.end,
            outage_start_at=p.outage_start,
        )


def set_status(location: Any, status: str, *, at: datetime) -> None:
    """Put the location's live state in ``status`` as of ``at``."""
    LocationState.objects.filter(location=location).update(
        status=status,
        on_since=at,
        last_heartbeat_at=at,
        outage_started_at=at if status == "off" else None,
        state_version=F("state_version") + 1,
    )


def _history(location: Any, now: datetime) -> None:
    """Three days of history: on, a two-hour outage, then on until now."""
    start = now - timedelta(days=3)
    outage = start + timedelta(hours=5)
    back = outage + timedelta(hours=2)
    insert_pieces(
        location,
        [
            Piece("on", start, outage, None),
            Piece("off", outage, back, outage),
            Piece("on", back, None, None),
        ],
    )
    set_status(location, "on", at=back)


def _url(pk: int) -> str:
    return f"/locations/{pk}/chart.png"


def _get(pk: int, now: datetime) -> HttpResponse:
    """The view's answer at ``now`` (injected clock), without the middleware."""
    request = RequestFactory().get(_url(pk))
    response: HttpResponse = LocationChartView.as_view(clock=FakeClock(now))(request, pk=pk)
    return response


def _expected(location: Any, now: datetime) -> bytes:
    """The worker's live render of today's chart for ``location`` at ``now``."""
    tz = settings.TIME_ZONE
    week = source.load_week(
        location.pk, today=model.local_today(now, tz), now=now, tz=tz, live=True
    )
    return render.render_png(week, lang=location.language, name=location.name)


# The channel's own chart (UI-06)


@pytest.mark.django_db
def test_UI06_chart_png_is_the_channel_render(
    kyiv: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(name="Home, Obolon", language="uk")
    _history(location, fixed_now)

    response = _get(location.pk, fixed_now)

    assert response.status_code == 200
    body = response.content
    assert body.startswith(PNG_SIGNATURE)
    with Image.open(io.BytesIO(body)) as image:
        assert image.size == (render.W, render.H)
    assert body == _expected(location, fixed_now)


@pytest.mark.django_db
def test_UI06_chart_png_headers(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")

    response = admin.get(_url(location.pk))

    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    cache_control = {part.strip() for part in response["Cache-Control"].split(",")}
    # F-25: the browser asks again on every view; no cache keeps a stale copy.
    assert {"private", "no-cache"} <= cache_control
    assert not any(part.startswith("max-age") for part in cache_control)
    assert "public" not in cache_control
    assert not any(part.startswith("s-maxage") for part in cache_control)
    assert "Cookie" in {part.strip() for part in response["Vary"].split(",")}
    assert not response.cookies


# Rendered per request (F-25)


@pytest.mark.django_db
def test_UI06_chart_png_is_fresh_after_a_change(
    kyiv: Any,
    renders: list[tuple[str, str]],
    location_factory: Callable[..., Any],
    fixed_now: datetime,
) -> None:
    home = location_factory(name="Home")
    office = location_factory(name="Office")
    _history(home, fixed_now)
    _history(office, fixed_now)

    first = _get(home.pk, fixed_now)
    second = _get(home.pk, fixed_now)

    # Expected: every GET renders; the same state gives the same bytes.
    assert renders == [("en", "Home"), ("en", "Home")]
    assert second.content == first.content

    Location.objects.filter(pk=home.pk).update(name="Home, renamed")
    third = _get(home.pk, fixed_now)

    # Edge: the next GET after a change shows it at once (no 60 s stale copy).
    assert renders[-1] == ("en", "Home, renamed")
    assert third.content != first.content
    assert third.content == _expected(Location.objects.get(pk=home.pk), fixed_now)

    other = _get(office.pk, fixed_now)

    # Failure: one location never gets another's bytes.
    assert renders[-1] == ("en", "Office")
    assert other.content not in (first.content, second.content, third.content)


# 404 before the render (R14 surface, TEST-STRATEGY §8.2)


@pytest.mark.django_db
def test_UI06_chart_png_404(
    admin: Client, renders: list[tuple[str, str]], location_factory: Callable[..., Any]
) -> None:
    gone = location_factory(name="Gone")
    unknown = gone.pk + 1000
    Location.objects.filter(pk=gone.pk).update(deleted_at=gone.created_at)

    for pk in (unknown, gone.pk):
        response = admin.get(_url(pk))
        assert response.status_code == 404, pk
        assert not response.content.startswith(PNG_SIGNATURE)
    assert renders == []


@pytest.mark.django_db
def test_UI06_chart_png_no_history(
    kyiv: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    # Pinned: a waiting location without stored pieces gets the all-no-data week, 200.
    location = location_factory(name="New")

    response = _get(location.pk, fixed_now)

    assert response.status_code == 200
    assert response.content.startswith(PNG_SIGNATURE)
    assert response.content == _expected(location, fixed_now)


# Access, method, side effects


@pytest.mark.django_db
def test_UI06_chart_png_access(
    client: Client,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
    fixed_now: datetime,
) -> None:
    location = location_factory(name="Office")
    _history(location, fixed_now)
    url = _url(location.pk)

    anonymous = client.get(url)

    assert anonymous.status_code == 302
    assert anonymous.url == f"/login/?next={url}"
    assert not anonymous.content.startswith(PNG_SIGNATURE)

    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    for method in (client.post, client.put, client.delete, client.head):
        assert method(url).status_code == 405
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)

    assert response.status_code == 200
    statements = [query["sql"].lstrip().upper() for query in queries.captured_queries]
    assert [sql for sql in statements if sql.startswith(WRITES)] == []
    # The preview renders from the stored timeline only: nothing is sent, edited or pinned.
    assert len(fake_telegram.calls) == 0


# INV-23 #2: no secret in the bytes


@pytest.mark.django_db
def test_INV23_2_chart_png_has_no_secrets(
    admin: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(name="Secret probe", bot_token=TOKEN, device_key=DEVICE_KEY)
    _history(location, fixed_now)

    body = admin.get(_url(location.pk)).content

    assert body.startswith(PNG_SIGNATURE)
    values = (
        *SECRETS,
        MASKED,
        DEVICE_KEY,
        keys.mask_key(DEVICE_KEY),
        DEVICE_KEY[-keys.MASK_VISIBLE :],
    )
    for value in values:
        assert value.encode("utf-8") not in body, value
        if value.isascii():
            assert value.encode("ascii") not in body, value
    with Image.open(io.BytesIO(body)) as image:
        # No text chunk: Pillow's own info holds no string at all.
        assert [k for k, v in image.info.items() if isinstance(v, str | bytes)] == []
