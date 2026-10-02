"""Admin forms (D-09). Copy follows the UI-SPEC copywriting contract."""

from typing import Any

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.forms.boundfield import BoundField
from django.utils.html import format_html

from powermon.locations import validators
from powermon.locations.models import LANGUAGE_CHOICES, MAX_SECONDS, MIN_SECONDS

# One message for every sign-in failure: it never says which credential was wrong.
SIGN_IN_ERROR = "Wrong username or password. Check both and try again."

# Add-location form copy (UI-SPEC copywriting contract, verbatim).
LOCATION_FORM_ERROR = "The location was not saved. Fix the fields marked below."
TOKEN_REPASTE_NOTE = "Paste the token again: it is never sent back to the browser."  # noqa: S105
NAME_MAX_LENGTH = 100
NAME_EMPTY = "Enter a name."
NAME_TOO_LONG = "Use at most 100 characters."
SECONDS_NOT_WHOLE = "Enter a whole number of seconds."
PERIOD_TOO_SHORT = "The heartbeat period must be at least 10 seconds."
GRACE_TOO_SHORT = "The grace period must be at least 10 seconds."
SECONDS_TOO_LONG = "Use at most 3600 seconds (1 hour)."
# Not in the UI-SPEC: the select offers only valid choices, so only a hand-made request
# gets this. It replaces Django's text, which would echo the posted value.
LANGUAGE_INVALID = "Choose Ukrainian, English or Russian."

HELP_NAME = "Subscribers will see this name in the weekly chart title."
HELP_PERIOD = "How often the device sends a heartbeat. 10 to 3600 seconds."
HELP_GRACE = (
    "Extra wait after a missed heartbeat. Power is reported OFF after period + grace "
    "seconds with no heartbeat (90 seconds with the defaults). 10 to 3600 seconds."
)
# The *_TOKEN_* names hold UI copy, not secrets (S105); ruff reports at the first line.
HELP_BOT_TOKEN = (
    "The token from @BotFather, like 123456789:AAH…. Each location uses its own bot. "  # noqa: S105
    "The token is saved but never shown again."
)
HELP_CHAT_ID = (
    "The channel's numeric ID, like -1001234567890. To find it, open the channel in "
    "Telegram Web (web.telegram.org/a): the number after # in the address is the ID. "
    "If it does not start with -100, add 100 after the minus sign. Make the bot an admin "
    "of the channel with the rights Post messages and Edit messages of others (editing "
    "is needed to update and pin the weekly chart)."
)
HELP_LANGUAGE = "Language of this location's alerts and weekly chart."

# Edit-location form copy (Phase 4 UI-SPEC › Edit form, verbatim).
EDIT_FORM_ERROR = "The changes were not saved. Fix the fields marked below."
NEW_TOKEN_LABEL = "New bot token"  # noqa: S105
# {masked} is validators.mask_token of the current token, in inline monospace; format_html
# escapes it. The rest is fixed copy.
HELP_NEW_BOT_TOKEN = (
    "Leave this empty to keep the current token, <code>{masked}</code>. To use another "  # noqa: S105
    "bot, paste its token from @BotFather. The token is saved but never shown again."
)

# Text inputs for secrets and IDs: no browser autofill, no spell-check underline.
_NO_ASSIST = {"autocomplete": "off", "spellcheck": "false"}


class SignInForm(AuthenticationForm):
    """The admin sign-in form (UI-SPEC screen 1, E1).

    Wrong, inactive and blank credentials, and credentials with a NUL, all get the same
    error, so Django's per-field "This field is required." and "Null characters are not
    allowed." never appear. The username keeps
    ``autocomplete="username"`` and ``autofocus``, the password keeps
    ``autocomplete="current-password"``, and the password is never rendered back.
    """

    error_messages = {
        "invalid_login": SIGN_IN_ERROR,
        "inactive": SIGN_IN_ERROR,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            # A blank field is reported by clean() with the generic error instead.
            field.required = False
            # A NUL (only a hand-made request sends one) is a wrong credential too.
            # Each form instance has its own copy of error_messages.
            field.error_messages["null_characters_not_allowed"] = SIGN_IN_ERROR

    def clean(self) -> dict[str, Any]:
        if not self.cleaned_data.get("username") or not self.cleaned_data.get("password"):
            raise self.get_invalid_login_error()
        return super().clean()


def _seconds_field(label: str, initial: int, too_short: str, help_text: str) -> forms.IntegerField:
    """A whole number of seconds, 10-3600 (D-10, K-6), with the UI-SPEC messages only."""
    return forms.IntegerField(
        label=label,
        initial=initial,
        min_value=MIN_SECONDS,
        max_value=MAX_SECONDS,
        # Django adds min and max to the number input; step keeps browsers on whole numbers.
        widget=forms.NumberInput(attrs={"step": "1"}),
        help_text=help_text,
        error_messages={
            # A browser submits "" for text typed into a number input, so empty and
            # non-numeric input share one message and "This field is required." never shows.
            "required": SECONDS_NOT_WHOLE,
            "invalid": SECONDS_NOT_WHOLE,
            "min_value": too_short,
            "max_value": SECONDS_TOO_LONG,
        },
    )


class LocationBoundField(BoundField):
    """Links the bot token's re-paste note to its input, after Django's help and error ids."""

    @property
    def aria_describedby(self) -> str | None:
        ids = super().aria_describedby
        form = self.form
        # The form decides when the note shows (LocationForm.show_token_note).
        if ids and self.name == "bot_token" and isinstance(form, LocationForm):
            if form.show_token_note:
                return f"{ids} {self.auto_id}_note"
        return ids


class LocationForm(forms.Form):
    """The add-location form (UI-SPEC screen 3; D-10 fields, D-11 write-only token, D-12).

    Validation is local only: the token and chat ID are checked for shape by the 01-09
    validators, and nothing is sent to Telegram. Every message is UI-SPEC copy; the
    validators' ValueError text is already that copy. One exception: no UI-SPEC row fits a
    NUL in the name, so that hand-made case keeps Django's "Null characters are not
    allowed." The bot token is a password input that is never rendered back, not even
    after an invalid submit.
    """

    bound_field_class = LocationBoundField
    # Printed by the template, so the copy has one source.
    form_error = LOCATION_FORM_ERROR
    token_note = TOKEN_REPASTE_NOTE

    name = forms.CharField(
        label="Name",
        max_length=NAME_MAX_LENGTH,
        help_text=HELP_NAME,
        widget=forms.TextInput(attrs={"autofocus": True}),
        error_messages={"required": NAME_EMPTY, "max_length": NAME_TOO_LONG},
    )
    period_s = _seconds_field("Heartbeat period (seconds)", 60, PERIOD_TOO_SHORT, HELP_PERIOD)
    grace_s = _seconds_field("Grace period (seconds)", 30, GRACE_TOO_SHORT, HELP_GRACE)
    # CharField refuses a NUL before clean_<field> runs. Its code maps to the copy the
    # 01-09 validator gives any value with a NUL, so Django's text never shows.
    bot_token = forms.CharField(
        label="Bot token",
        help_text=HELP_BOT_TOKEN,
        widget=forms.PasswordInput(render_value=False, attrs=_NO_ASSIST),
        error_messages={
            "required": validators.TOKEN_EMPTY,
            "null_characters_not_allowed": validators.TOKEN_FORMAT,
        },
    )
    # Text, not number: the ID is signed and 64-bit. No max_length: parse_chat_id bounds
    # the length itself and answers any oversized paste with the too-long copy.
    chat_id = forms.CharField(
        label="Channel chat ID",
        help_text=HELP_CHAT_ID,
        widget=forms.TextInput(attrs=_NO_ASSIST),
        error_messages={
            "required": validators.CHAT_ID_EMPTY,
            "null_characters_not_allowed": validators.CHAT_ID_NOT_INTEGER,
        },
    )
    language = forms.ChoiceField(
        label="Language",
        choices=LANGUAGE_CHOICES,
        initial="uk",
        help_text=HELP_LANGUAGE,
        error_messages={"required": LANGUAGE_INVALID, "invalid_choice": LANGUAGE_INVALID},
    )

    @property
    def show_token_note(self) -> bool:
        """Show the token re-paste note: after any invalid submit (the input is always empty)."""
        return self.is_bound and bool(self.errors)

    def clean_bot_token(self) -> str:
        try:
            return validators.clean_bot_token(self.cleaned_data["bot_token"])
        except ValueError as exc:
            # The message is UI copy and never contains the token.
            raise forms.ValidationError(str(exc), code="invalid") from None

    def clean_chat_id(self) -> int:
        try:
            return validators.parse_chat_id(self.cleaned_data["chat_id"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc), code="invalid") from None


class LocationEditForm(LocationForm):
    """The edit-location form (Phase 4 UI-SPEC screen C; D-07, UI-D12, SEC-04).

    The add form's fields, rules and copy, with the stored values as initial values (the
    view passes them), except the bot token: "New bot token" is optional and write-only.
    Its input never carries a value, not even after an invalid submit; empty (after
    trimming) keeps the current token, which the help shows only masked as
    ``{bot_id}:••••••••``; a typed token gets the add form's format check and copy. The
    re-paste note shows only when the invalid submit contained a non-empty token: a note
    asking to re-paste a token the admin never typed would be wrong (UI-D12).

    The form holds no switch, no device key and no state, and the view saves it through
    ``actions.update_config``, never ``form.save()`` or ``Location.save()`` (INV-02 #3).
    """

    form_error = EDIT_FORM_ERROR

    # Redefined in place: the field keeps its position between the grace period and the
    # chat ID. A NUL keeps the format copy, as on the add form.
    bot_token = forms.CharField(
        label=NEW_TOKEN_LABEL,
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs=_NO_ASSIST),
        error_messages={"null_characters_not_allowed": validators.TOKEN_FORMAT},
    )

    def __init__(self, *args: Any, current_token: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The current token only ever goes out masked (SEC-04); format_html escapes it.
        self.fields["bot_token"].help_text = format_html(
            HELP_NEW_BOT_TOKEN, masked=validators.mask_token(current_token)
        )

    @property
    def show_token_note(self) -> bool:
        """Only after an invalid submit that carried a non-empty token (UI-D12)."""
        submitted = str(self.data.get(self.add_prefix("bot_token"), ""))
        return super().show_token_note and bool(submitted.strip())

    def clean_bot_token(self) -> str:
        """``""`` keeps the current token; anything else must be a valid token."""
        if not self.cleaned_data["bot_token"].strip():
            return ""
        return super().clean_bot_token()
