"""The shared partials every layout includes: toasts, the alert / banner and the button
(UI-09, UI-12; 06-UI-SPEC Layout Shell > Toasts, Components > Toast, Alert / banner, Button).

- partials/_toasts.html always renders both live regions, also when there is no message:
  ``toasts-alert`` (role=alert, assertive) holds the error toasts and ``toasts-status``
  (role=status, polite) every other level, each region bound to the ``toasts`` component.
  A toast carries its Django level tag in ``data-level``, its text in ``toast-text`` after a
  visually hidden "Warning: " / "Error: " prefix, ``data-sticky`` when the view tagged it
  sticky, the 10 s timer bar only on a success that is not sticky, and a Dismiss button that
  is JS-only and rendered hidden. Messages render in arrival order and are escaped (R1).
- partials/_alert.html renders ``data-tone`` for the five tones, the prefix only for warning
  and error, the hatch stripe only for muted, a role only when one is passed and an optional
  action template.
- partials/_button.html renders ``data-variant`` and never the disabled attribute; an href
  makes it a link.
- admin.js registers the toasts component in its alpine:init listener and installs the
  delegated submit guard and the pageshow reset (node --check proves it parses; the browser
  behaviour is a manual UAT check).

Pages are read through tests/web/pages.py, never through classes or raw markup. Messages are
queued on a RequestFactory request with session-backed FallbackStorage and read by Django's
messages context processor, as on a real page.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from bs4 import Tag
from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.template.loader import render_to_string
from django.test import RequestFactory
from pages import (
    Message,
    all_by_testid,
    assert_no_injected_script,
    by_testid,
    messages,
    parse,
    text,
)

TOASTS = "partials/_toasts.html"
ALERT = "partials/_alert.html"
BUTTON = "partials/_button.html"
ADMIN_JS = Path(settings.BASE_DIR) / "powermon" / "web" / "static" / "web" / "admin.js"
# Copy rows shell.toast_prefix and shell.dismiss (06-UI-SPEC copy table).
PREFIX = {"warning": "Warning: ", "error": "Error: "}
DISMISS = "Dismiss"
TONES = ("success", "info", "warning", "error", "muted")
VARIANTS = ("primary", "secondary", "ghost", "danger", "outline-danger")
# A level with no tag of its own (MESSAGE_LEVEL lets it through): it renders as info.
CUSTOM_LEVEL = 35


def _render_toasts(*queued: tuple[int, str, str], **context: Any) -> str:
    """_toasts.html for a request whose message storage holds ``queued`` (level, text, tags)."""
    request = RequestFactory().get("/")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    for level, message, extra_tags in queued:
        django_messages.add_message(request, level, message, extra_tags=extra_tags)
    return render_to_string(TOASTS, context, request=request)


def _toasts(html: str) -> list[Tag]:
    return all_by_testid(parse(html), "toast")


def _dismiss(toast: Tag) -> Tag:
    buttons = toast.find_all("button", attrs={"aria-label": DISMISS})
    assert len(buttons) == 1, f"expected one Dismiss button, found {len(buttons)}"
    button = buttons[0]
    assert isinstance(button, Tag)
    return button


def _timers(toast: Tag) -> list[Tag]:
    return toast.select("[data-toast-timer]")


# Toast regions (UI-09, UI-12)


def test_toast_regions_always_present() -> None:
    for html in (_render_toasts(), _render_toasts(auth=True), render_to_string(TOASTS)):
        page = parse(html)
        alert = by_testid(page, "toasts-alert")
        status = by_testid(page, "toasts-status")

        assert (alert.get("role"), alert.get("aria-live")) == ("alert", "assertive")
        assert (status.get("role"), status.get("aria-live")) == ("status", "polite")
        assert alert.get("x-data") == status.get("x-data") == "toasts"
        # Empty: no toast and no text in either region.
        assert _toasts(html) == []
        assert messages(page) == []
        assert text(alert) == text(status) == ""


def test_toast_levels_and_regions() -> None:
    html = _render_toasts(
        (django_messages.SUCCESS, "Location created.", ""),
        (django_messages.INFO, "Nothing changed.", ""),
        (django_messages.WARNING, "Telegram did not answer.", ""),
        (django_messages.ERROR, "The bot was blocked.", ""),
    )

    # Errors in the alert region, every other level in the status region; data-level is the
    # level tag and Message.text is the toast text without its prefix.
    assert messages(html) == [
        Message("error", "alert", "The bot was blocked."),
        Message("success", "status", "Location created."),
        Message("info", "status", "Nothing changed."),
        Message("warning", "status", "Telegram did not answer."),
    ]
    for toast in _toasts(html):
        level = str(toast["data-level"])
        toast_text = text(by_testid(toast, "toast-text"))
        # The visually hidden prefix comes first, for warning and error only.
        assert text(toast) == PREFIX.get(level, "") + toast_text
        # Every toast can be dismissed, but only with JavaScript.
        dismiss = _dismiss(toast)
        assert dismiss.has_attr("data-js-only") and dismiss.has_attr("hidden")
        assert dismiss.get("type") == "button"
        # Only a success that is not sticky gets the timer bar.
        assert len(_timers(toast)) == (1 if level == "success" else 0)
        assert not toast.has_attr("data-sticky")


def test_toast_sticky_success_has_no_timer() -> None:
    html = _render_toasts(
        (django_messages.SUCCESS, "Location created. Set up the device next.", "sticky"),
        (django_messages.SUCCESS, "Saved.", ""),
        (django_messages.ERROR, "Not removed.", "sticky"),
    )
    sticky_success, plain_success = all_by_testid(by_testid(parse(html), "toasts-status"), "toast")
    (sticky_error,) = all_by_testid(by_testid(parse(html), "toasts-alert"), "toast")

    assert sticky_success.has_attr("data-sticky") and _timers(sticky_success) == []
    assert not plain_success.has_attr("data-sticky") and len(_timers(plain_success)) == 1
    # The timer bar is decoration: hidden from assistive technology.
    assert all(
        timer.find_parent(attrs={"aria-hidden": "true"}) is not None
        for timer in _timers(plain_success)
    )
    # Errors never time out; the tag still marks them.
    assert sticky_error.has_attr("data-sticky") and _timers(sticky_error) == []


def test_toasts_keep_arrival_order() -> None:
    html = _render_toasts(
        (django_messages.SUCCESS, "First.", ""),
        (django_messages.ERROR, "Second.", ""),
        (django_messages.SUCCESS, "Third.", ""),
        (django_messages.ERROR, "Fourth.", ""),
    )

    # Adjacent messages are separate toasts, each region in arrival order.
    assert messages(html) == [
        Message("error", "alert", "Second."),
        Message("error", "alert", "Fourth."),
        Message("success", "status", "First."),
        Message("success", "status", "Third."),
    ]


def test_toast_unknown_level_renders_as_info() -> None:
    html = _render_toasts((CUSTOM_LEVEL, "A custom level.", ""))

    assert messages(html) == [Message("info", "status", "A custom level.")]


def test_toast_long_text_wraps() -> None:
    words = " ".join(f"word{n:03d}" for n in range(60))[:399] + "."
    unbroken = "x" * 400
    html = _render_toasts(
        (django_messages.ERROR, words, ""), (django_messages.SUCCESS, unbroken, "")
    )

    # The whole text is in the toast: nothing truncated, with or without spaces (E11).
    assert len(words) == len(unbroken) == 400
    assert [message.text for message in messages(html)] == [words, unbroken]


def test_toast_text_is_escaped() -> None:
    payload = '<script>alert(1)</script><img src=x alt="">'
    html = _render_toasts((django_messages.ERROR, payload, ""))

    # Autoescaped (R1): the payload is text, never markup.
    assert_no_injected_script(html, "toasts")
    assert parse(html).find("img") is None
    assert messages(html) == [Message("error", "alert", payload)]


# Alert / banner (UI-09, UI-12)


def _render_alert(**context: Any) -> Tag:
    page = parse(render_to_string(ALERT, context))
    found = page.select("[data-tone]")
    assert len(found) == 1, f"expected one alert, found {len(found)}"
    return found[0]


def _stripes(alert: Tag) -> list[Tag]:
    """Decorative empty elements: the hatch stripe of the muted tone (icons are svg)."""
    return [
        span
        for span in alert.find_all("span", attrs={"aria-hidden": "true"})
        if isinstance(span, Tag) and not span.get_text(strip=True) and span.find(True) is None
    ]


@pytest.mark.parametrize("tone", TONES)
def test_alert_tones(tone: str) -> None:
    alert = _render_alert(tone=tone, title="Delivery failing", body="Telegram answered 403.")
    title, body = alert.find_all("p")

    assert alert["data-tone"] == tone
    # The prefix only for warning and error, before the title (read per element: text()
    # does not separate adjacent blocks).
    assert text(alert).startswith(PREFIX.get(tone, "") + "Delivery failing")
    assert ("Warning:" in text(alert), "Error:" in text(alert)) == (
        tone == "warning",
        tone == "error",
    )
    assert (text(title), text(body)) == ("Delivery failing", "Telegram answered 403.")
    # The hatch stripe only for muted (maintenance).
    assert len(_stripes(alert)) == (1 if tone == "muted" else 0)
    # Not a live message unless a role is passed; one tone icon, hidden from AT.
    assert not alert.has_attr("role")
    icons = alert.find_all("svg")
    assert len(icons) == 1 and icons[0].get("aria-hidden") == "true"


def test_alert_role_testid_and_body_only() -> None:
    alert = _render_alert(
        tone="error", body="The sign-in failed.", role="alert", testid="form-error"
    )

    assert (alert["role"], alert["data-testid"]) == ("alert", "form-error")
    # Without a title the prefix still comes first.
    assert [text(p) for p in alert.find_all("p")] == ["The sign-in failed."]
    assert text(alert).startswith("Error: The sign-in failed.")


def test_alert_action_template() -> None:
    alert = _render_alert(
        tone="warning",
        title="Delivery failing",
        action_template=BUTTON,
        variant="primary",
        label="Send test message",
    )

    buttons = alert.find_all("button")
    assert len(buttons) == 1 and text(buttons[0]) == "Send test message"
    assert buttons[0]["data-variant"] == "primary"
    # Without an action template there is no button.
    assert _render_alert(tone="warning", title="Delivery failing").find("button") is None


def test_alert_escapes_its_values() -> None:
    alert = _render_alert(tone="info", title="<b>x</b>", body='<a href="/x">y</a>')

    assert alert.find("b") is None and alert.find("a") is None
    assert [text(p) for p in alert.find_all("p")] == ["<b>x</b>", '<a href="/x">y</a>']


# Button (UI-09, UI-12)


@pytest.mark.parametrize("variant", VARIANTS)
def test_button_variants(variant: str) -> None:
    button = parse(render_to_string(BUTTON, {"variant": variant, "label": "Save"})).find(True)
    link = parse(
        render_to_string(BUTTON, {"variant": variant, "label": "Edit", "href": "/locations/1/"})
    ).find(True)

    assert isinstance(button, Tag) and isinstance(link, Tag)
    assert (button.name, button["data-variant"], button.get("type")) == (
        "button",
        variant,
        "submit",
    )
    assert (link.name, link["data-variant"], link["href"]) == ("a", variant, "/locations/1/")
    # Pending is aria-busy + aria-disabled from the submit guard, never disabled (UI-09).
    assert not button.has_attr("disabled") and not link.has_attr("disabled")


def test_button_pending_label() -> None:
    button = parse(
        render_to_string(
            BUTTON,
            {"variant": "secondary", "label": "Send test message", "pending_label": "Sending…"},
        )
    ).find("button")

    assert isinstance(button, Tag)
    assert button["data-pending-label"] == "Sending…"
    assert not button.has_attr("disabled")


# admin.js (UI-09): the toasts component, the submit guard and the pageshow reset


def test_admin_js_registers_toasts_and_the_submit_guard() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # One alpine:init listener registering the toasts component.
    assert source.count('document.addEventListener("alpine:init"') == 1
    assert source.count('Alpine.data("toasts"') == 1
    # One delegated submit guard and one pageshow reset, at the top level.
    assert source.count('document.addEventListener("submit"') == 1
    assert source.count('window.addEventListener("pageshow"') == 1
    for marker in ("aria-busy", "aria-disabled", "data-pending-label", "defaultPrevented"):
        assert marker in source, marker
    # The rail flag stays the first statement's key.
    assert "powermon.sidebar.rail" in source


def test_admin_js_never_disables_or_resubmits() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Never the disabled attribute or property (its name/value would not post), and the
    # guard never submits a form itself (UI-09, Pitfall 12).
    assert re.search(r"\.disabled\s*=|setAttribute\(\s*[\"']disabled[\"']", source) is None
    assert re.search(r"\.(requestSubmit|submit)\s*\(", source) is None
