"""Locations and device keys at the database level (LOC-02, D-07, D-10, K-6 defence in depth).

PostgreSQL itself rejects a period or grace outside 10-3600 s, an unknown language, a
duplicate device key and an off state without an outage start, so no code path (form,
shell, later migration) can store them. 01-09 adds the validator tests and 01-10 the
form-level K-6 tests to this file.
"""

import re
import string
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID
from django.db import IntegrityError, transaction

from powermon.engine.models import LocationState
from powermon.locations.keys import KEY_ALPHABET, KEY_LENGTH, generate_device_key, mask_key
from powermon.locations.models import Location

# D-07: 32 characters from [A-Za-z0-9], so a key never needs URL-encoding.
KEY_SHAPE = re.compile(r"[A-Za-z0-9]{32}")
SAMPLE_KEY = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"


def _new_location(now: datetime, **overrides: Any) -> Location:
    """A Location saved with only its required fields, plus ``overrides``."""
    fields: dict[str, Any] = {
        "name": "Office",
        "bot_token": DEFAULT_BOT_TOKEN,
        "chat_id": DEFAULT_CHAT_ID,
        "device_key": generate_device_key(),
        "created_at": now,
        **overrides,
    }
    return Location.objects.create(**fields)


def _assert_rejected(constraint: str, now: datetime, **overrides: Any) -> None:
    # The savepoint keeps the test transaction usable after the failed INSERT.
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        _new_location(now, **overrides)


# Location defaults and CHECK constraints


@pytest.mark.django_db
def test_location_defaults_match_D10(fixed_now: datetime) -> None:
    location = _new_location(fixed_now)
    location.refresh_from_db()

    assert (location.period_s, location.grace_s) == (60, 30)
    assert location.router_grace is False
    assert location.maintenance is False
    assert location.alerts_enabled is True
    assert location.language == "uk"
    assert location.deleted_at is None
    assert location.created_at == fixed_now
    assert location.chat_id == DEFAULT_CHAT_ID


@pytest.mark.django_db
def test_K6_db_rejects_period_below_10(fixed_now: datetime) -> None:
    _assert_rejected("location_period_10_3600", fixed_now, period_s=9)


@pytest.mark.django_db
def test_K6_db_rejects_grace_below_10(fixed_now: datetime) -> None:
    _assert_rejected("location_grace_10_3600", fixed_now, grace_s=9)


@pytest.mark.django_db
def test_db_rejects_period_above_3600(fixed_now: datetime) -> None:
    _assert_rejected("location_period_10_3600", fixed_now, period_s=3601)

    # Both bounds are inclusive.
    assert _new_location(fixed_now, period_s=10).period_s == 10
    assert _new_location(fixed_now, period_s=3600).period_s == 3600


@pytest.mark.django_db
def test_db_rejects_grace_above_3600(fixed_now: datetime) -> None:
    _assert_rejected("location_grace_10_3600", fixed_now, grace_s=3601)

    assert _new_location(fixed_now, grace_s=10).grace_s == 10
    assert _new_location(fixed_now, grace_s=3600).grace_s == 3600


@pytest.mark.django_db
def test_db_rejects_unknown_language(fixed_now: datetime) -> None:
    _assert_rejected("location_language_valid", fixed_now, language="de")

    for language in ("uk", "en", "ru"):
        assert _new_location(fixed_now, language=language).language == language


@pytest.mark.django_db
def test_db_stores_a_channel_chat_id_beyond_32_bits(fixed_now: datetime) -> None:
    # D-12: channel IDs are -100... and do not fit a 32-bit integer.
    location = _new_location(fixed_now, chat_id=-1009876543210)
    location.refresh_from_db()

    assert location.chat_id == -1009876543210


# Device keys (D-07)


@pytest.mark.django_db
def test_device_key_is_unique(fixed_now: datetime) -> None:
    _new_location(fixed_now, device_key=SAMPLE_KEY)

    _assert_rejected("device_key", fixed_now, device_key=SAMPLE_KEY)


def test_generate_device_key_shape() -> None:
    keys = [generate_device_key() for _ in range(1000)]

    assert KEY_LENGTH == 32
    # No "%", "-" or "_": safe in crontabs and URLs without encoding.
    assert set(KEY_ALPHABET) == set(string.ascii_letters + string.digits)
    assert all(KEY_SHAPE.fullmatch(key) for key in keys)
    assert len(set(keys)) == 1000


def test_mask_key() -> None:
    masked = mask_key(SAMPLE_KEY)

    assert masked == "•" * 12 + "Xy4z"
    assert SAMPLE_KEY[:28] not in masked


@pytest.mark.parametrize(
    "key",
    ["", SAMPLE_KEY[:31], SAMPLE_KEY + "A"],
    ids=["empty", "31-characters", "33-characters"],
)
def test_mask_key_rejects_other_lengths(key: str) -> None:
    with pytest.raises(ValueError, match="32 characters"):
        mask_key(key)


# Live state (location_state)


@pytest.mark.django_db
def test_location_state_off_requires_outage_start(fixed_now: datetime) -> None:
    location = _new_location(fixed_now)

    with (
        pytest.raises(IntegrityError, match="location_state_off_needs_outage_start"),
        transaction.atomic(),
    ):
        LocationState.objects.create(location=location, status="off")


@pytest.mark.django_db
def test_location_state_off_with_outage_start_is_accepted(fixed_now: datetime) -> None:
    location = _new_location(fixed_now)
    outage_start = fixed_now - timedelta(minutes=5)

    LocationState.objects.create(location=location, status="off", outage_started_at=outage_start)

    stored = LocationState.objects.get(pk=location.pk)
    assert (stored.status, stored.outage_started_at) == ("off", outage_start)


@pytest.mark.django_db
def test_location_state_rejects_unknown_status(fixed_now: datetime) -> None:
    location = _new_location(fixed_now)

    with pytest.raises(IntegrityError, match="location_state_status_valid"), transaction.atomic():
        LocationState.objects.create(location=location, status="bogus")


@pytest.mark.django_db
def test_location_factory_creates_waiting_state(location_factory: Any, fixed_now: datetime) -> None:
    location = location_factory()

    state = LocationState.objects.get(pk=location.pk)
    assert state.status == "waiting"
    assert state.state_version == 0
    assert (state.last_heartbeat_at, state.on_since, state.outage_started_at) == (None, None, None)
    assert state.window_start_at is None
    # v1-lessons section 1 test defaults.
    assert (location.period_s, location.grace_s, location.language) == (60, 30, "en")
    assert KEY_SHAPE.fullmatch(location.device_key)
