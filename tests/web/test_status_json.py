"""The status JSON for live refresh: ``GET /locations/status.json`` (UI-05, UI-04; D6-04;
TEST-STRATEGY §8.1).

- Shape: the top level is exactly {generated_at, ops_configured, counts, locations};
  ``locations`` is keyed by the location id and each entry has exactly {status, label,
  power, last_heartbeat, since, delivery}. ``label`` is ``STATUS_LABELS[status]``; every
  time is {iso, display, compact} with ``display`` from ``display_time`` and ``compact``
  from ``display_time_compact``; ``delivery.text`` is the list's ``delivery_text`` or "OK".
- Access and caching: login-required (anonymous 302 to sign-in), GET only (405 for POST,
  PUT, PATCH, DELETE, HEAD and OPTIONS), ``Cache-Control`` no-store and private,
  ``Vary: Cookie``, nosniff, no cookie set, the query string ignored and never echoed.
- No side effects: nothing is written, and a pending flash survives a poll.
- Two queries for any number of locations (one with none); the fleet counts are the
  tiles': an Off location with failing delivery counts as off and as failing (UI-04).
- Rows: non-deleted locations only; a deletion between two polls drops the row; two
  polls with no change are equal apart from ``generated_at``.
- INV-23 #2: no token, secret part or mask, no device key, key mask or key tail, no chat
  ID, no location name and no "•" at all.

The view takes an injected clock (``LocationStatusJsonView.as_view(clock=...)``) through
RequestFactory where the time matters; the access and header checks run through the test
client with the whole middleware stack.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_CHAT_ID, FakeClock
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from secret_fixtures import MASKED, SECRETS, TOKEN

from powermon.alerts import delivery
from powermon.engine.models import LocationState
from powermon.locations import keys
from powermon.locations.models import Location
from powermon.web.live import (
    FLEET_KEYS,
    LiveRow,
    LocationStatusJsonView,
    fleet_counts,
    iso_local,
    live_rows,
)
from powermon.web.location_views import ALERTS_COPY
from powermon.web.status import STATUS_LABELS
from powermon.web.templatetags.display_time import display_time, display_time_compact
from powermon.web.views import delivery_text

User = get_user_model()

URL = "/locations/status.json"
TOP_KEYS = {"generated_at", "ops_configured", "counts", "locations"}
ROW_KEYS = {"status", "label", "power", "last_heartbeat", "since", "delivery"}
TIME_KEYS = {"iso", "display", "compact"}
COUNT_KEYS = {"on", "off", "maintenance", "waiting", "failing"}
WRITES = ("INSERT", "UPDATE", "DELETE")
# A fixed device key, so the key-tail scan is deterministic.
DEVICE_KEY = "Qm4Rt8Wz2Xc6Vb0Nk3Lp7Hj5Gf9Ds1Az"
SECRET_NAME = "Dacha Zvenyhorod Secretname"


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


@pytest.fixture
def kyiv(settings: Any) -> Any:
    """Pin the display TZ, so the expected times do not depend on the env file."""
    settings.TIME_ZONE = "Europe/Kyiv"
    return settings


def _set_state(location: Any, **fields: Any) -> None:
    LocationState.objects.filter(location=location).update(**fields)


def _on(location: Any, since: datetime, heartbeat: datetime | None = None) -> None:
    _set_state(location, status="on", on_since=since, last_heartbeat_at=heartbeat or since)


def _off(location: Any, since: datetime) -> None:
    """Off since ``since``: the last heartbeat came 90 s before (period 60 + grace 30)."""
    _set_state(
        location,
        status="off",
        outage_started_at=since,
        last_heartbeat_at=since - timedelta(seconds=90),
    )


def _maintenance(location: Any) -> None:
    Location.objects.filter(pk=location.pk).update(maintenance=True)


def _fail(location: Any, started_at: datetime, status: int = 403) -> None:
    """Open the location's delivery_failing incident as the relay does (D-10)."""
    with transaction.atomic():
        delivery.open_failing(location.pk, started_at, status)


def _poll(now: datetime) -> dict[str, Any]:
    """The payload as the view answers it at ``now`` (injected clock, no middleware)."""
    response = LocationStatusJsonView.as_view(clock=FakeClock(now))(RequestFactory().get(URL))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    payload: dict[str, Any] = json.loads(response.content)
    return payload


def _instant(value: datetime) -> dict[str, str]:
    return {
        "iso": iso_local(value),
        "display": display_time(value),
        "compact": display_time_compact(value),
    }


def _writes(queries: CaptureQueriesContext) -> list[str]:
    """Every INSERT, UPDATE or DELETE statement the context captured."""
    statements = [query["sql"] for query in queries.captured_queries]
    return [sql for sql in statements if sql.lstrip().upper().startswith(WRITES)]


# Shape (UI-05)


@pytest.mark.django_db
def test_UI05_status_json_shape(
    kyiv: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    alpha = location_factory(name="Alpha")
    beta = location_factory(name="Beta")
    heartbeat = fixed_now - timedelta(seconds=10)
    _on(alpha, fixed_now - timedelta(hours=1), heartbeat)
    _off(beta, fixed_now - timedelta(minutes=2))
    _fail(beta, fixed_now - timedelta(minutes=30))

    payload = _poll(fixed_now)

    assert set(payload) == TOP_KEYS
    assert payload["generated_at"] == "2026-10-01T11:00:00+03:00"
    # The test env has no ops chat.
    assert payload["ops_configured"] is False
    assert set(payload["counts"]) == COUNT_KEYS
    # An object keyed by id, so no consumer relies on order.
    assert set(payload["locations"]) == {str(alpha.pk), str(beta.pk)}
    for entry in payload["locations"].values():
        assert set(entry) == ROW_KEYS
        assert entry["label"] == STATUS_LABELS[entry["status"]]
        assert set(entry["last_heartbeat"]) == TIME_KEYS

    on = payload["locations"][str(alpha.pk)]
    assert (on["status"], on["label"], on["power"]) == ("on", "On", "on")
    assert on["last_heartbeat"] == _instant(heartbeat)
    assert on["last_heartbeat"] == {
        "iso": "2026-10-01T10:59:50+03:00",
        "display": "2026-10-01 10:59:50 EEST",
        "compact": "2026-10-01 10:59",
    }
    assert on["delivery"] == {"state": "ok", "text": "OK"}

    off = payload["locations"][str(beta.pk)]
    assert (off["status"], off["label"], off["power"]) == ("off", "Off", "off")
    failing = delivery.failing_incidents([beta.pk])[beta.pk]
    assert off["delivery"] == {"state": "failing", "text": delivery_text(failing, fixed_now)}
    assert off["delivery"]["text"] == "Failing since 10:30 (http_403)"


@pytest.mark.django_db
def test_UI05_status_json_ops_configured(
    ops_settings: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location_factory()

    assert _poll(fixed_now)["ops_configured"] is True


@pytest.mark.django_db
def test_UI05_status_json_waiting_and_never(
    kyiv: Any, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    waiting = location_factory(name="Waiting")
    on = location_factory(name="On")
    off = location_factory(name="Off")
    mnt_on = location_factory(name="Maintenance on")
    mnt_off = location_factory(name="Maintenance off")
    on_since = fixed_now - timedelta(hours=3)
    outage = fixed_now - timedelta(minutes=5)
    _on(on, on_since)
    _off(off, outage)
    _on(mnt_on, on_since)
    _maintenance(mnt_on)
    _off(mnt_off, outage)
    _maintenance(mnt_off)

    rows = _poll(fixed_now)["locations"]

    # Waiting: no heartbeat yet ("Never" on the page) and no since.
    assert rows[str(waiting.pk)]["status"] == "waiting"
    assert rows[str(waiting.pk)]["label"] == "Waiting for first heartbeat"
    assert rows[str(waiting.pk)]["last_heartbeat"] is None
    assert rows[str(waiting.pk)]["since"] is None
    assert rows[str(on.pk)]["since"] == {"kind": "on", **_instant(on_since)}
    assert rows[str(off.pk)]["since"] == {"kind": "outage", **_instant(outage)}
    # Maintenance is the status; the stored power state stays visible underneath.
    assert (rows[str(mnt_on.pk)]["status"], rows[str(mnt_on.pk)]["power"]) == (
        "maintenance",
        "on",
    )
    assert rows[str(mnt_on.pk)]["since"]["kind"] == "on"
    assert (rows[str(mnt_off.pk)]["status"], rows[str(mnt_off.pk)]["power"]) == (
        "maintenance",
        "off",
    )
    assert rows[str(mnt_off.pk)]["since"] == {"kind": "outage", **_instant(outage)}


@pytest.mark.django_db
def test_UI05_status_json_on_without_on_since_has_no_since(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    # A stored value the admin pages must never fail on: status on, no on_since.
    location = location_factory()
    _set_state(location, status="on", last_heartbeat_at=fixed_now)

    assert _poll(fixed_now)["locations"][str(location.pk)]["since"] is None


# Access, methods and headers (R14, TEST-STRATEGY §8.1)


@pytest.mark.django_db
def test_UI05_status_json_anonymous_redirects(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")

    response = client.get(URL)

    assert response.status_code == 302
    assert response.url == f"/login/?next={URL}"
    assert b"locations" not in response.content
    assert str(location.pk).encode() not in response.content


@pytest.mark.django_db
def test_UI05_status_json_methods(admin: Client, location_factory: Callable[..., Any]) -> None:
    location_factory()

    for method in (admin.post, admin.put, admin.patch, admin.delete, admin.head, admin.options):
        assert method(URL).status_code == 405, method.__name__
    assert admin.get(URL).status_code == 200


@pytest.mark.django_db
def test_UI05_status_json_headers(admin: Client, location_factory: Callable[..., Any]) -> None:
    location_factory(name="Office")

    plain = admin.get(URL)
    response = admin.get(URL, {"callback": "x", "_": "123"})

    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    cache_control = {part.strip() for part in response["Cache-Control"].split(",")}
    assert {"no-store", "private"} <= cache_control
    assert "public" not in cache_control
    assert "Cookie" in {part.strip() for part in response["Vary"].split(",")}
    assert response["X-Content-Type-Options"] == "nosniff"
    # Polling never refreshes the session or the CSRF cookie.
    assert not response.cookies
    assert "Set-Cookie" not in response.headers
    # The query string changes nothing and is never echoed (no JSONP).
    assert b"callback" not in response.content
    assert response.content.startswith(b"{")
    first, second = plain.json(), response.json()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


@pytest.mark.django_db
def test_UI05_status_json_writes_nothing_and_keeps_the_flash(
    admin: Client, location_factory: Callable[..., Any]
) -> None:
    location = location_factory(name="Office")
    # Queue a flash: the alerts switch redirects with it, and nothing reads it yet.
    assert admin.post(f"/locations/{location.pk}/alerts/", {"value": "off"}).status_code == 302

    with CaptureQueriesContext(connection) as queries:
        response = admin.get(URL)

    assert response.status_code == 200
    assert queries.captured_queries
    assert _writes(queries) == []
    assert not response.cookies
    # The next page view shows the flash: the poll did not consume it.
    page = admin.get(f"/locations/{location.pk}/")
    assert [str(message) for message in page.context["messages"]] == [ALERTS_COPY["off"]]


# Query count and fleet counts (UI-04, UI-05)


@pytest.mark.django_db
def test_UI05_status_json_constant_queries(
    location_factory: Callable[..., Any], fixed_now: datetime, django_assert_num_queries: Any
) -> None:
    first = location_factory(name="L0")
    _off(first, fixed_now - timedelta(minutes=1))
    _fail(first, fixed_now - timedelta(minutes=1))

    with django_assert_num_queries(2):
        one = _poll(fixed_now)

    for i in range(1, 6):
        location = location_factory(name=f"L{i}")
        _on(location, fixed_now - timedelta(hours=i))
        _fail(location, fixed_now - timedelta(minutes=i))

    with django_assert_num_queries(2):
        six = _poll(fixed_now)

    assert len(one["locations"]) == 1
    assert len(six["locations"]) == 6


@pytest.mark.django_db
def test_UI04_fleet_counts(
    location_factory: Callable[..., Any], fixed_now: datetime, django_assert_num_queries: Any
) -> None:
    # Edge: zero locations, every key present and 0, one query (no incident query).
    with django_assert_num_queries(1):
        empty = _poll(fixed_now)
    assert empty["locations"] == {}
    assert empty["counts"] == dict.fromkeys(COUNT_KEYS, 0)

    on = location_factory(name="On")
    _on(on, fixed_now - timedelta(hours=1))
    off = location_factory(name="Off")
    _off(off, fixed_now - timedelta(minutes=3))
    mnt_on = location_factory(name="Maintenance, power on")
    _on(mnt_on, fixed_now - timedelta(hours=2))
    _maintenance(mnt_on)
    mnt_off = location_factory(name="Maintenance, power off")
    _off(mnt_off, fixed_now - timedelta(minutes=9))
    _maintenance(mnt_off)
    location_factory(name="Waiting")
    off_failing = location_factory(name="Off and failing")
    _off(off_failing, fixed_now - timedelta(minutes=4))
    _fail(off_failing, fixed_now - timedelta(minutes=20))

    counts = _poll(fixed_now)["counts"]

    # Maintenance never counts as on or off; failing counts on top of the status.
    assert counts == {"on": 1, "off": 2, "maintenance": 2, "waiting": 1, "failing": 1}


def _row(status: str, *, failing: bool = False) -> LiveRow:
    now = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    return LiveRow(
        pk=1,
        name="x",
        status=status,
        status_label=STATUS_LABELS.get(status, status),
        last_heartbeat_at=now,
        alerts_off=False,
        router_grace=False,
        delivery="Failing since 10:00 (http_403)" if failing else None,
        power="off" if status == "off" else "on",
        on_since=now,
        outage_started_at=now if status == "off" else None,
        delivery_failing=failing,
    )


def test_UI04_fleet_counts_function() -> None:
    # Expected: one per status, failing on top.
    rows = [_row("on"), _row("off", failing=True), _row("maintenance"), _row("waiting")]
    assert fleet_counts(rows) == {"on": 1, "off": 1, "maintenance": 1, "waiting": 1, "failing": 1}
    # Edge: no rows, every key present.
    assert fleet_counts([]) == dict.fromkeys(FLEET_KEYS, 0)
    assert tuple(fleet_counts([])) == FLEET_KEYS
    # Failure: a status outside the vocabulary is never silently counted.
    with pytest.raises(KeyError):
        fleet_counts([_row("unknown")])


def test_UI05_iso_local(kyiv: Any) -> None:
    # Expected: the display TZ with its offset, to the second (microseconds dropped).
    assert iso_local(datetime(2026, 10, 1, 8, 0, 5, 999_999, tzinfo=UTC)) == (
        "2026-10-01T11:00:05+03:00"
    )
    # Edge: the repeated autumn hour keeps two distinct offsets.
    assert iso_local(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == "2026-10-25T03:30:00+03:00"
    assert iso_local(datetime(2026, 10, 25, 1, 30, tzinfo=UTC)) == "2026-10-25T03:30:00+02:00"
    # Failure: a naive datetime has no defined instant.
    with pytest.raises(ValueError, match="naive"):
        iso_local(datetime(2026, 10, 1, 8, 0))  # noqa: DTZ001


@pytest.mark.django_db
def test_UI05_live_rows_order_and_deleted(
    location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    b = location_factory(name="b")
    upper_a = location_factory(name="A")
    lower_a = location_factory(name="a")
    location_factory(name="Gone", deleted_at=fixed_now)

    rows = live_rows(fixed_now)

    # Lower(name), then the lower id; the soft-deleted location is not listed.
    assert [row.pk for row in rows] == [upper_a.pk, lower_a.pk, b.pk]
    assert all(row.delivery_failing is False and row.delivery is None for row in rows)


# Rows across polls (UI-05 concurrency and idempotency)


@pytest.mark.django_db
def test_UI05_deleted_location_vanishes(
    admin: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    keep = location_factory(name="Keep")
    gone = location_factory(name="Gone")
    location_factory(name="Deleted long ago", deleted_at=fixed_now)

    first = admin.get(URL).json()
    Location.objects.filter(pk=gone.pk).update(deleted_at=fixed_now)
    second = admin.get(URL).json()

    assert set(first["locations"]) == {str(keep.pk), str(gone.pk)}
    assert set(second["locations"]) == {str(keep.pk)}
    assert second["counts"]["waiting"] == 1


@pytest.mark.django_db
def test_UI05_two_polls_are_equal(
    admin: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(name="Office")
    _on(location, fixed_now)

    first = admin.get(URL).json()
    second = admin.get(URL).json()

    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
    # Under one clock the whole body is equal, generated_at included.
    assert _poll(fixed_now) == _poll(fixed_now)


# INV-23 #2: no secret, not even masked


@pytest.mark.django_db
def test_INV23_2_status_json_has_no_secrets(
    admin: Client, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory(name=SECRET_NAME, bot_token=TOKEN, device_key=DEVICE_KEY)
    _off(location, fixed_now - timedelta(minutes=5))
    _fail(location, fixed_now - timedelta(minutes=10), status=403)

    response = admin.get(URL)

    assert response.status_code == 200
    raw = response.content.decode()
    # The decoded payload too: JsonResponse escapes non-ASCII characters.
    readable = json.dumps(response.json(), ensure_ascii=False)
    forbidden = (
        *SECRETS,
        MASKED,
        DEVICE_KEY,
        keys.mask_key(DEVICE_KEY),
        DEVICE_KEY[-keys.MASK_VISIBLE :],
        str(DEFAULT_CHAT_ID),
        str(abs(DEFAULT_CHAT_ID)),
        SECRET_NAME,
        "•",
        "\\u2022",
    )
    for body in (raw, readable):
        for value in forbidden:
            assert value not in body, value
