"""Locations and device keys (LOC-02, D-07, D-10, D-11, D-12, K-6).

PostgreSQL itself rejects a period or grace outside 10-3600 s, an unknown language, a
duplicate device key and an off state without an outage start, so no code path (form,
shell, later migration) can store them. 01-09 adds the validator tests and 01-10 the
add-location form tests: K-6 at form level, the UI-SPEC validation copy, the write-only
token and the create itself (one transaction, no Telegram call).
"""

import logging
import re
import string
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from html import unescape
from typing import Any

import pytest
from conftest import DEFAULT_BOT_TOKEN, DEFAULT_CHAT_ID, FakeClock, FakeTelegram
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory
from pages import messages

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
from powermon.web.views import LocationCreateView

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


# The add-location form (LOC-02, UI-SPEC screen 3): K-6 at form level, the UI-SPEC copy,
# the write-only token (D-11) and a local-only create (D-12). Copy from 01-UI-SPEC.md.

FORM_ERROR_MSG = "The location was not saved. Fix the fields marked below."
REPASTE_NOTE = "Paste the token again: it is never sent back to the browser."
NAME_EMPTY_MSG = "Enter a name."
NAME_TOO_LONG_MSG = "Use at most 100 characters."
SECONDS_MSG = "Enter a whole number of seconds."
PERIOD_TOO_SHORT_MSG = "The heartbeat period must be at least 10 seconds."
GRACE_TOO_SHORT_MSG = "The grace period must be at least 10 seconds."
SECONDS_TOO_LONG_MSG = "Use at most 3600 seconds (1 hour)."
CREATED_FLASH = "Location created. Reveal the key below, then copy an example to the device."
DJANGO_REQUIRED = "This field is required."
NEW_URL = "/locations/new/"
SETUP_PATH = re.compile(r"/locations/([0-9]+)/setup/")

User = get_user_model()


@pytest.fixture
def admin(client: Client, db: None) -> Client:
    """A client signed in as the single admin."""
    client.force_login(User.objects.create_user("admin", password="not-used-here"))
    return client


def _form(**overrides: str) -> dict[str, str]:
    """A valid add-location POST, with ``overrides``."""
    return {
        "name": "Office",
        "period_s": "60",
        "grace_s": "30",
        "bot_token": GOOD_TOKEN,
        "chat_id": "-1001234567890",
        "language": "uk",
        **overrides,
    }


def _field_error(page: str, field: str) -> str | None:
    match = re.search(rf'<p class="error" id="id_{field}_error">(.*?)</p>', page, re.S)
    return unescape(match.group(1)) if match else None


def _alerts(page: str) -> list[str]:
    return [unescape(t.strip()) for t in re.findall(r'role="alert"[^>]*>([^<]*)<', page)]


def _tag(page: str, name: str) -> str:
    """The rendered ``<input>`` or ``<select>`` tag whose name attribute is ``name``."""
    match = re.search(rf'<(input|select)\b[^>]*\bname="{name}"[^>]*>', page)
    assert match is not None, f"no field named {name!r} in the page"
    return match.group(0)


def _rejected(admin: Client, **overrides: str) -> str:
    """POST an invalid form: 200, the form-level callout, nothing saved. Returns the page."""
    response = admin.post(NEW_URL, _form(**overrides))

    assert response.status_code == 200
    page = response.content.decode()
    assert _alerts(page) == [FORM_ERROR_MSG]
    assert Location.objects.count() == 0
    assert LocationState.objects.count() == 0
    assert DJANGO_REQUIRED not in page
    return page


def test_K6_period_below_10_field_error_nothing_saved(admin: Client) -> None:
    page = _rejected(admin, period_s="9")

    assert _field_error(page, "period_s") == PERIOD_TOO_SHORT_MSG
    assert _field_error(page, "grace_s") is None


def test_K6_grace_below_10_field_error_nothing_saved(admin: Client) -> None:
    page = _rejected(admin, grace_s="9")

    assert _field_error(page, "grace_s") == GRACE_TOO_SHORT_MSG
    assert _field_error(page, "period_s") is None


@pytest.mark.parametrize(("period", "grace"), [("10", "10"), ("3600", "3600")], ids=["min", "max"])
def test_K6_bounds_are_accepted(admin: Client, period: str, grace: str) -> None:
    response = admin.post(NEW_URL, _form(period_s=period, grace_s=grace))

    assert response.status_code == 302
    location = Location.objects.get()
    assert (location.period_s, location.grace_s) == (int(period), int(grace))


@pytest.mark.parametrize("field", ["period_s", "grace_s"])
def test_period_above_3600(admin: Client, field: str) -> None:
    page = _rejected(admin, **{field: "3601"})

    assert _field_error(page, field) == SECONDS_TOO_LONG_MSG


@pytest.mark.parametrize(
    "value", ["", "   ", "abc", "1.5", "60s", "9" * 5000], ids=lambda v: repr(v)[:12]
)
@pytest.mark.parametrize("field", ["period_s", "grace_s"])
def test_period_empty_or_not_a_whole_number(admin: Client, field: str, value: str) -> None:
    page = _rejected(admin, **{field: value})

    assert _field_error(page, field) == SECONDS_MSG
    assert "Exceeds the limit" not in page


@pytest.mark.parametrize(
    ("value", "message"),
    [("", NAME_EMPTY_MSG), ("   ", NAME_EMPTY_MSG), ("x" * 101, NAME_TOO_LONG_MSG)],
    ids=["empty", "whitespace", "101-characters"],
)
def test_name_empty_whitespace_and_too_long(admin: Client, value: str, message: str) -> None:
    page = _rejected(admin, name=value)

    assert _field_error(page, "name") == message


def test_name_counts_code_points_after_trimming(admin: Client) -> None:
    # 100 Cyrillic letters are 200 bytes in UTF-8 but 100 characters.
    name = "Ж" * 100

    response = admin.post(NEW_URL, _form(name=f"  {name}  "))

    assert response.status_code == 302
    assert Location.objects.get().name == name


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bot_token", "", TOKEN_EMPTY_MSG),
        ("bot_token", "   ", TOKEN_EMPTY_MSG),
        ("bot_token", "123456789:short", TOKEN_FORMAT_MSG),
        ("bot_token", "bot" + GOOD_TOKEN, TOKEN_FORMAT_MSG),
        ("chat_id", "", CHAT_ID_EMPTY_MSG),
        ("chat_id", "@my_channel", CHAT_ID_USERNAME_MSG),
        ("chat_id", "1_000", CHAT_ID_NOT_INTEGER_MSG),
        ("chat_id", "-100 123", CHAT_ID_NOT_INTEGER_MSG),
        ("chat_id", "1" * 25, CHAT_ID_TOO_LONG_MSG),
        ("chat_id", "1" * 5000, CHAT_ID_TOO_LONG_MSG),
        ("chat_id", str(INT64_MAX + 1), CHAT_ID_TOO_LONG_MSG),
    ],
    ids=[
        "token-empty",
        "token-whitespace",
        "token-short",
        "token-bot-prefix",
        "chat-empty",
        "chat-username",
        "chat-separator",
        "chat-inner-space",
        "chat-25-digits",
        "chat-5000-digits",
        "chat-above-int64",
    ],
)
def test_token_and_chat_id_errors_shown_on_the_form(
    admin: Client, field: str, value: str, message: str
) -> None:
    page = _rejected(admin, **{field: value})

    assert _field_error(page, field) == message
    # Python's own int() text for a huge paste never reaches the page.
    assert "Exceeds the limit" not in page
    assert "invalid literal" not in page


def test_missing_fields_show_field_messages_and_keep_the_rest(admin: Client) -> None:
    page = _rejected(
        admin, name="", period_s="", grace_s="45", bot_token="", chat_id="", language="ru"
    )

    assert _field_error(page, "name") == NAME_EMPTY_MSG
    assert _field_error(page, "period_s") == SECONDS_MSG
    assert _field_error(page, "bot_token") == TOKEN_EMPTY_MSG
    assert _field_error(page, "chat_id") == CHAT_ID_EMPTY_MSG
    assert _field_error(page, "grace_s") is None
    assert 'value="45"' in _tag(page, "grace_s")
    assert '<option value="ru" selected>Russian</option>' in page


def test_post_without_any_field_never_shows_djangos_required_text(admin: Client) -> None:
    response = admin.post(NEW_URL, {})

    page = response.content.decode()
    assert response.status_code == 200
    assert DJANGO_REQUIRED not in page
    assert _field_error(page, "language") == "Choose Ukrainian, English or Russian."
    assert Location.objects.count() == 0


def test_unknown_language_gets_ui_copy_not_the_posted_value(admin: Client) -> None:
    page = _rejected(admin, language="<b>de</b>")

    assert _field_error(page, "language") == "Choose Ukrainian, English or Russian."
    assert "de</b>" not in page
    assert "Select a valid choice" not in page


def test_invalid_submit_keeps_values_and_marks_the_field(admin: Client) -> None:
    page = _rejected(admin, name="Kyiv office", period_s="9", chat_id="-100777")

    period = _tag(page, "period_s")
    assert 'aria-invalid="true"' in period
    assert 'aria-describedby="id_period_s_helptext id_period_s_error"' in period
    assert 'value="9"' in period
    assert 'value="Kyiv office"' in _tag(page, "name")
    assert "aria-invalid" not in _tag(page, "name")
    assert 'value="-100777"' in _tag(page, "chat_id")


def test_token_never_rendered_back(admin: Client) -> None:
    # Only the period is wrong: the well-formed token must still come back empty (D-11).
    page = _rejected(admin, period_s="9")

    assert GOOD_TOKEN not in page
    assert GOOD_TOKEN.partition(":")[2] not in page
    token = _tag(page, "bot_token")
    assert 'type="password"' in token
    assert "value=" not in token
    assert f'<p class="help" id="id_bot_token_note">{REPASTE_NOTE}</p>' in page
    assert 'aria-describedby="id_bot_token_helptext id_bot_token_note"' in token


def test_form_defaults_and_attributes(admin: Client) -> None:
    response = admin.get(NEW_URL)

    assert response.status_code == 200
    page = response.content.decode()
    assert "<title>Add location · Power Monitor</title>" in page
    assert "<h1>Add location</h1>" in page
    assert re.search(r'<form class="form" method="post" action="/locations/new/" novalidate>', page)
    assert 'name="csrfmiddlewaretoken"' in page
    name = _tag(page, "name")
    assert 'maxlength="100"' in name
    assert "autofocus" in name
    assert "value=" not in name
    for field, initial in (("period_s", "60"), ("grace_s", "30")):
        tag = _tag(page, field)
        assert 'type="number"' in tag
        assert f'value="{initial}"' in tag
        assert 'min="10"' in tag
        assert 'max="3600"' in tag
        assert 'step="1"' in tag
    token = _tag(page, "bot_token")
    assert 'type="password"' in token
    assert "value=" not in token
    chat = _tag(page, "chat_id")
    assert 'type="text"' in chat
    assert "value=" not in chat
    for tag in (token, chat):
        assert 'autocomplete="off"' in tag
        assert 'spellcheck="false"' in tag
    assert '<option value="uk" selected>Ukrainian</option>' in page
    assert '<option value="en">English</option>' in page
    assert '<option value="ru">Russian</option>' in page
    assert "placeholder" not in page
    # First load: no error callout, no re-paste note, no invalid marks.
    assert _alerts(page) == []
    assert REPASTE_NOTE not in page
    assert "aria-invalid" not in page
    assert ">Create location</button>" in page
    assert '<a class="btn btn--secondary" href="/">Back to locations</a>' in page
    # The help text is the UI-SPEC copy, linked to its input.
    assert 'aria-describedby="id_chat_id_helptext"' in chat
    assert "web.telegram.org/a" in page


def test_LOC02_valid_create_redirects_to_setup(admin: Client) -> None:
    response = admin.post(NEW_URL, _form(name="Home", language="ru", period_s="45"))

    assert response.status_code == 302
    match = SETUP_PATH.fullmatch(response.url)
    assert match is not None
    location = Location.objects.get()
    assert int(match.group(1)) == location.pk
    assert location.name == "Home"
    assert (location.period_s, location.grace_s, location.language) == (45, 30, "ru")
    assert location.bot_token == GOOD_TOKEN
    assert location.chat_id == -1001234567890
    assert type(location.chat_id) is int
    assert KEY_SHAPE.fullmatch(location.device_key)
    assert (location.router_grace, location.maintenance, location.alerts_enabled) == (
        False,
        False,
        True,
    )
    state = LocationState.objects.get(location=location)
    assert (state.status, state.last_heartbeat_at) == ("waiting", None)

    setup = admin.get(response.url).content.decode()
    assert [(flash.role, flash.text) for flash in messages(setup)] == [("status", CREATED_FLASH)]
    # The flash shows once.
    assert CREATED_FLASH not in admin.get(response.url).content.decode()


@pytest.mark.django_db
def test_LOC02_create_stamps_created_at_from_the_clock(
    rf: RequestFactory, fixed_now: datetime
) -> None:
    request = rf.post(NEW_URL, _form())
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]

    response = LocationCreateView.as_view(clock=FakeClock(fixed_now))(request)

    assert response.status_code == 302
    assert Location.objects.get().created_at == fixed_now


def test_double_submit_creates_two_distinct_locations(admin: Client) -> None:
    first = admin.post(NEW_URL, _form())
    second = admin.post(NEW_URL, _form())

    assert (first.status_code, second.status_code) == (302, 302)
    assert first.url != second.url
    keys = list(Location.objects.values_list("device_key", flat=True))
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert list(LocationState.objects.values_list("status", flat=True)) == ["waiting"] * 2


def test_create_is_atomic(admin: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("state row insert failed")

    # A failure stub on the second write: the location row must not survive it.
    monkeypatch.setattr(LocationState.objects, "create", fail)

    with pytest.raises(RuntimeError, match="state row insert failed"):
        admin.post(NEW_URL, _form())

    assert Location.objects.count() == 0


def test_no_telegram_call_on_save(admin: Client, fake_telegram: FakeTelegram) -> None:
    response = admin.post(NEW_URL, _form())

    assert response.status_code == 302
    assert Location.objects.count() == 1
    assert len(fake_telegram.calls) == 0


@pytest.mark.django_db
def test_anonymous_add_form_redirects_to_sign_in(client: Client) -> None:
    assert client.get(NEW_URL).url == "/login/?next=/locations/new/"

    response = client.post(NEW_URL, _form())

    assert response.status_code == 302
    assert response.url == "/login/?next=/locations/new/"
    assert Location.objects.count() == 0
