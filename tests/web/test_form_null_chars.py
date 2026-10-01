"""A NUL character in a form field gets UI-SPEC copy, never Django's text (LOC-02, LOC-01).

Django's CharField always runs ProhibitNullCharactersValidator, and it runs before a
form's ``clean_<field>`` method. Without a mapping, its "Null characters are not allowed."
would replace the 01-09 validators' copy for the bot token and the chat ID. Only a
hand-made request can carry a NUL. It must still be refused in the contract's wording,
save nothing and never reach PostgreSQL, which cannot store a NUL in text.

The messages are copied from 01-UI-SPEC.md here on purpose, so a drift in the module
constants fails these tests. No UI-SPEC row fits a NUL in the location name, so the name
keeps Django's text (a recorded residual) and its test checks only the safe refusal.
"""

import re
from html import unescape

import pytest
from conftest import DEFAULT_BOT_TOKEN
from django.contrib.auth import get_user, get_user_model
from django.test import Client

from powermon.engine.models import LocationState
from powermon.locations.models import Location
from powermon.web.admin_sync import sync_admin
from powermon.web.forms import LocationForm, SignInForm

DJANGO_NUL_TEXT = "Null characters are not allowed."
TOKEN_FORMAT_MSG = (
    "This does not look like a bot token. It should be digits, a colon, then at least 30 "
    "letters, digits, - or _ (like 123456789:AAH…)."
)
CHAT_ID_NOT_INTEGER_MSG = "Enter a whole number, like -1001234567890."
FORM_ERROR_MSG = "The location was not saved. Fix the fields marked below."
SIGN_IN_ERROR = "Wrong username or password. Check both and try again."

GOOD_CHAT_ID = "-1001234567890"
NEW_URL = "/locations/new/"

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
        "bot_token": DEFAULT_BOT_TOKEN,
        "chat_id": GOOD_CHAT_ID,
        "language": "uk",
        **overrides,
    }


def _errors(form: LocationForm | SignInForm) -> dict[str, list[str]]:
    """Every error message of a bound form, by field name ("__all__" for form-level)."""
    return {name: list(messages) for name, messages in form.errors.items()}


def _field_error(page: str, field: str) -> str | None:
    match = re.search(rf'<p class="error" id="id_{field}_error">(.*?)</p>', page, re.S)
    return unescape(match.group(1)) if match else None


def _alerts(page: str) -> list[str]:
    return [unescape(t.strip()) for t in re.findall(r'role="alert"[^>]*>([^<]*)<', page)]


# The add-location form: the 01-09 validators' copy for the token and the chat ID.


def test_LOC02_valid_token_and_chat_id_still_pass() -> None:
    form = LocationForm(data=_form())

    assert form.is_valid(), _errors(form)
    assert form.cleaned_data["bot_token"] == DEFAULT_BOT_TOKEN
    assert form.cleaned_data["chat_id"] == int(GOOD_CHAT_ID)


@pytest.mark.parametrize(
    "value",
    [
        "\x00" + DEFAULT_BOT_TOKEN,
        DEFAULT_BOT_TOKEN[:10] + "\x00" + DEFAULT_BOT_TOKEN[10:],
        DEFAULT_BOT_TOKEN + "\x00",
        "\x00",
        # Trimming removes the spaces but not the NUL, so this is not an empty token.
        "  \x00  ",
    ],
    ids=["start", "after-the-colon", "end", "nul-only", "nul-between-spaces"],
)
def test_LOC02_nul_in_token_gets_the_token_format_copy(value: str) -> None:
    form = LocationForm(data=_form(bot_token=value))

    assert not form.is_valid()
    assert _errors(form) == {"bot_token": [TOKEN_FORMAT_MSG]}


@pytest.mark.parametrize(
    "value",
    [
        "\x00" + GOOD_CHAT_ID,
        "-100\x001234567890",
        "-100\x00",
        "\x00",
        " \x00 ",
        # The NUL check runs before the 01-09 "@" check, so this gets the
        # not-an-integer copy. It is still UI-SPEC copy, and true: it is no number.
        "@my_channel\x00",
    ],
    ids=["start", "middle", "end", "nul-only", "nul-between-spaces", "username"],
)
def test_LOC02_nul_in_chat_id_gets_the_not_an_integer_copy(value: str) -> None:
    form = LocationForm(data=_form(chat_id=value))

    assert not form.is_valid()
    assert _errors(form) == {"chat_id": [CHAT_ID_NOT_INTEGER_MSG]}


def test_LOC02_nul_in_token_and_chat_id_through_the_add_form(admin: Client) -> None:
    token = DEFAULT_BOT_TOKEN + "\x00"

    response = admin.post(NEW_URL, _form(bot_token=token, chat_id="-100\x00"))

    assert response.status_code == 200
    page = response.content.decode()
    assert _alerts(page) == [FORM_ERROR_MSG]
    assert _field_error(page, "bot_token") == TOKEN_FORMAT_MSG
    assert _field_error(page, "chat_id") == CHAT_ID_NOT_INTEGER_MSG
    assert DJANGO_NUL_TEXT not in page
    # The token stays write-only after this refusal too.
    assert DEFAULT_BOT_TOKEN.partition(":")[2] not in page
    assert Location.objects.count() == 0
    assert LocationState.objects.count() == 0


def test_LOC02_nul_in_name_is_refused_and_nothing_saved(admin: Client) -> None:
    # Residual: no UI-SPEC row fits, so the field error is Django's own text. The
    # request still fails safely: 200 with the form callout, no 500, nothing saved.
    response = admin.post(NEW_URL, _form(name="Office\x00"))

    assert response.status_code == 200
    page = response.content.decode()
    assert _alerts(page) == [FORM_ERROR_MSG]
    assert _field_error(page, "name") is not None
    assert _field_error(page, "bot_token") is None
    assert _field_error(page, "chat_id") is None
    assert Location.objects.count() == 0


# The sign-in form: a NUL is a wrong credential and gets the one sign-in error.


@pytest.mark.django_db
def test_LOC01_valid_credentials_still_pass_the_sign_in_form() -> None:
    sync_admin("admin", "pw-one")

    form = SignInForm(data={"username": "admin", "password": "pw-one"})

    assert form.is_valid(), _errors(form)
    assert form.get_user().username == "admin"


@pytest.mark.parametrize(
    ("username", "password", "field"),
    [
        ("\x00admin", "pw-one", "username"),
        ("admin\x00", "pw-one", "username"),
        ("admin", "pw\x00-one", "password"),
        ("admin", "pw-one\x00", "password"),
    ],
    ids=["username-start", "username-end", "password-middle", "password-end"],
)
def test_LOC01_nul_in_credentials_gets_only_the_sign_in_error(
    username: str, password: str, field: str
) -> None:
    form = SignInForm(data={"username": username, "password": password})

    assert not form.is_valid()
    assert _errors(form) == {field: [SIGN_IN_ERROR], "__all__": [SIGN_IN_ERROR]}


@pytest.mark.django_db
def test_LOC01_nul_in_username_page_shows_only_the_sign_in_error(client: Client) -> None:
    sync_admin("admin", "pw-one")

    response = client.post("/login/", {"username": "admin\x00", "password": "pw-one"})

    assert response.status_code == 200
    page = response.content.decode()
    assert _alerts(page) == [SIGN_IN_ERROR]
    assert DJANGO_NUL_TEXT not in page
    assert "pw-one" not in page
    assert not get_user(client).is_authenticated  # type: ignore[arg-type]
