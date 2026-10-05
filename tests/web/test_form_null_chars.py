"""A NUL character in a form field gets UI-SPEC copy, never Django's text (LOC-02, LOC-01).

Django's CharField always runs ProhibitNullCharactersValidator, and it runs before a
form's ``clean_<field>`` method. Without a mapping, its "Null characters are not allowed."
would replace the 01-09 validators' copy for the bot token and the chat ID. Only a
hand-made request can carry a NUL. It must still be refused in the contract's wording,
save nothing and never reach PostgreSQL, which cannot store a NUL in text.

The validator messages are copied from 01-UI-SPEC.md here on purpose, so a drift in the
module constants fails these tests. No UI-SPEC row fits a NUL in the location name, so the
name keeps Django's text (a recorded residual) and its test checks only the safe refusal.

The sign-in page (S1) and the add-location page (S4) are read through tests/web/pages.py
and their hooks only: a NUL never gives a 500, only the field error and the error summary
(R10). The last section pins the project form renderer (TemplatesSetting) that every form
renders through: the field group template, its include parameters (the S4/S6 add-on, icon,
control attributes and help disclosure) and the input and select widgets (06-UI-SPEC
Components > Form field, R3).
"""

import pytest
from bs4 import Tag
from conftest import DEFAULT_BOT_TOKEN
from django import forms
from django.conf import settings
from django.contrib.auth import get_user, get_user_model
from django.forms.renderers import TemplatesSetting
from django.template import Context, Template, TemplateSyntaxError
from django.test import Client
from pages import assert_page, by_testid, field, field_error, parse, section, text

from powermon.engine.models import LocationState
from powermon.locations.models import Location
from powermon.locations.validators import mask_token
from powermon.web.admin_sync import sync_admin
from powermon.web.forms import (
    HELP_BOT_TOKEN,
    HELP_CHAT_ID,
    LOCATION_FORM_ERROR,
    TOKEN_REPASTE_NOTE,
    LocationEditForm,
    LocationForm,
    SignInForm,
)

DJANGO_NUL_TEXT = "Null characters are not allowed."
TOKEN_FORMAT_MSG = (
    "This does not look like a bot token. It should be digits, a colon, then at least 30 "
    "letters, digits, - or _ (like 123456789:AAH…)."
)
CHAT_ID_NOT_INTEGER_MSG = "Enter a whole number, like -1001234567890."
SIGN_IN_ERROR = "Wrong username or password. Check both and try again."
# The S1 form-error alert as a screen reader reads it: its visually hidden prefix first.
SIGN_IN_ALERT = "Error: " + SIGN_IN_ERROR

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


def _summary_links(page: Tag) -> list[tuple[str, str]]:
    """(href, text) of each jump link in the S4 error summary (06-UI-SPEC N10)."""
    summary = by_testid(page, "error-summary")
    assert text(summary).startswith(f"Error: {LOCATION_FORM_ERROR}")
    return [(str(link.get("href")), text(link)) for link in summary.find_all("a")]


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

    # R10: 200 with S4, the two field errors and the summary linking to both; never a 500.
    page = assert_page(response, app=True, title="Add location")
    assert field_error(page, "bot_token") == TOKEN_FORMAT_MSG
    assert field_error(page, "chat_id") == CHAT_ID_NOT_INTEGER_MSG
    labels = {name: f.label for name, f in LocationForm.base_fields.items()}
    assert _summary_links(page) == [
        ("#id_bot_token", f"{labels['bot_token']}: {TOKEN_FORMAT_MSG}"),
        ("#id_chat_id", f"{labels['chat_id']}: {CHAT_ID_NOT_INTEGER_MSG}"),
    ]
    html = response.content.decode()
    assert DJANGO_NUL_TEXT not in html
    # The token stays write-only after this refusal too.
    assert DEFAULT_BOT_TOKEN.partition(":")[2] not in html
    assert not field(page, "bot_token").has_attr("value")
    assert Location.objects.count() == 0
    assert LocationState.objects.count() == 0


def test_LOC02_nul_in_name_is_refused_and_nothing_saved(admin: Client) -> None:
    # Residual: no UI-SPEC row fits, so the field error is Django's own text. The
    # request still fails safely: 200 with the error summary, no 500, nothing saved.
    response = admin.post(NEW_URL, _form(name="Office\x00"))

    page = assert_page(response, app=True, title="Add location")
    assert field_error(page, "name") is not None
    assert field_error(page, "bot_token") is None
    assert field_error(page, "chat_id") is None
    assert [href for href, _ in _summary_links(page)] == ["#id_name"]
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
@pytest.mark.parametrize(
    ("username", "password", "nul_field"),
    [("admin\x00", "pw-one", "username"), ("admin", "pw\x00-one", "password")],
    ids=["username", "password"],
)
def test_LOC01_nul_in_username_page_shows_only_the_sign_in_error(
    client: Client, username: str, password: str, nul_field: str
) -> None:
    sync_admin("admin", "pw-one")

    response = client.post("/login/", {"username": username, "password": password})

    # R10: 200 with the one sign-in error, never a 500 and never Django's text.
    assert response.status_code == 200
    page = parse(response)
    alert = by_testid(page, "form-error")
    assert alert.get("role") == "alert"
    assert text(alert) == SIGN_IN_ALERT
    html = response.content.decode()
    assert DJANGO_NUL_TEXT not in html
    assert "pw-one" not in html
    # The field with the NUL is marked, and its aria-describedby names its shown error.
    control = field(page, nul_field)
    assert control.get("aria-invalid") == "true"
    assert control.get("aria-describedby") == f"id_{nul_field}_error"
    assert field_error(page, nul_field) == SIGN_IN_ERROR
    assert not field(page, "password").has_attr("value")
    assert not get_user(client).is_authenticated  # type: ignore[arg-type]


# The project form renderer (TemplatesSetting; 06-UI-SPEC Components > Form field, R3)


class _Probe(forms.Form):
    """A form whose label and help hold markup, to prove the field group escapes both."""

    name = forms.CharField(label="<i>Name</i>", help_text="<b>bold</b> help")


def test_form_renderer_is_the_project_templates_setting() -> None:
    # Expected: the TemplatesSetting renderer, with django.forms after powermon.web so the
    # project's django/forms templates win over Django's own.
    assert settings.FORM_RENDERER == "django.forms.renderers.TemplatesSetting"
    apps = list(settings.INSTALLED_APPS)
    assert apps.index("django.forms") > apps.index("powermon.web")
    form = LocationForm()
    assert isinstance(form.renderer, TemplatesSetting)
    # Every widget keeps Django's stock attribute order, with the one added attribute (the
    # field look) right after name.
    for name in ("name", "period_s", "bot_token", "chat_id"):
        control = field(parse(str(form[name])), name)
        assert list(control.attrs)[:3] == ["type", "name", "class"], name
    language = field(parse(str(form["language"])), "language")
    assert language.name == "select"
    assert list(language.attrs)[:2] == ["name", "class"]
    # Edge: a hidden input keeps the stock markup, without the field look.
    hidden = parse(forms.HiddenInput().render("marker", "abc")).find("input")
    assert hidden is not None and list(hidden.attrs) == ["type", "name", "value"]


def test_form_renderer_keeps_the_token_out(admin: Client) -> None:
    secret = DEFAULT_BOT_TOKEN.partition(":")[2]
    # R3 through a real page: an invalid add-location POST with a typed token renders the
    # token input with no value at all.
    response = admin.post(NEW_URL, _form(name="", bot_token=DEFAULT_BOT_TOKEN))

    assert response.status_code == 200
    token = field(response, "bot_token")
    assert token.get("type") == "password"
    assert not token.has_attr("value")
    assert secret not in response.content.decode()
    # The same through the field group, and the widget itself even when handed the value.
    form = LocationForm(data=_form(name="", bot_token=DEFAULT_BOT_TOKEN))
    assert not form.is_valid()
    group = parse(form["bot_token"].as_field_group())
    assert not field(group, "bot_token").has_attr("value")
    assert secret not in str(group)
    rendered = forms.PasswordInput(render_value=False).render("bot_token", DEFAULT_BOT_TOKEN)
    assert secret not in rendered
    # Failure case for contrast: a text input does send its bound value back.
    assert 'value="Office"' in forms.TextInput().render("name", "Office")


def test_form_field_group_contract() -> None:
    # Expected: an invalid token field group, in order: the label for the input, the input,
    # the first error, the re-paste note and the help; the input's aria-describedby names
    # exactly those ids (Django's wiring plus the project's note id) and is aria-invalid.
    form = LocationForm(data=_form(bot_token="\x00"))
    assert not form.is_valid()
    group = parse(form["bot_token"].as_field_group())
    wrapper = group.find("div")
    assert wrapper is not None
    children = [
        (child.name, child.get("for") or child.get("id"))
        for child in wrapper.find_all(True, recursive=False)
    ]
    assert children == [
        ("label", "id_bot_token"),
        ("input", "id_bot_token"),
        ("p", "id_bot_token_error"),
        ("p", "id_bot_token_note"),
        ("p", "id_bot_token_helptext"),
    ]
    assert text(group.find("label")) == "Bot token"
    token = field(group, "bot_token")
    assert token.get("aria-invalid") == "true"
    assert token.get("aria-describedby") == (
        "id_bot_token_helptext id_bot_token_error id_bot_token_note"
    )
    assert field_error(group, "bot_token") == TOKEN_FORMAT_MSG
    assert text(section(group, "id_bot_token_note")) == TOKEN_REPASTE_NOTE
    assert text(section(group, "id_bot_token_helptext")) == HELP_BOT_TOKEN

    # Edge: an unbound field has no error, no note and no invalid mark; the help stays linked.
    blank = parse(LocationForm()["bot_token"].as_field_group())
    assert field_error(blank, "bot_token") is None
    assert blank.find_all(id="id_bot_token_note") == []
    assert not field(blank, "bot_token").has_attr("aria-invalid")
    assert field(blank, "bot_token").get("aria-describedby") == "id_bot_token_helptext"

    # Edge: the edit form shows no note when no token was typed, and its help is the
    # format_html string, so the mask stays in its code element.
    edit = LocationEditForm(data=_form(name="", bot_token=""), current_token=DEFAULT_BOT_TOKEN)
    assert not edit.is_valid()
    edit_group = parse(edit["bot_token"].as_field_group())
    assert edit_group.find_all(id="id_bot_token_note") == []
    (code,) = section(edit_group, "id_bot_token_helptext").find_all("code")
    assert text(code) == mask_token(DEFAULT_BOT_TOKEN)


def test_form_field_group_escapes_and_shows_the_first_error() -> None:
    probe = _Probe(data={"name": ""})
    assert not probe.is_valid()
    probe.add_error("name", "A second error.")
    group = parse(probe["name"].as_field_group())

    # Failure case: markup in a plain label or help is text, never elements (R1).
    assert group.find("i") is None and group.find("b") is None
    assert text(group.find("label")) == "<i>Name</i>"
    assert text(section(group, "id_name_helptext")) == "<b>bold</b> help"
    # Only the first error shows; the field is still marked invalid.
    assert field_error(group, "name") == "This field is required."
    assert field(group, "name").get("aria-invalid") == "true"


def _included(bound: forms.BoundField, params: str = "") -> str:
    """The field group included the way S4 and S6 include it, with ``params``."""
    source = '{% include "django/forms/field.html" with field=bound ' + params + " only %}"
    return Template(source).render(Context({"bound": bound}))


def test_form_field_group_include_parameters() -> None:
    form = LocationForm()
    # Expected: with no parameter the include is exactly the plain field group (the
    # renderer strips the template's trailing newline).
    for name in form.fields:
        assert _included(form[name]).strip() == form[name].as_field_group(), name

    # The seconds fields: the "s" add-on (aria-hidden: the label names the unit) and
    # inputmode numeric; every other attribute is Django's own, unchanged.
    seconds = parse(_included(form["period_s"], 'suffix="s" control="seconds"'))
    control = field(seconds, "period_s")
    assert control.get("inputmode") == "numeric"
    plain = field(parse(str(form["period_s"])), "period_s")
    assert {key: value for key, value in control.attrs.items() if key != "inputmode"} == (
        plain.attrs
    )
    add_ons = [
        found
        for found in seconds.find_all(attrs={"aria-hidden": "true"})
        if found.get_text(strip=True) == "s"
    ]
    assert len(add_ons) == 1

    # The token: the key icon inside the field and the password-manager ignore hints.
    token = parse(_included(form["bot_token"], 'leading_icon="key-round" control="secret"'))
    secret = field(token, "bot_token")
    hints = ("data-1p-ignore", "data-lpignore", "data-bwignore")
    assert [secret.get(hint) for hint in hints] == ["", "true", ""]
    assert not secret.has_attr("value")
    assert [svg.get("aria-hidden") for svg in token.find_all("svg")] == ["true"]
    # Edge: the sign-in password renders the plain group, so managers still fill it in.
    password = field(parse(SignInForm()["password"].as_field_group()), "password")
    assert not any(password.has_attr(hint) for hint in hints)

    # The chat ID: the help in a closed disclosure, still linked to its input.
    chat = parse(
        _included(
            form["chat_id"], 'help_in_details="How to find the chat ID" help_testid="chat-id-help"'
        )
    )
    details = by_testid(chat, "chat-id-help")
    assert details.name == "details"
    assert not details.has_attr("open")
    summary = details.find("summary")
    assert isinstance(summary, Tag)
    assert text(summary) == "How to find the chat ID"
    assert text(section(details, "id_chat_id_helptext")) == HELP_CHAT_ID
    assert field(chat, "chat_id").get("aria-describedby") == "id_chat_id_helptext"

    # Failure: an unknown control kind fails the render instead of dropping attributes.
    with pytest.raises(TemplateSyntaxError, match="unknown kind"):
        _included(form["name"], 'control="spinner"')
