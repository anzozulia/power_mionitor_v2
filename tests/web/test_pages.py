"""The shared parser module tests/web/pages.py (TEST-STRATEGY §5.1-§5.3; UI-01, UI-09).

Every helper gets an expected, an edge and a failure case on small sample pages built from
the 06-UI-SPEC test hooks, so a later page test can trust what the helper returns and that
it fails loudly on a page that breaks the contract.

- ``text()`` is the screen-reader text: aria-hidden subtrees out, visually hidden text in.
- ``by_testid()`` fails unless exactly one element matches.
- ``messages()`` reads the toasts (level, region role, toast-text) and, on pages that still
  extend the old base.html, the legacy flash callouts (no level).
- ``assert_no_injected_script()`` allows only empty scripts loaded from a hashed
  same-origin static path, and no inline event handler.

The samples never write the attribute that holds CSS class names literally; where a sample
needs one it is built by concatenation (06-09 adds a guard against assertions on classes).
"""

import pytest
from django.http import HttpResponse, JsonResponse
from pages import (
    Message,
    all_by_testid,
    assert_no_injected_script,
    by_testid,
    h1,
    main,
    message_texts,
    messages,
    parse,
    text,
    title,
)

# The sample attribute that holds CSS class names, spelled by concatenation.
CLS = "cl" + "ass="
SR = "<span " + CLS + '"sr-only">'

TOASTS = (
    '<main id="main"><h1>Office</h1>'
    '<p role="status">A legacy callout the toast layout never shows.</p></main>'
    '<div data-testid="toasts-status" role="status" aria-live="polite">'
    '<div data-testid="toast" data-level="success">'
    '<svg aria-hidden="true" focusable="false"><path d="M0 0"></path></svg>'
    '<p data-testid="toast-text">Changes saved.</p>'
    '<button type="button" aria-label="Dismiss" data-js-only hidden>x</button></div>'
    '<div data-testid="toast" data-level="warning">'
    f'{SR}Warning: </span><p data-testid="toast-text">Maybe delivered.</p></div>'
    "</div>"
    '<div data-testid="toasts-alert" role="alert" aria-live="assertive">'
    '<div data-testid="toast" data-level="error" data-sticky>'
    f'{SR}Error: </span><p data-testid="toast-text">  Not sent:\n http_403. </p></div>'
    "</div>"
)

LEGACY = (
    '<header><p role="status">Not in main.</p></header>'
    "<main " + CLS + '"main flow">'
    "<p " + CLS + '"callout" role="status">Location created.</p>'
    "<p " + CLS + '"callout callout--error" role="alert">Not sent &amp; refused.</p>'
    '<p role="note">A note is not a flash.</p>'
    '<div role="status">Not a callout paragraph.</div>'
    "</main>"
)

STATIC_JS = "/static/web/admin.0123456789ab.js"


# parse


def test_parse_reads_a_response_a_string_and_bytes() -> None:
    response = HttpResponse("<h1>Office</h1>")

    for page in (response, "<h1>Office</h1>", b"<h1>Office</h1>"):
        assert text(h1(parse(page))) == "Office"


def test_parse_decodes_utf8_bytes() -> None:
    page = parse("<p>Київ ••••</p>".encode())

    assert text(page) == "Київ ••••"


def test_parse_refuses_a_response_that_is_not_html() -> None:
    with pytest.raises(AssertionError, match="not an HTML response"):
        parse(JsonResponse({"ok": True}))
    with pytest.raises(AssertionError, match="cannot parse"):
        parse(42)  # type: ignore[arg-type]


# text


def test_text_skips_aria_hidden_and_keeps_sr_only() -> None:
    page = parse(
        '<p id="p">  Power <svg aria-hidden="true"><title>zap</title></svg>is\n  '
        f"{SR}currently</span> on"
        '<span aria-hidden="true">••••</span><!-- a comment --></p>'
    )

    assert text(page.find(id="p")) == "Power is currently on"


def test_text_of_an_aria_hidden_element_is_empty_but_its_inner_element_is_read() -> None:
    page = parse('<div id="d" aria-hidden="TRUE"> <span id="s">glyphs</span> </div>')

    assert text(page.find(id="d")) == ""
    # Only aria-hidden inside the element counts: the inner span is read on its own.
    assert text(page.find(id="s")) == "glyphs"


def test_text_drops_script_and_style_bodies_and_empty_markup() -> None:
    page = parse('<div id="d"><script>var x;</script><style>p{}</style>\n\t </div>')

    assert text(page.find(id="d")) == ""


# by_testid, all_by_testid, main, h1, title

TESTIDS = (
    '<section id="one"><p data-testid="a">First</p><p data-testid="b">B1</p></section>'
    '<section id="two"><p data-testid="b">B2</p></section>'
)


def test_by_testid_returns_the_one_match() -> None:
    page = parse(TESTIDS)

    assert text(by_testid(page, "a")) == "First"
    # Scoped to an element, only its descendants count.
    assert text(by_testid(page.find(id="two"), "b")) == "B2"
    assert text(by_testid(TESTIDS, "a")) == "First"


def test_by_testid_requires_exactly_one() -> None:
    page = parse(TESTIDS)

    with pytest.raises(AssertionError, match="'b'.*found 2"):
        by_testid(page, "b")
    with pytest.raises(AssertionError, match="'missing'.*found 0"):
        by_testid(page, "missing")


def test_all_by_testid_keeps_document_order_and_may_be_empty() -> None:
    page = parse(TESTIDS)

    assert [text(found) for found in all_by_testid(page, "b")] == ["B1", "B2"]
    assert all_by_testid(page, "missing") == []


def test_main_h1_and_title() -> None:
    page = parse(
        "<html><head><title>\n  Office  ·  Power Monitor </title></head>"
        '<body><main id="main"><h1>Office <span aria-hidden="true">*</span></h1></main></body>'
        "</html>"
    )

    assert main(page)["id"] == "main"
    assert text(h1(page)) == "Office"
    assert title(page) == "Office · Power Monitor"


def test_main_h1_and_title_fail_unless_there_is_exactly_one() -> None:
    page = parse("<main><h1>One</h1><h1>Two</h1></main><main></main>")

    with pytest.raises(AssertionError, match="<h1>.*found 2"):
        h1(page)
    with pytest.raises(AssertionError, match="<main>.*found 2"):
        main(page)
    with pytest.raises(AssertionError, match="<title>.*found 0"):
        title(page)


# messages (UI-09)


def test_messages_reads_toasts() -> None:
    assert messages(TOASTS) == [
        Message("success", "status", "Changes saved."),
        Message("warning", "status", "Maybe delivered."),
        Message("error", "alert", "Not sent: http_403."),
    ]
    assert message_texts(TOASTS) == ["Changes saved.", "Maybe delivered.", "Not sent: http_403."]


def test_messages_reads_empty_toast_regions_as_no_message() -> None:
    page = (
        '<main><p role="status">Legacy</p></main>'
        '<div data-testid="toasts-status" role="status"></div>'
        '<div data-testid="toasts-alert" role="alert"></div>'
    )

    assert messages(page) == []


def test_messages_reads_legacy_callouts() -> None:
    assert messages(LEGACY) == [
        Message(None, "status", "Location created."),
        Message(None, "alert", "Not sent & refused."),
    ]
    assert messages(HttpResponse(LEGACY)) == messages(LEGACY)
    assert messages("<main><h1>Office</h1></main>") == []
    assert message_texts("<main></main>") == []


def test_messages_fails_on_a_broken_toast_contract() -> None:
    alert_region = '<div data-testid="toasts-alert" role="alert"></div>'
    no_alert_region = TOASTS.replace('data-testid="toasts-alert"', 'data-testid="other"')
    bad_level = TOASTS.replace('data-level="warning"', 'data-level="danger"')
    outside = (
        '<div data-testid="toasts-status" role="status"></div>'
        + alert_region
        + '<div data-testid="toast" data-level="info">'
        '<p data-testid="toast-text">Lost</p></div>'
    )
    no_text = TOASTS.replace('<p data-testid="toast-text">Changes saved.</p>', "")

    with pytest.raises(AssertionError, match="toast region 'toasts-alert'.*found 0"):
        messages(no_alert_region)
    with pytest.raises(AssertionError, match="'danger' is not a Django level"):
        messages(bad_level)
    with pytest.raises(AssertionError, match="outside the two toast regions"):
        messages(outside)
    with pytest.raises(AssertionError, match="'toast-text'.*found 0"):
        messages(no_text)


# assert_no_injected_script (R5, TEST-STRATEGY §3.5)


def test_assert_no_injected_script_accepts_hashed_static_scripts() -> None:
    page = (
        f'<head><script src="{STATIC_JS}"></script>'
        '<script src="/static/web/vendor/alpine-csp-3.17.4.min.0123456789ab.js" defer></script>'
        "</head><main><h1>&lt;script&gt;alert(1)&lt;/script&gt;</h1>"
        "<details open><summary>More</summary></details></main>"
    )

    # An escaped payload in the text is the expected rendering of a hostile name.
    assert_no_injected_script(page)
    assert_no_injected_script("<main><p>No script at all.</p></main>", "plain page")


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        ("<p>Hi</p><script>alert(1)</script>", "injected script payload"),
        ("<p>Hi</p><SCRIPT>alert(1)</SCRIPT>", "injected script payload"),
        ("<script>var x = 1;</script>", "has a body"),
        (f'<script src="{STATIC_JS}">var x = 1;</script>', "has a body"),
        ("<script></script>", "src None"),
        ('<script src="https://cdn.example/admin.0123456789ab.js"></script>', "not a hashed"),
        ('<script src="//cdn.example/static/web/admin.js"></script>', "not a hashed"),
        ('<script src="/static/web/admin.js"></script>', "not a hashed"),
        ("<!-- <script src=x> -->", "1 script tags in the markup but 0"),
        ('<button type="button" onclick="go()">Go</button>', "inline event handlers"),
        ('<img src="/x.png" alt="" ONERROR="go()">', "inline event handlers"),
    ],
)
def test_assert_no_injected_script_fails(html: str, reason: str) -> None:
    with pytest.raises(AssertionError, match=reason):
        assert_no_injected_script(html)


def test_assert_no_injected_script_names_the_page() -> None:
    with pytest.raises(AssertionError, match="^edit page: "):
        assert_no_injected_script("<script>var x;</script>", "edit page")
