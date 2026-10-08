"""The location page's Weekly chart card (UI-06, D6-04; 06-UI-SPEC Page Contracts › S5
Weekly chart; TEST-STRATEGY §8.2, §7.5 UI-06).

- With stored history the card shows ``figure[data-testid=weekly-chart]`` (bound to the
  chartImage component): a link (new tab, noopener) around the lazy image of the
  ``location-chart`` route, 1280 x 1000, decoding async, with its alt text and a caption
  naming the channel's language; the header action ``chart-full-size``; the hidden
  ``weekly-chart-error`` warning that chartImage reveals when the image fails to load.
- Without stored history (``has_history`` false) it shows ``weekly-chart-empty`` and no
  image anywhere on the page, so the browser requests no PNG, and no header action.
- A waiting location with history (after a restore) and a location in maintenance show the
  image too: what decides is the stored history, not the status.
- While the channel's chart fails (an open ``chart_failing`` or ``chart_pin_failed``
  incident) the card shows one ``weekly-chart-trouble`` warning with fixed copy, the
  short ``http_NNN`` code and the start time, never the location name (F-04, quick task
  261008-vdk).
- Rendering the page never renders the chart: the browser requests the PNG lazily, and the
  route renders it per request (T-06-48; F-25, quick task 261008-vdk). Polling never
  touches the card.

Pages are read through tests/web/pages.py and the 06-UI-SPEC hooks only.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bs4 import Tag
from conftest import FakeClock
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from pages import all_by_testid, assert_page, by_testid, main, parse, section, text

from powermon.alerts.models import OpsIncident
from powermon.chart import render
from powermon.engine.models import LocationState, PowerInterval
from powermon.locations.models import Location
from powermon.web.location_views import LocationDetailView
from powermon.web.status import failing_since_text

User = get_user_model()

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 06-UI-SPEC copy table, loc.chart_*.
CHART_DESC = "The chart pinned in the channel"
CHART_LINK = "Open the weekly chart at full size (opens in a new tab)"
CHART_FULL = "Open full size (opens in a new tab)"
CHART_EMPTY = (
    "No chart yet The weekly chart appears here once the device has sent its first heartbeat."
)
CHART_ERROR = "The chart could not be drawn right now. Reload the page to try again."
LANGUAGE_LABELS = {"uk": "Ukrainian", "en": "English", "ru": "Russian"}
NAME = 'Office <b>"main"</b> & Co'


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, tzinfo=UTC)


def _page(location: Any) -> str:
    return f"/locations/{location.pk}/"


def _chart(location: Any) -> str:
    return reverse("location-chart", args=[location.pk])


def _history(location: Any) -> None:
    """Stored history: on 08:00-09:00, off 09:00-10:00, on since 10:00 (UTC)."""
    for state, start, end in (
        ("on", _at(8), _at(9)),
        ("off", _at(9), _at(10)),
        ("on", _at(10), None),
    ):
        PowerInterval.objects.create(
            location=location,
            state=state,
            start_at=start,
            end_at=end,
            outage_start_at=start if state == "off" else None,
        )


def _on(location: Any) -> None:
    LocationState.objects.filter(location=location).update(
        status="on", on_since=_at(10), last_heartbeat_at=_at(15)
    )


def _card(page: Any) -> Tag:
    return section(page, "weekly-chart")


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["uk", "en", "ru"])
def test_UI06_chart_card_with_history(
    admin: Client, location_factory: Callable[..., Any], language: str
) -> None:
    location = location_factory(name=NAME, language=language)
    _history(location)
    _on(location)
    url = _chart(location)

    soup = assert_page(admin.get(_page(location)), title=NAME, app=True)

    card = _card(soup)
    assert CHART_DESC in text(card)
    figure = by_testid(card, "weekly-chart")
    assert (figure.name, figure.get("x-data")) == ("figure", "chartImage")
    [image] = figure.find_all("img")
    assert image["src"] == url == f"/locations/{location.pk}/chart.png"
    assert (image["width"], image["height"]) == ("1280", "1000")
    assert (image["loading"], image["decoding"]) == ("lazy", "async")
    assert image["alt"] == (
        f"Weekly power chart for {NAME}, the same image as the chart pinned in the Telegram channel"
    )
    # The caption names the channel's language; the image is described by it.
    caption = figure.find("figcaption")
    assert isinstance(caption, Tag)
    assert image["aria-describedby"] == caption["id"] == "chart-caption"
    assert text(caption) == (
        f"Shown in the channel's language ({LANGUAGE_LABELS[language]}). The same outages are "
        "listed under Recent outages."
    )
    # The image link and the header action open the PNG in a new tab, without the opener.
    link = image.find_parent("a")
    assert isinstance(link, Tag)
    full = by_testid(card, "chart-full-size")
    for anchor, name in ((link, CHART_LINK), (full, CHART_FULL)):
        assert (anchor["href"], anchor["target"], anchor["rel"]) == (url, "_blank", ["noopener"])
        assert (anchor.get("aria-label") or text(anchor)) == name
    assert full.find_parent("figure") is None
    # The error fallback is in the same card, hidden until chartImage reveals it.
    error = by_testid(card, "weekly-chart-error")
    assert error.has_attr("hidden")
    assert error.find_parent("figure") is figure
    assert [found["data-tone"] for found in error.select("[data-tone]")] == ["warning"]
    assert text(error) == f"Warning: {CHART_ERROR}"
    assert not all_by_testid(soup, "weekly-chart-empty")
    # The poll never touches the card, so it never asks for the image again.
    assert not card.select("[data-live]")
    assert not card.select("[data-location-id]")
    # The image URL is the working PNG route.
    png = admin.get(url)
    assert (png.status_code, png["Content-Type"]) == (200, "image/png")
    assert png.content.startswith(PNG_SIGNATURE)


@pytest.mark.django_db
def test_UI06_chart_card_without_history(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    soup = assert_page(admin.get(_page(location)), title="Office", app=True)

    card = _card(soup)
    empty = by_testid(card, "weekly-chart-empty")
    assert text(empty) == CHART_EMPTY
    # No image anywhere on the page, so the browser requests no PNG; no header action.
    assert soup.find_all("img") == []
    assert _chart(location) not in str(soup)
    for hook in ("weekly-chart", "chart-full-size", "weekly-chart-error"):
        assert not all_by_testid(soup, hook), hook


@pytest.mark.django_db
def test_UI06_chart_card_waiting_with_history_and_maintenance(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    # Waiting with stored pieces, as after a restore: the history decides, not the status.
    restored = location_factory(name="Restored")
    _history(restored)
    paused = location_factory(name="Paused", maintenance=True)
    _history(paused)
    _on(paused)

    for location, status in ((restored, "waiting"), (paused, "maintenance")):
        page = parse(admin.get(_page(location)))
        pill = by_testid(by_testid(page, "location-header"), "status-pill")
        assert pill["data-status"] == status
        [image] = by_testid(_card(page), "weekly-chart").find_all("img")
        assert image["src"] == _chart(location)
        assert all_by_testid(page, "chart-full-size")
        assert not all_by_testid(page, "weekly-chart-empty")


@pytest.mark.django_db
def test_UI06_page_render_does_not_render_the_chart(
    admin: Client, monkeypatch: pytest.MonkeyPatch, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    _history(location)
    _on(location)
    calls: list[str] = []
    real = render.render_png

    def spy(*args: Any, **kwargs: Any) -> bytes:
        calls.append(str(kwargs.get("name")))
        return real(*args, **kwargs)

    monkeypatch.setattr(render, "render_png", spy)

    response = admin.get(_page(location))

    # The page only points at the PNG (lazily); it never renders the chart (T-06-48).
    assert response.status_code == 200
    assert by_testid(main(parse(response)), "weekly-chart").find("img") is not None
    assert calls == []
    # Every request for the image renders it (F-25). No byte equality: the live chart's
    # "now" moves under the real clock.
    first = admin.get(_chart(location))
    second = admin.get(_chart(location))
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.content.startswith(PNG_SIGNATURE)
    assert second.content.startswith(PNG_SIGNATURE)
    assert calls == ["Office", "Office"]
    # A deleted location's page and chart answer 404, the chart without a render.
    Location.objects.filter(pk=location.pk).update(deleted_at=_at(16))
    assert admin.get(_page(location)).status_code == 404
    assert admin.get(_chart(location)).status_code == 404
    assert calls == ["Office", "Office"]


# F-04 (quick task 261008-vdk): the card warns while the channel's chart fails

CHART_FAILING_TITLE = "The channel's chart is not being updated"
CHART_PIN_TITLE = "Today's chart is not pinned"


def _incident(location: Any, kind: str, details: dict[str, Any], **kw: Any) -> OpsIncident:
    return OpsIncident.objects.create(
        kind=kind, location=location, started_at=_at(9, 5), details=details, **kw
    )


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["failing", "pin_only", "both", "closed", "delivery_only"])
def test_weekly_chart_card_warns_while_the_chart_fails(
    admin: Client,
    location_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setattr(LocationDetailView, "clock", FakeClock(_at(15)))
    location = location_factory(name=NAME)
    _history(location)
    _on(location)
    if case in ("failing", "both"):
        _incident(location, "chart_failing", {"http_status": 403})
    if case in ("pin_only", "both"):
        _incident(location, "chart_pin_failed", {})
    if case == "closed":
        _incident(location, "chart_failing", {"http_status": 403}, ended_at=_at(10))
        _incident(location, "chart_pin_failed", {}, ended_at=_at(10))
    if case == "delivery_only":
        _incident(location, "delivery_failing", {"http_status": 403})
    since = failing_since_text(_at(9, 5), _at(15), settings.TIME_ZONE)

    response = admin.get(_page(location))
    soup = assert_page(response, title=NAME, app=True)

    card = _card(soup)
    found = all_by_testid(card, "weekly-chart-trouble")
    assert all_by_testid(soup, "weekly-chart-trouble") == found
    if case in ("closed", "delivery_only"):
        assert found == []
    else:
        [warning] = found
        assert warning["data-tone"] == "warning"
        body = text(warning)
        if case == "pin_only":
            assert CHART_PIN_TITLE in body
            assert f"Telegram refused the pin (http_400) since {since}." in body
            assert CHART_FAILING_TITLE not in body
        else:
            assert CHART_FAILING_TITLE in body
            assert f"Telegram refused to post or update it (http_403) since {since}." in body
            assert CHART_PIN_TITLE not in body
        # Fixed copy: never the location name, raw or escaped.
        assert NAME not in body
        assert "Office" not in body
        assert "Office" not in str(warning)
    # INV-23: the page shows no secret.
    html = response.content.decode()
    assert location.device_key not in html
    assert location.bot_token not in html
