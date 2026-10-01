"""Locations and device keys at the database level (LOC-02, D-07, D-10, K-6 defence in depth).

PostgreSQL itself rejects a period or grace outside 10-3600 s, an unknown language, a
duplicate device key and an off state without an outage start, so no code path (form,
shell, later migration) can store them. 01-09 adds the validator tests and 01-10 the
form-level K-6 tests to this file.
"""

import logging
import re
import string
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID
from django.db import IntegrityError, transaction

from powermon.engine.models import LocationState
from powermon.locations.keys import KEY_ALPHABET, KEY_LENGTH, generate_device_key, mask_key
from powermon.locations.models import Location
from powermon.locations.validators import (
    MAX_BOT_TOKEN_LENGTH,
    MAX_CHAT_ID_DIGITS,
    clean_bot_token,
    mask_token,
    parse_chat_id,
)
from powermon.logging_setup import RedactingFormatter

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


# Bot token and chat ID input (LOC-02, D-12): local checks only, ASCII only, UI-SPEC copy.
# The messages are copied from 01-UI-SPEC.md here on purpose, so a drift in the module
# constants fails these tests.

TOKEN_EMPTY_MSG = "Paste the bot token from @BotFather."
TOKEN_FORMAT_MSG = (
    "This does not look like a bot token. It should be digits, a colon, then at least 30 "
    "letters, digits, - or _ (like 123456789:AAH…)."
)
CHAT_ID_EMPTY_MSG = "Enter the channel's numeric chat ID."
CHAT_ID_USERNAME_MSG = (
    "Use the numeric chat ID, not the @username. Private channels have no username."
)
CHAT_ID_NOT_INTEGER_MSG = "Enter a whole number, like -1001234567890."
CHAT_ID_TOO_LONG_MSG = (
    "This number is too long for a Telegram chat ID. Copy the ID again, like -1001234567890."
)
# 35 secret characters, including both punctuation characters a token may hold.
GOOD_TOKEN = "123456789:" + "AAH-dq_Tc9" * 3 + "XyZ01"
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def _assert_message(func: Callable[[str], object], value: str, message: str) -> None:
    """``func(value)`` raises ValueError whose text is exactly ``message`` and nothing else."""
    with pytest.raises(ValueError) as excinfo:
        func(value)
    assert str(excinfo.value) == message


def test_clean_bot_token() -> None:
    assert clean_bot_token(GOOD_TOKEN) == GOOD_TOKEN
    # Whitespace around a pasted token is trimmed.
    assert clean_bot_token(f"  {GOOD_TOKEN}\n") == GOOD_TOKEN
    # The shortest accepted shape: 5 digits, a colon, 30 secret characters.
    assert clean_bot_token("12345:" + "a" * 30) == "12345:" + "a" * 30
    # The longest one fills the bot_token column (255 characters).
    longest = "123456789:" + "A" * 245
    assert clean_bot_token(longest) == longest


@pytest.mark.parametrize("value", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_clean_bot_token_empty(value: str) -> None:
    _assert_message(clean_bot_token, value, TOKEN_EMPTY_MSG)


@pytest.mark.parametrize(
    "value",
    [
        "١٢٣٤٥٦٧٨٩:" + "A" * 35,
        "123456789:" + "A" * 29,
        "123456789:" + "A" * 17 + "/" + "A" * 17,
        "123456789:" + "A" * 17 + "\n" + "A" * 17,
        "1234:" + "A" * 35,
        "123456789" + "A" * 35,
        "123456789:" + "A" * 34 + "Ж",
        "bot123456789:" + "A" * 35,
        "123456789:" + "A" * 35 + "?x",
        "123456789:" + "A" * 246,
    ],
    ids=[
        "unicode-digits",
        "29-secret-characters",
        "slash",
        "embedded-newline",
        "4-digit-bot-id",
        "no-colon",
        "cyrillic-letter",
        "bot-prefix",
        "query-characters",
        "longer-than-the-column",
    ],
)
def test_clean_bot_token_rejects_bad_format(value: str) -> None:
    _assert_message(clean_bot_token, value, TOKEN_FORMAT_MSG)


def test_every_accepted_token_is_redacted_in_logs() -> None:
    # D-12: the validator accepts only the shape the log redaction scrubs.
    formatter = RedactingFormatter("%(message)s")
    for token in (GOOD_TOKEN, "12345:" + "a" * 30, "123456789:" + "A" * 245):
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "token %s", (token,), None)
        assert formatter.format(record) == "token [REDACTED-TOKEN]"
        assert clean_bot_token(token) == token


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-1001234567890", -1001234567890),
        ("  -1001234567890\n", -1001234567890),
        ("123456789", 123456789),
        (str(INT64_MIN), INT64_MIN),
        (str(INT64_MAX), INT64_MAX),
    ],
    ids=["channel", "trimmed", "positive", "int64-min", "int64-max"],
)
def test_parse_chat_id(value: str, expected: int) -> None:
    result = parse_chat_id(value)

    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", CHAT_ID_EMPTY_MSG),
        ("   ", CHAT_ID_EMPTY_MSG),
        ("@my_channel", CHAT_ID_USERNAME_MSG),
        (" @my_channel ", CHAT_ID_USERNAME_MSG),
        ("@100123", CHAT_ID_USERNAME_MSG),
        ("1_000", CHAT_ID_NOT_INTEGER_MSG),
        ("+100", CHAT_ID_NOT_INTEGER_MSG),
        ("١٢٣", CHAT_ID_NOT_INTEGER_MSG),
        ("１２３", CHAT_ID_NOT_INTEGER_MSG),
        ("12\n3", CHAT_ID_NOT_INTEGER_MSG),
        ("-", CHAT_ID_NOT_INTEGER_MSG),
        ("--100", CHAT_ID_NOT_INTEGER_MSG),
        ("-100 123", CHAT_ID_NOT_INTEGER_MSG),
        ("1.5", CHAT_ID_NOT_INTEGER_MSG),
        ("1e3", CHAT_ID_NOT_INTEGER_MSG),
        ("https://t.me/c/1234567890", CHAT_ID_NOT_INTEGER_MSG),
        (str(INT64_MAX + 1), CHAT_ID_TOO_LONG_MSG),
        (str(INT64_MIN - 1), CHAT_ID_TOO_LONG_MSG),
        ("12345678901234567890", CHAT_ID_TOO_LONG_MSG),
        ("-12345678901234567890", CHAT_ID_TOO_LONG_MSG),
    ],
    ids=[
        "empty",
        "spaces",
        "username",
        "username-with-spaces",
        "at-digits",
        "digit-separator",
        "leading-plus",
        "arabic-indic-digits",
        "fullwidth-digits",
        "embedded-newline",
        "minus-only",
        "double-minus",
        "inner-space",
        "decimal",
        "exponent",
        "link",
        "19-digits-above-int64",
        "19-digits-below-int64",
        "20-digits",
        "minus-20-digits",
    ],
)
def test_parse_chat_id_rejects(value: str, message: str) -> None:
    _assert_message(parse_chat_id, value, message)


@pytest.mark.parametrize(
    "value", ["1" * 5000, "-" + "9" * 5000], ids=["5000-digits", "minus-5000-digits"]
)
def test_parse_chat_id_huge_paste_never_shows_python_text(value: str) -> None:
    # int() refuses this many digits with its own "Exceeds the limit" text.
    assert len(value.lstrip("-")) > sys.get_int_max_str_digits()

    _assert_message(parse_chat_id, value, CHAT_ID_TOO_LONG_MSG)


def test_validator_bounds_match_int64_and_the_schema() -> None:
    # No signed 64-bit integer has more than 19 digits.
    assert MAX_CHAT_ID_DIGITS == len(str(INT64_MAX)) == len(str(INT64_MIN).lstrip("-")) == 19
    assert MAX_BOT_TOKEN_LENGTH == Location._meta.get_field("bot_token").max_length


def test_mask_token() -> None:
    masked = mask_token(GOOD_TOKEN)

    assert masked == "123456789:••••••••"
    assert GOOD_TOKEN.partition(":")[2] not in masked
    # Only the first colon splits; the secret part is never shown.
    assert mask_token("12345:ab:cd") == "12345:••••••••"


@pytest.mark.parametrize("value", ["no-colon-secret", ""], ids=["no-colon", "empty"])
def test_mask_token_without_colon_shows_only_bullets(value: str) -> None:
    assert mask_token(value) == "••••••••"
