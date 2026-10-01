"""Admin forms (D-09). Copy follows the UI-SPEC copywriting contract."""

from typing import Any

from django.contrib.auth.forms import AuthenticationForm

# One message for every sign-in failure: it never says which credential was wrong.
SIGN_IN_ERROR = "Wrong username or password. Check both and try again."


class SignInForm(AuthenticationForm):
    """The admin sign-in form (UI-SPEC screen 1, E1).

    Wrong, inactive and blank credentials all get the same form-level error, so Django's
    per-field "This field is required." never appears. The username keeps
    ``autocomplete="username"`` and ``autofocus``, the password keeps
    ``autocomplete="current-password"``, and the password is never rendered back.
    """

    error_messages = {
        "invalid_login": SIGN_IN_ERROR,
        "inactive": SIGN_IN_ERROR,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A blank field is reported by clean() with the generic error instead.
        for field in self.fields.values():
            field.required = False

    def clean(self) -> dict[str, Any]:
        if not self.cleaned_data.get("username") or not self.cleaned_data.get("password"):
            raise self.get_invalid_login_error()
        return super().clean()
