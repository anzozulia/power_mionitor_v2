"""Delete a location (LOC-04; D-09, D-17; UI-SPEC screen D, UI-D5, UI-D8; INV-19 #2).

- Delete is a GET confirmation page, then a POST (CSRF). The page says what the delete
  does (the five consequences), points to the reversible options and to re-creating, and
  holds one form: the destructive "Delete location" POST, after "Keep location". No
  autofocus, no inline script. With the request header ``X-PM-Fragment: 1`` the same GET
  answers the shared confirmation partial alone for the modal (UI-07); that variant is
  tested in tests/web/test_fragments.py.
- The POST is one transaction under the location's ``location_state`` row lock
  (``actions.delete_location``): the ``deleted_at`` tombstone, a ``state_version`` bump
  (a detector snapshot read before it loses its OFF CAS), the pending subscriber alerts
  dropped (``location_deleted``) and the open ops incidents closed without a recovery
  notice. It makes no network call (KD2) and redirects to the list with the success flash.
- INV-19 #2 end to end: the device key gets 401, the queued alerts are never sent, the
  worker unpins the chart in its stored chat and posts no new one, and the location is gone
  from the list; every other URL of it answers 404. A second delete POST redirects to the
  list with the UI-D8 info flash and changes nothing.
- The races the transaction alone does not close are covered where they are handled: a
  heartbeat waiting on the row lock is ignored (04-03), a send in flight comes back to
  pending and the relay drops it (04-05), and a detector snapshot read before the delete
  loses its OFF even when its ``mark_off`` waited on the row lock during the delete.

Worker-, detection- and race-driven tests are ``django_db(transaction=True)``. The delete
POST gets a ``FakeClock`` by constructor injection through ``RequestFactory`` where its time
is asserted (``_post_delete``). The chart helpers are copied from tests/chart (tests have no
``__init__.py``). Pages are read through ``pages.py`` and the 06-UI-SPEC hooks (S7: the
``confirm`` root, ``confirm-title``, ``consequences``, ``confirm-form``, ``keep`` and
``confirm-submit``), never through markup or classes.
"""

import dataclasses
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import (
    DEFAULT_BOT_TOKEN,
    DEFAULT_CHAT_ID,
    OPS_BOT_TOKEN,
    Actor,
    FakeClock,
    FakeTelegram,
    blocked_on_lock,
    terminate_backends,
    wait_for,
)
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.http import HttpResponse
from django.test import Client, RequestFactory
from pages import (
    Message,
    assert_page,
    breadcrumbs,
    by_testid,
    h1,
    main,
    message_texts,
    messages,
    parse,
    text,
    title,
)

from powermon.alerts import delivery, outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.chart.models import ChartMessage
from powermon.engine import rules, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.locations import actions
from powermon.locations.models import Location
from powermon.web import location_views
from powermon.worker import io_loop

User = get_user_model()

KYIV = "Europe/Kyiv"
DELETED_FLASH = (
    "Location deleted. Its alerts have stopped. Its weekly chart is unpinned when the bot can "
    "do so; if the pin stays, unpin it by hand in Telegram."
)
ALREADY_DELETED_FLASH = "This location was already deleted."
LEAD = "This cannot be undone. Deleting this location:"
CONSEQUENCES = [
    "stops its alerts at once and drops the alerts still queued, so they are never sent;",
    "makes its device key stop working: the device gets HTTP 401;",
    "unpins its weekly chart in the channel if the bot can still pin there; otherwise unpin "
    "it by hand in Telegram (the posted messages stay in the channel);",
    "closes its open problems, such as failing delivery, without a recovery notice;",
    "hides it from the admin panel. Its history stays in the database but is never shown again.",
]
ALTERNATIVES = (
    "To pause this location instead, turn maintenance on or alerts off on the location page."
)
RECREATE = (
    "To monitor this place again later, add a new location. It gets a new device key and "
    "starts with an empty history."
)
SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
XSS_NAME = "<script>alert(1)</script>"
MIN_US = 60_000_000


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """An aware UTC instant on the fixed test day (2026-10-01)."""
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-01 12:00"`` (fold 0) as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(KYIV)).astimezone(UTC)


def _delete(location: Any) -> str:
    return f"/locations/{location.pk}/delete/"


def _post_delete(
    rf: RequestFactory, location: Any, clock: FakeClock
) -> tuple[HttpResponse, list[str]]:
    """POST the delete to the view with an injected clock; the response and its flashes."""
    request = rf.post(_delete(location))
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    response = location_views.LocationDeleteView.as_view(clock=clock)(request, pk=location.pk)
    return response, [str(m) for m in request._messages]  # type: ignore[attr-defined]


def _trail(location: Any, name: str) -> list[tuple[str, str | None]]:
    """The S7 breadcrumbs: Locations > {name} > Delete (the current page)."""
    return [("Locations", "/"), (name, f"/locations/{location.pk}/"), ("Delete", None)]


def _queue(location: Any, recorded_at: datetime) -> OutboxMessage:
    """A pending OFF alert of the location, due at ``recorded_at``."""
    with transaction.atomic():
        return outbox.enqueue(
            outbox.KIND_POWER_OFF,
            location.pk,
            event_at=recorded_at,
            recorded_at=recorded_at,
            payload={"was_on_us": 5 * MIN_US},
        )


def _row(row: OutboxMessage) -> OutboxMessage:
    return OutboxMessage.objects.get(pk=row.pk)


def _monitor(location: Any, since: datetime) -> None:
    """On since ``since``: live state "on" and one open on piece (chart_fixtures.monitor)."""
    LocationState.objects.filter(location=location).update(
        status="on",
        on_since=since,
        last_heartbeat_at=since,
        state_version=F("state_version") + 1,
    )
    PowerInterval.objects.create(location=location, state="on", start_at=since)


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass with charts, the detection cursor moved to the clock's now (never back)."""
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
    return io_loop.run_iteration(clock, state, charts=True)


def _chart_steps(fake: FakeTelegram, start: int = 0) -> list[tuple[str, int, int | None]]:
    """(method, chat id, message id) of every accepted chart call from index ``start`` on."""
    out = []
    for call in fake.chart_calls[start:]:
        message_id = call.fields.get("message_id")
        out.append(
            (
                call.method,
                int(call.fields["chat_id"]),
                None if message_id is None else int(message_id),
            )
        )
    return out


def _location_urls(location: Any) -> list[str]:
    """Every GET page of a location (the delete confirmation included)."""
    base = f"/locations/{location.pk}/"
    return [base, f"{base}edit/", f"{base}setup/", f"{base}setup/regenerate/", f"{base}delete/"]


def _finish(*actors: Actor, release: threading.Event) -> None:
    """Release the hook and join every started actor; end any session still stuck."""
    release.set()
    started = [actor for actor in actors if actor.ident is not None]
    for actor in started:
        actor.join(5)
    if any(actor.is_alive() for actor in started):
        terminate_backends(Actor.APPLICATION_NAME)
        for actor in started:
            actor.join(5)


# INV-19 #2 end to end (the tracer of this task)


@pytest.mark.django_db(transaction=True)
def test_INV19_2_delete_stops_everything(
    admin: Client,
    rf: RequestFactory,
    ops_settings: Any,
    location_factory: Callable[..., Any],
    fake_telegram: FakeTelegram,
) -> None:
    ops_settings.CFG = dataclasses.replace(ops_settings.CFG, display_tz=KYIV)
    location = location_factory(name="Office")
    key = location.device_key
    _monitor(location, _kyiv("2026-10-01 08:00"))
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept(OPS_BOT_TOKEN)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(_kyiv("2026-10-01 12:05"))
    relay = io_loop.RelayState()

    # Today's chart is posted and pinned in the location's chat.
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is True
    [record] = ChartMessage.objects.all()
    assert (record.chat_id, record.message_id, record.pinned) == (DEFAULT_CHAT_ID, 1001, True)

    # A queued OFF alert, held after a refusal, and the location's open failing incident.
    clock.advance(seconds=30)
    queued = _queue(location, clock.now())
    OutboxMessage.objects.filter(pk=queued.pk).update(
        attempts=1, last_error="http_403", next_attempt_at=clock.now() + timedelta(minutes=15)
    )
    with transaction.atomic():
        assert delivery.open_failing(location.pk, clock.now(), 403) is True
    # The failing notice about it goes out first, as it would have before the delete.
    assert _pass(clock, relay) is True
    assert fake_telegram.count(OPS_BOT_TOKEN, "sendMessage") == 1

    # The confirmation page (06-UI-SPEC Confirmations, S7).
    confirm = admin.get(_delete(location))
    soup = assert_page(confirm, app=True, title="Office · Delete")
    root = by_testid(main(soup), "confirm")
    assert text(h1(soup)) == "Delete Office?"
    assert h1(soup) is by_testid(root, "confirm-title")
    consequences = by_testid(root, "consequences")
    assert [text(item) for item in consequences.find_all("li")] == CONSEQUENCES
    words = text(root)
    assert words.index(LEAD) < words.index(ALTERNATIVES) < words.index(RECREATE)
    # One form in main: the destructive POST, after Keep.
    [form] = main(soup).find_all("form")
    assert (form.get("method"), form.get("action")) == ("post", _delete(location))
    assert form is by_testid(root, "confirm-form")
    submit = by_testid(form, "confirm-submit")
    assert (submit.get("type"), submit.get("data-variant"), text(submit)) == (
        "submit",
        "danger",
        "Delete location",
    )
    keep = by_testid(root, "keep")
    assert (keep.get("href"), text(keep)) == (f"/locations/{location.pk}/", "Keep location")
    assert soup.find_all(autofocus=True) == []
    # Nothing changed on GET.
    assert Location.objects.get(pk=location.pk).deleted_at is None

    # The delete: one transaction, no Telegram call, then the list with the success flash.
    calls_before = len(fake_telegram.calls)
    response, flashes = _post_delete(rf, location, clock)
    assert response.status_code == 302
    assert response["Location"] == "/"
    assert flashes == [DELETED_FLASH]
    assert len(fake_telegram.calls) == calls_before
    assert Location.objects.get(pk=location.pk).deleted_at == clock.now()
    dropped = _row(queued)
    assert (dropped.status, dropped.last_error) == ("dropped", "location_deleted")
    incident = OpsIncident.objects.get(kind=delivery.KIND_DELIVERY_FAILING, location=location)
    assert incident.ended_at == clock.now()
    # Closed without a recovery notice.
    assert not OutboxMessage.objects.filter(kind=outbox.KIND_OPS_DELIVERY_RESTORED).exists()

    # The device key gets 401 at once, in the header and in the URL.
    assert admin.get("/hb", HTTP_AUTHORIZATION=f"Bearer {key}").status_code == 401
    assert admin.post(f"/hb?key={key}").status_code == 401

    # The worker unpins the chart in its stored chat, by its message id, and nothing else:
    # no alert, no new chart, no refresh, not even 20 minutes later.
    start = len(fake_telegram.chart_calls)
    assert _pass(clock, relay) is True
    assert _pass(clock, relay) is False
    clock.advance(minutes=20)
    assert _pass(clock, relay) is False
    assert _chart_steps(fake_telegram, start) == [("unpinChatMessage", DEFAULT_CHAT_ID, 1001)]
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendMessage") == 0
    assert fake_telegram.count(DEFAULT_BOT_TOKEN, "sendPhoto") == 1
    record.refresh_from_db()
    assert record.retired_at is not None
    assert _row(queued).status == "dropped"
    # Only the failing notice queued before the delete reached the admin chat.
    assert fake_telegram.count(OPS_BOT_TOKEN, "sendMessage") == 1

    # The location is gone from the list, and every other URL of it answers 404.
    listing = parse(admin.get("/"))
    assert listing.find_all("a", href=f"/locations/{location.pk}/") == []
    for url in _location_urls(location):
        assert admin.get(url).status_code == 404, url
    for switch in ("maintenance", "alerts", "router-grace"):
        url = f"/locations/{location.pk}/{switch}/"
        assert admin.post(url, {"value": "on"}).status_code == 404, url
    assert admin.post(f"/locations/{location.pk}/edit/", {"name": "x"}).status_code == 404
    # History stays in the database.
    assert PowerInterval.objects.filter(location=location).exists()


# UI-D8: a second delete changes nothing


@pytest.mark.django_db
def test_delete_twice_shows_already_deleted(
    admin: Client, location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office")

    first = admin.post(_delete(location))
    assert first.status_code == 302
    assert first.url == "/"
    assert message_texts(admin.get(first.url)) == [DELETED_FLASH]
    deleted_at = Location.objects.get(pk=location.pk).deleted_at
    assert deleted_at is not None
    version = LocationState.objects.get(location=location).state_version
    # A row queued after the first delete (it would be swept by the relay) stays as it is.
    late = _queue(location, _at(9, 0))

    # The second tab or the double click: the list with the info flash, nothing else.
    second = admin.post(_delete(location))
    assert second.status_code == 302
    assert second.url == "/"
    page = admin.get(second.url).content.decode()
    assert message_texts(page) == [ALREADY_DELETED_FLASH]
    # An info toast, announced as status.
    assert messages(page) == [Message("info", "status", ALREADY_DELETED_FLASH)]
    assert Location.objects.get(pk=location.pk).deleted_at == deleted_at
    assert LocationState.objects.get(location=location).state_version == version
    assert _row(late).status == "pending"
    assert len(fake_telegram.calls) == 0


# Races (D-09, RESEARCH Pattern 4)


@pytest.mark.django_db(transaction=True)
def test_delete_leaves_a_send_in_flight_to_the_relay(
    location_factory: Callable[..., Any], fake_telegram: FakeTelegram
) -> None:
    location = location_factory(name="Office")
    in_flight = _queue(location, _at(9, 0))
    assert outbox.claim(in_flight.pk) is True

    # The delete drops pending rows only: the send in flight is not touched.
    assert actions.delete_location(location.pk, _at(9, 0, 5)) is True
    row = _row(in_flight)
    assert (row.status, row.attempts, row.last_error) == ("sending", 1, "")

    # Its outcome (a refusal) puts it back to pending; the next pass drops it, unsent.
    later = _at(9, 15)
    assert outbox.mark_retry(in_flight.pk, later, "http_403", attempts=1) is True
    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    assert io_loop.run_iteration(FakeClock(later), io_loop.RelayState()) is False
    row = _row(in_flight)
    assert (row.status, row.last_error) == ("dropped", "location_deleted")
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db(transaction=True)
def test_snapshot_taken_before_delete_loses_its_off(location_factory: Callable[..., Any]) -> None:
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": _at(9, 0), "web_started_at": None}
    )
    location = location_factory(name="Office")
    # Heartbeats until 09:59:00; at 10:02:00 the silence is past period + grace (90 s).
    for minute in range(55, 60):
        transitions.record_heartbeat(location.pk, _at(9, minute))
    now = _at(10, 2)
    # The detector reads its snapshot and decides OFF, as detection.run_cycle does...
    [snap] = [s for s in transitions.read_snapshots() if s.location_id == location.pk]
    system = SystemState.objects.get(pk=1)
    anchors = rules.Anchors(
        detection_resumed_at=system.detection_resumed_at, web_started_at=system.web_started_at
    )
    decision = rules.decide(snap, anchors, now)
    assert decision.off is True
    version = snap.state_version
    inside = threading.Event()
    release = threading.Event()

    def delete_and_hold() -> bool:
        # ...while the admin's delete holds the row lock, not yet committed.
        with transaction.atomic():
            deleted = actions.delete_location(location.pk, now)
            inside.set()
            if not release.wait(5):
                raise AssertionError("the delete was never released")
        return deleted

    deleting = Actor(delete_and_hold)
    detector = Actor(lambda: transitions.mark_off(snap, decision, now))
    try:
        deleting.start()
        assert inside.wait(5)
        detector.start()
        # The OFF transition waits on the row lock the delete holds.
        assert wait_for(lambda: detector.pid is not None and blocked_on_lock(detector.pid))
        release.set()
        deleting.join(5)
        detector.join(5)
    finally:
        _finish(deleting, detector, release=release)

    assert deleting.exc is None, deleting.exc
    assert detector.exc is None, detector.exc
    assert deleting.result is True
    # Its CAS then matches no row: no OFF, no off interval, no OFF alert.
    assert detector.result is False
    state = LocationState.objects.get(location=location)
    assert (state.status, state.state_version) == ("on", version + 1)
    assert not PowerInterval.objects.filter(location=location, state="off").exists()
    assert not OutboxMessage.objects.filter(location=location, kind="power_off").exists()


# The action on its own


@pytest.mark.django_db
def test_delete_location_touches_only_its_own_rows(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    other = location_factory(name="Other")
    mine = _queue(location, _at(9, 0))
    theirs = _queue(other, _at(9, 0))
    with transaction.atomic():
        assert delivery.open_failing(location.pk, _at(9, 0), 403) is True
        assert delivery.open_failing(other.pk, _at(9, 0), 403) is True
    gap = OpsIncident.objects.create(kind="monitoring_gap", started_at=_at(8, 0))

    assert actions.delete_location(location.pk, _at(9, 5)) is True

    assert _row(mine).status == "dropped"
    assert _row(theirs).status == "pending"
    open_incidents = OpsIncident.objects.filter(ended_at__isnull=True)
    assert set(open_incidents.values_list("id", flat=True)) == {
        gap.pk,
        OpsIncident.objects.get(location=other).pk,
    }
    assert Location.objects.get(pk=other.pk).deleted_at is None
    # Unknown or already deleted: False, nothing written.
    assert actions.delete_location(location.pk, _at(9, 6)) is False
    assert actions.delete_location(other.pk + 1000, _at(9, 6)) is False
    assert Location.objects.get(pk=location.pk).deleted_at == _at(9, 5)


@pytest.mark.django_db
def test_delete_location_without_a_state_row_and_a_naive_now(
    location_factory: Callable[..., Any],
) -> None:
    location = location_factory(name="No state row")
    LocationState.objects.filter(location=location).delete()

    with pytest.raises(ValueError, match="aware"):
        actions.delete_location(location.pk, datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001
    assert Location.objects.get(pk=location.pk).deleted_at is None

    assert actions.delete_location(location.pk, _at(9, 0)) is True
    assert Location.objects.get(pk=location.pk).deleted_at == _at(9, 0)


# Failure: unknown, deleted, anonymous, no CSRF token


@pytest.mark.django_db
def test_delete_unknown_id_is_404(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    unknown = location.pk + 1000

    assert admin.get(f"/locations/{unknown}/delete/").status_code == 404
    assert admin.post(f"/locations/{unknown}/delete/").status_code == 404
    assert Location.objects.get(pk=location.pk).deleted_at is None


@pytest.mark.django_db
def test_delete_get_of_deleted_is_404(admin: Client, location_factory: Callable[..., Any]) -> None:
    gone = location_factory(name="Gone", deleted_at=_at(9, 0))

    assert admin.get(_delete(gone)).status_code == 404


@pytest.mark.django_db
def test_delete_anonymous_redirects(client: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    url = _delete(location)

    assert client.get(url).url == f"/login/?next={url}"
    response = client.post(url)

    assert response.status_code == 302
    assert response.url == f"/login/?next={url}"
    assert Location.objects.get(pk=location.pk).deleted_at is None


@pytest.mark.django_db
def test_delete_without_a_csrf_token_is_refused(location_factory: Callable[..., Any]) -> None:
    location = location_factory(name="Office")
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    response = client.post(_delete(location))

    assert response.status_code == 403
    assert Location.objects.get(pk=location.pk).deleted_at is None


# UI rule 1, E6: the name is escaped and whole; the page shows no secret and no injected script


@pytest.mark.django_db
def test_delete_escapes_the_name(admin: Client, location_factory: Callable[..., Any]) -> None:
    location = location_factory(name=XSS_NAME, bot_token=TOKEN)

    response = admin.get(_delete(location))

    # assert_page also proves no injected or inline script reached the page (R1).
    soup = assert_page(response, app=True, title=f"{XSS_NAME} · Delete")
    assert text(h1(soup)) == f"Delete {XSS_NAME}?"
    assert breadcrumbs(soup)[1] == (XSS_NAME, f"/locations/{location.pk}/")
    page = response.content.decode()
    assert XSS_NAME not in page
    # No secret, not even masked: the page shows neither the token nor the key (SEC-04).
    assert SECRET not in page
    assert location.device_key not in page
    assert "•" not in str(main(soup))


@pytest.mark.django_db
def test_delete_page_long_name_and_crumbs(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    name = "x" * 100
    location = location_factory(name=name)

    soup = assert_page(admin.get(_delete(location)), app=True, title=f"{name} · Delete")

    # E10 long-text: the whole name, never truncated, in the title, h1 and both trails.
    assert title(soup) == f"{name} · Delete · Power Monitor"
    assert text(h1(soup)) == f"Delete {name}?"
    assert breadcrumbs(soup) == _trail(location, name)
    assert breadcrumbs(soup, "breadcrumbs-compact") == _trail(location, name)
    # One destructive button, no accent button (UI-D5).
    page = main(soup)
    dangers = page.find_all(attrs={"data-variant": "danger"})
    assert [found.get("data-testid") for found in dangers] == ["confirm-submit"]
    assert page.find_all(attrs={"data-variant": "primary"}) == []
