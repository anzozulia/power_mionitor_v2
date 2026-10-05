"""Toasts on a live flow and by level (UI-09, UI-12; 06-UI-SPEC Layout Shell > Toasts,
Components > Toast; TEST-STRATEGY §7.5, UI-09 row).

- The live flow: after a sign-out the sign-in page (the auth layout) shows exactly one info
  toast with SIGNED_OUT_MESSAGE in the status region, and the next GET shows none.
- By level: success, info and warning toasts sit in ``toasts-status`` (role=status), error
  toasts in ``toasts-alert`` (role=alert). ``data-level`` is the Django level tag, the tone
  icon follows the level, and warning and error toasts start with the visually hidden
  "Warning: " / "Error: " prefix (copy row shell.toast_prefix). The two warnings of the
  test message (maybe delivered, rate limited) render as warning, never as success
  (amendment A5). Every Dismiss button is named "Dismiss" (shell.dismiss).
- Lifetime marks: only a success that is not sticky has the 10 s timer bar; a success the
  view tagged ``sticky`` carries ``data-sticky``; info, warning and error stay until closed.
- Both regions are always rendered, also when empty, and messages keep arrival order.

Only the server half is pinned here: the timer, its pause on hover and focus, Dismiss and
the re-announcement are browser behaviour, checked by hand (TEST-STRATEGY §11 item 12).
Pages are read through tests/web/pages.py. The partial is rendered for a RequestFactory
request whose session-backed FallbackStorage holds the queued messages, read by Django's
messages context processor as on a real page. Python-owned copy is imported.
"""

import pytest
from bs4 import Tag
from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.test.html import Element, parse_html
from pages import Message, all_by_testid, assert_page, by_testid, messages, parse, text

from powermon.telegram.client import SendResult
from powermon.web.history_views import HISTORY_RESET_MESSAGE, RESET_REFUSED_MESSAGE
from powermon.web.location_views import CHANGES_SAVED_MESSAGE, flash_for_test_message
from powermon.web.templatetags.icons import ICONS
from powermon.web.views import SIGNED_OUT_MESSAGE

User = get_user_model()

TOASTS = "partials/_toasts.html"
# Copy rows shell.toast_prefix and shell.dismiss (06-UI-SPEC copy table).
PREFIX = {"warning": "Warning: ", "error": "Error: "}
DISMISS = "Dismiss"
# Components > Toast: the tone icon of each level.
TONE_ICONS = {
    "success": "circle-check",
    "info": "info",
    "warning": "triangle-alert",
    "error": "circle-alert",
}


def _render(*queued: tuple[int, str, str]) -> str:
    """_toasts.html for a request whose message storage holds ``queued`` (level, text, tags)."""
    request = RequestFactory().get("/")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    for level, message, extra_tags in queued:
        django_messages.add_message(request, level, message, extra_tags=extra_tags)
    return render_to_string(TOASTS, request=request)


def _region_levels(page: Tag, region: str) -> list[str]:
    """The ``data-level`` of each toast inside one toast region, in document order."""
    return [str(toast["data-level"]) for toast in all_by_testid(by_testid(page, region), "toast")]


def _timers(toast: Tag) -> list[Tag]:
    """The toast's success timer bar, if it has one."""
    return toast.select("[data-toast-timer]")


def _is_icon(svg: Tag, name: str) -> bool:
    """A parsed inline ``<svg>`` draws the shapes of the vendored icon ``name``."""
    drawn = parse_html(f"<g>{svg.decode_contents()}</g>")
    vendored = parse_html(f"<g>{ICONS[name]}</g>")
    assert isinstance(drawn, Element) and isinstance(vendored, Element)
    return drawn.children == vendored.children


# The live flow: sign out -> the signed-out toast on S1, once


@pytest.mark.django_db
def test_UI09_signed_out_toast_once(client: Client) -> None:
    client.force_login(User.objects.create_user("admin", password="not-used-here"))

    response = client.post("/logout/", follow=True)

    # Expected: one info toast in the status region of the sign-in page.
    assert response.redirect_chain == [("/login/", 302)]
    page = assert_page(response, title="Sign in", app=False)
    assert messages(page) == [Message("info", "status", SIGNED_OUT_MESSAGE)]
    (toast,) = all_by_testid(by_testid(page, "toasts-status"), "toast")
    # Info stays until it is closed: no timer bar, and no sticky mark is needed for that.
    assert _timers(toast) == [] and not toast.has_attr("data-sticky")
    assert _region_levels(page, "toasts-alert") == []
    # Edge: a flash is shown once; the next GET has both regions and no toast.
    again = assert_page(client.get("/login/"), title="Sign in", app=False)
    assert messages(again) == []
    assert all_by_testid(again, "toast") == []


# Levels, regions, tones and the Dismiss name


def test_UI09_levels_map_to_tones_and_regions() -> None:
    maybe = flash_for_test_message(SendResult("maybe_delivered", code="read_timeout"), False)
    limited = flash_for_test_message(SendResult("rate_limited", retry_after=7), False)
    # A5: both test-message warnings are warning-level flashes.
    assert maybe[0] == limited[0] == django_messages.WARNING

    html = _render(
        (django_messages.SUCCESS, CHANGES_SAVED_MESSAGE, ""),
        (django_messages.INFO, SIGNED_OUT_MESSAGE, ""),
        (maybe[0], maybe[1], ""),
        (limited[0], limited[1], ""),
        (django_messages.ERROR, RESET_REFUSED_MESSAGE, ""),
    )
    page = parse(html)

    # Errors only in the alert region, every other level only in the status region.
    assert _region_levels(page, "toasts-alert") == ["error"]
    assert _region_levels(page, "toasts-status") == ["success", "info", "warning", "warning"]
    assert messages(page) == [
        Message("error", "alert", RESET_REFUSED_MESSAGE),
        Message("success", "status", CHANGES_SAVED_MESSAGE),
        Message("info", "status", SIGNED_OUT_MESSAGE),
        Message("warning", "status", maybe[1]),
        Message("warning", "status", limited[1]),
    ]
    for toast in all_by_testid(page, "toast"):
        level = str(toast["data-level"])
        # The sr-only prefix for warning and error only, read before the text.
        assert text(toast) == PREFIX.get(level, "") + text(by_testid(toast, "toast-text"))
        # The tone icon follows the level, so a warning never looks like a success (A5).
        tone_icon = toast.find("svg")
        assert isinstance(tone_icon, Tag) and _is_icon(tone_icon, TONE_ICONS[level])
        # Failure guard: one Dismiss button per toast, named, JS-only and rendered hidden.
        (dismiss,) = toast.find_all("button")
        assert dismiss.get("aria-label") == DISMISS
        assert dismiss.get("type") == "button"
        assert dismiss.has_attr("data-js-only") and dismiss.has_attr("hidden")


# Lifetime marks: sticky and the timer bar


def test_UI09_sticky_and_timer() -> None:
    html = _render(
        (django_messages.SUCCESS, HISTORY_RESET_MESSAGE, "sticky"),
        (django_messages.SUCCESS, CHANGES_SAVED_MESSAGE, ""),
        (django_messages.INFO, SIGNED_OUT_MESSAGE, ""),
        (django_messages.WARNING, "Telegram did not answer in time.", ""),
        (django_messages.ERROR, RESET_REFUSED_MESSAGE, ""),
    )
    page = parse(html)
    sticky, plain, info, warning = all_by_testid(by_testid(page, "toasts-status"), "toast")
    (error,) = all_by_testid(by_testid(page, "toasts-alert"), "toast")

    # Expected: a sticky success has data-sticky and never times out.
    assert sticky.has_attr("data-sticky") and _timers(sticky) == []
    # Edge: a plain success has exactly one timer bar, hidden from assistive technology.
    assert not plain.has_attr("data-sticky")
    (timer,) = _timers(plain)
    assert timer.find_parent(attrs={"aria-hidden": "true"}) is not None
    # Failure guard: info, warning and error stay until dismissed, with no timer bar.
    for toast in (info, warning, error):
        assert _timers(toast) == [] and not toast.has_attr("data-sticky")


# Empty regions and arrival order


def test_UI09_empty_and_order() -> None:
    # Edge: no message -> both regions present, announced through their roles, and empty.
    empty = parse(_render())
    alert, status = by_testid(empty, "toasts-alert"), by_testid(empty, "toasts-status")
    assert (alert.get("role"), alert.get("aria-live")) == ("alert", "assertive")
    assert (status.get("role"), status.get("aria-live")) == ("status", "polite")
    assert all_by_testid(empty, "toast") == []
    assert messages(empty) == [] and text(alert) == text(status) == ""

    # Expected: two messages in one response are two toasts, in arrival order.
    html = _render(
        (django_messages.SUCCESS, CHANGES_SAVED_MESSAGE, ""),
        (django_messages.INFO, SIGNED_OUT_MESSAGE, ""),
    )
    assert messages(html) == [
        Message("success", "status", CHANGES_SAVED_MESSAGE),
        Message("info", "status", SIGNED_OUT_MESSAGE),
    ]
    # Adjacent errors stay separate toasts in the alert region, in arrival order too.
    errors = _render(
        (django_messages.ERROR, RESET_REFUSED_MESSAGE, ""),
        (django_messages.ERROR, "A second error.", ""),
    )
    assert messages(errors) == [
        Message("error", "alert", RESET_REFUSED_MESSAGE),
        Message("error", "alert", "A second error."),
    ]
