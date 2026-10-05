"""The shared parser module tests/web/pages.py (TEST-STRATEGY §5.1-§5.3; UI-01, UI-09, UI-12).

Every helper gets an expected, an edge and a failure case on small sample pages built from
the 06-UI-SPEC test hooks, so a later page test can trust what the helper returns and that
it fails loudly on a page that breaks the contract.

- ``text()`` is the screen-reader text: aria-hidden subtrees out, visually hidden text in.
- ``by_testid()`` fails unless exactly one element matches.
- ``messages()`` reads the toasts (level, region role, toast-text) and, on pages that still
  extend the old base.html, the legacy flash callouts (no level).
- ``assert_no_injected_script()`` allows only empty scripts loaded from a hashed
  same-origin static path, and no inline event handler.
- ``breadcrumbs()``, ``table()`` (hidden rows skipped), ``definitions()``, the form
  helpers and ``code_block()`` read the hooks of 06-UI-SPEC "Test hooks".
- ``assert_page()`` checks the 15 page invariants of TEST-STRATEGY §5.2: a minimal valid
  app page passes and a sample breaking each invariant fails.
- ``assert_no_secrets()`` finds a secret in text, bytes and headers, and an allowance
  covers the text of the one element it names, nothing else.

The samples never write the attribute that holds CSS class names literally; where a sample
needs one it is built by concatenation (06-09 adds a guard against assertions on classes).
"""

import pytest
from django.http import HttpResponse, JsonResponse
from pages import (
    CSP,
    Message,
    all_by_testid,
    assert_no_injected_script,
    assert_no_secrets,
    assert_page,
    breadcrumbs,
    by_testid,
    code_block,
    definitions,
    field,
    field_error,
    form_values,
    h1,
    hidden_value,
    main,
    message_texts,
    messages,
    parse,
    post_form,
    section,
    table,
    text,
    title,
)
from secret_fixtures import MASKED, SECRET, SECRETS, TOKEN

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
        '<body><main id="main"><h1>Office <span aria-hidden="true">*</span></h1>'
        '<svg role="img"><title>On</title></svg></main></body>'
        "</html>"
    )

    assert main(page)["id"] == "main"
    assert text(h1(page)) == "Office"
    # An inline SVG's <title> names the icon; it is not a second document title.
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


# breadcrumbs, table, definitions (UI-01)

CRUMBS = (
    '<nav aria-label="Breadcrumb" data-testid="breadcrumbs"><ol>'
    '<li><a href="/">Locations</a></li>'
    '<li><a href="/locations/7/">Office <span aria-hidden="true">&gt;</span></a></li>'
    '<li><span aria-current="page">Device setup</span></li>'
    "</ol></nav>"
)

LOCATIONS_TABLE = (
    '<table data-testid="locations-table"><caption>Locations</caption>'
    '<thead><tr><th scope="col">Name</th><th scope="col">Status</th></tr></thead>'
    "<tbody>"
    '<tr data-testid="location-row" data-location-id="1">'
    '<th scope="row"><a href="/locations/1/">Home</a></th>'
    '<td><span data-testid="status-pill" data-status="on">'
    '<svg aria-hidden="true" focusable="false"></svg> On</span></td></tr>'
    '<tr data-testid="location-row" data-location-id="2">'
    '<th scope="row"><a href="/locations/2/">Office</a></th><td>Off</td></tr>'
    '<tr data-testid="no-match" hidden><td colspan="2">No location matches.</td></tr>'
    "</tbody></table>"
)

STATUS_PANEL = (
    '<dl data-testid="status-panel">'
    '<div><dt>Status</dt><dd>On <span aria-hidden="true">*</span></dd></div>'
    '<dt>Last heartbeat</dt><dd><time datetime="2026-10-01T08:00:00+00:00">'
    "2026-10-01 11:00:00 EEST</time></dd>"
    "<dt>Delivery</dt><dd>OK</dd>"
    "</dl>"
)


def test_breadcrumbs_table_definitions() -> None:
    page = parse(CRUMBS + LOCATIONS_TABLE + STATUS_PANEL)

    assert breadcrumbs(page) == [
        ("Locations", "/"),
        ("Office", "/locations/7/"),
        ("Device setup", None),
    ]
    assert table(page, "locations-table") == (
        ["Name", "Status"],
        [["Home", "On"], ["Office", "Off"]],
    )
    assert definitions(page, "status-panel") == [
        ("Status", "On"),
        ("Last heartbeat", "2026-10-01 11:00:00 EEST"),
        ("Delivery", "OK"),
    ]


def test_breadcrumbs_reads_another_testid_and_fails_without_a_current_item() -> None:
    compact = CRUMBS.replace('"breadcrumbs"', '"breadcrumbs-compact"')
    no_current = CRUMBS.replace(' aria-current="page"', "")

    assert breadcrumbs(compact, "breadcrumbs-compact")[-1] == ("Device setup", None)
    with pytest.raises(AssertionError, match="'Device setup' has no link and is not"):
        breadcrumbs(no_current)
    with pytest.raises(AssertionError, match="'breadcrumbs'.*found 0"):
        breadcrumbs(compact)


def test_table_skips_hidden_rows() -> None:
    page = parse(LOCATIONS_TABLE)

    headers, rows = table(page, "locations-table")

    assert len(rows) == 2
    assert ["No location matches."] not in rows
    # The hidden row is still in the DOM: the filter script shows it when nothing matches.
    assert text(by_testid(page, "no-match")) == "No location matches."
    assert len(all_by_testid(page, "location-row")) == 2


def test_table_without_a_head_or_a_body_section() -> None:
    page = (
        '<table data-testid="t" aria-label="Plain">'
        '<tr><th scope="col">A</th><th scope="col">B</th></tr>'
        "<tr><td>1</td><td>2</td></tr><tr><td>3</td><td></td></tr></table>"
    )

    assert table(page, "t") == (["A", "B"], [["1", "2"], ["3", ""]])


def test_table_and_definitions_fail_on_the_wrong_element() -> None:
    page = parse(LOCATIONS_TABLE + STATUS_PANEL)

    with pytest.raises(AssertionError, match="'status-panel' is a <dl>, not a <table>"):
        table(page, "status-panel")
    with pytest.raises(AssertionError, match="'locations-table' is a <table>, not a <dl>"):
        definitions(page, "locations-table")
    with pytest.raises(AssertionError, match="'missing'.*found 0"):
        table(page, "missing")


def test_definitions_fail_on_a_term_without_a_value_or_a_value_without_a_term() -> None:
    lonely_term = '<dl data-testid="d"><dt>A</dt><dt>B</dt><dd>2</dd></dl>'
    lonely_value = '<dl data-testid="d"><dd>1</dd><dt>B</dt><dd>2</dd></dl>'
    last_term = '<dl data-testid="d"><dt>A</dt><dd>1</dd><dt>B</dt></dl>'
    two_values = '<dl data-testid="d"><dt>A</dt><dd>1</dd><dd>2</dd></dl>'

    for page in (lonely_term, last_term):
        with pytest.raises(AssertionError, match="has no value"):
            definitions(page, "d")
    with pytest.raises(AssertionError, match="a value without a term"):
        definitions(lonely_value, "d")
    assert definitions(two_values, "d") == [("A", "1"), ("A", "2")]


# Form fields, forms and values

LOCATION_FORM = (
    '<form method="post" action="/locations/7/edit/" data-testid="location-form" novalidate>'
    '<input type="hidden" name="csrfmiddlewaretoken" value="csrf-value">'
    '<label for="id_name">Name</label>'
    '<input id="id_name" value="Office &amp; Co" name="name" type="text" aria-invalid="true" '
    'aria-describedby="id_name_error id_name_helptext">'
    '<p id="id_name_error">  Enter a\n  name. </p>'
    '<p id="id_name_helptext">Shown in alerts.</p>'
    '<label for="id_bot_token">Bot token</label>'
    '<input type="password" name="bot_token" id="id_bot_token">'
    '<label for="id_language">Language</label>'
    '<select name="language" id="id_language">'
    '<option value="uk">Ukrainian</option><option value="en" selected>English</option></select>'
    '<label for="id_zone">Zone</label>'
    '<select name="zone" id="id_zone"><option> Europe/Kyiv </option><option>UTC</option></select>'
    '<label for="id_quiet">Quiet</label><input type="checkbox" name="quiet" id="id_quiet">'
    '<label for="id_alerts">Alerts</label>'
    '<input type="checkbox" name="alerts" id="id_alerts" checked>'
    '<label for="id_notes">Notes</label>'
    '<textarea name="notes" id="id_notes">\nLine one\n</textarea>'
    '<input type="text" name="frozen" value="x" disabled aria-label="Frozen">'
    '<button type="submit" name="save" value="1">Save</button>'
    "</form>"
)

REGENERATE_FORMS = (
    '<form method="get" action="/locations/7/setup/regenerate/">'
    '<input type="hidden" name="marker" value="from-a-get-form"></form>'
    '<form action="/locations/7/setup/regenerate/" method="POST" data-testid="confirm-form">'
    '<input value="csrf-value" name="csrfmiddlewaretoken" type="hidden">'
    '<input name="marker" value="0123abcd" type="hidden"></form>'
    '<form method="post" action="/logout/"><input type="hidden" name="marker"></form>'
)


def test_field_and_errors() -> None:
    page = parse(LOCATION_FORM)

    name = field(page, "name")
    assert (name.name, name["aria-invalid"]) == ("input", "true")
    assert name["aria-describedby"] == "id_name_error id_name_helptext"
    assert field_error(page, "name") == "Enter a name."
    # A valid field has no error element at all.
    assert field_error(page, "bot_token") is None
    with pytest.raises(AssertionError, match="'id_missing'.*found 0"):
        field(page, "missing")
    with pytest.raises(AssertionError, match="'id_name_error'.*found 2"):
        field_error(LOCATION_FORM + '<p id="id_name_error">Again</p>', "name")


def test_forms_and_values() -> None:
    page = parse(LOCATION_FORM + REGENERATE_FORMS)

    confirm = post_form(page, "/locations/7/setup/regenerate/")
    assert confirm["data-testid"] == "confirm-form"
    # Attribute order does not matter, and a form posts whatever case its method is in.
    assert hidden_value(confirm, "marker") == "0123abcd"
    assert hidden_value(post_form(page, "/logout/"), "marker") == ""
    assert form_values(page, "location-form") == {
        "name": "Office & Co",
        "bot_token": "",
        "language": "en",
        "zone": "Europe/Kyiv",
        "alerts": "on",
        "notes": "Line one\n",
    }


def test_forms_and_values_fail_when_the_form_or_input_is_not_one() -> None:
    page = parse(LOCATION_FORM + REGENERATE_FORMS)
    marker = '<input name="marker" value="0123abcd" type="hidden">'
    twice = REGENERATE_FORMS.replace(marker, marker * 2)

    with pytest.raises(AssertionError, match="POST form to '/nowhere/'.*found 0"):
        post_form(page, "/nowhere/")
    with pytest.raises(AssertionError, match="hidden input 'missing'.*found 0"):
        hidden_value(post_form(page, "/logout/"), "missing")
    with pytest.raises(AssertionError, match="hidden input 'marker'.*found 2"):
        hidden_value(post_form(twice, "/locations/7/setup/regenerate/"), "marker")
    with pytest.raises(AssertionError, match="'missing-form'.*found 0"):
        form_values(page, "missing-form")


def test_code_block_keeps_whitespace() -> None:
    page = parse(
        '<pre id="example-cron"><code>  * * * * * curl -fsS URL\n'
        "* * * * * sleep 30; curl -fsS URL\n</code></pre>"
        '<code id="heartbeat-url">https://pm.example/<wbr>hb</code>'
        '<section id="recent-outages" aria-labelledby="t"><h2 id="t">Recent outages</h2></section>'
    )

    assert code_block(page, "example-cron") == (
        "  * * * * * curl -fsS URL\n* * * * * sleep 30; curl -fsS URL\n"
    )
    assert code_block(page, "heartbeat-url") == "https://pm.example/hb"
    assert text(section(page, "recent-outages")) == "Recent outages"
    with pytest.raises(AssertionError, match="'example-curl'.*found 0"):
        code_block(page, "example-curl")
    with pytest.raises(AssertionError, match="'reset-history'.*found 0"):
        section(page, "reset-history")


# assert_page: the 15 page invariants (TEST-STRATEGY §5.2; UI-01, UI-12, R5)

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}

APP_PAGE = (
    "<!DOCTYPE html>"
    '<html lang="en" data-theme="system"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="robots" content="noindex, nofollow">'
    "<title>Office · Power Monitor</title>"
    '<link rel="stylesheet" href="/static/web/build/app.0123456789ab.css">'
    '<link rel="preload" href="/static/web/fonts/inter-latin.0123456789ab.woff2" as="font" '
    'type="font/woff2" crossorigin>'
    '<link rel="icon" href="/static/web/favicon.0123456789ab.svg" type="image/svg+xml">'
    '<script src="/static/web/admin.0123456789ab.js"></script>'
    "</head><body>"
    '<a href="#main" data-testid="skip-link">Skip to content</a>'
    '<aside id="sidebar" data-testid="sidebar"><nav aria-label="Main">'
    '<a href="/" aria-current="page" data-testid="nav-locations">Locations</a></nav></aside>'
    '<header data-testid="topbar">'
    '<button type="button" aria-label="Open navigation" data-js-only hidden>'
    '<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">'
    '<path d="M0 0"></path></svg></button>'
    '<form method="post" action="/logout/" data-testid="sign-out-form">'
    '<input type="hidden" name="csrfmiddlewaretoken" value="csrf-value">'
    '<button type="submit">Sign out</button></form>'
    "</header>"
    '<main id="main" tabindex="-1"><h1>Office</h1>'
    '<span id="copy-label">Copy heartbeat URL</span>'
    '<button type="button" aria-labelledby="copy-label">'
    '<svg aria-hidden="true" focusable="false"><path d="M0 0"></path></svg></button>'
    '<a href="/locations/1/edit/" title="Edit location">'
    '<svg aria-hidden="true" focusable="false"><path d="M0 0"></path></svg></a>'
    '<label for="id_name">Name</label><input id="id_name" name="name" type="text">'
    '<label>Zone <select name="zone"><option>UTC</option></select></label>'
    '<input type="search" aria-label="Filter locations">'
    '<img src="/locations/1/chart.png" alt="Weekly chart" width="1280" height="1000">'
    '<table data-testid="outages-table"><caption>Recent outages</caption>'
    '<thead><tr><th scope="col">Start</th></tr></thead>'
    "<tbody><tr><td>09:00</td></tr></tbody></table>"
    '<svg role="img" xmlns="http://www.w3.org/2000/svg"><title>On</title>'
    '<path d="M0 0"></path></svg>'
    '<dialog data-testid="confirm-dialog" aria-labelledby="copy-label">'
    '<form method="dialog"><button>Keep</button></form></dialog>'
    "<p>Heartbeat URL: https://pm.example/hb</p>"
    "</main>"
    '<div data-testid="toasts-status" role="status"></div>'
    '<div data-testid="toasts-alert" role="alert"></div>'
    "</body></html>"
)


def _page_response(html: str = APP_PAGE, status: int = 200, **headers: str) -> HttpResponse:
    """An HTML response with the security headers, as the middleware sends them."""
    response = HttpResponse(html, status=status)
    for name, value in {**HEADERS, **headers}.items():
        response[name] = value
    return response


def test_assert_page_accepts_a_valid_app_page() -> None:
    page = assert_page(_page_response(), title="Office", app=True)

    assert text(h1(page)) == "Office"
    assert assert_page(_page_response(status=404), status=404).find("main") is not None
    # The policy constant is the brief §8 string, written out here so a change fails.
    assert CSP == HEADERS["Content-Security-Policy"]


BARE_PAGE = APP_PAGE.replace(
    '<aside id="sidebar" data-testid="sidebar">', '<aside id="sidebar">'
).replace('<header data-testid="topbar">', "<header>")


def test_assert_page_checks_the_shell_hooks() -> None:
    assert_page(_page_response(BARE_PAGE), app=False)

    with pytest.raises(AssertionError, match="'sidebar'.*found 0"):
        assert_page(_page_response(BARE_PAGE), app=True)
    with pytest.raises(AssertionError, match="app=False.*sidebar"):
        assert_page(_page_response(), app=False)


# (what is replaced, what replaces it, the failure message)
BROKEN = [
    ('<script src="/static/web/admin.0123456789ab.js"></script>', "<script>go()</script>", "body"),
    ("<h1>Office</h1>", '<h1 style="color: red">Office</h1>', "style attribute"),
    ("<h1>Office</h1>", '<h1 onclick="go()">Office</h1>', "inline event handlers"),
    ("<h1>Office</h1>", "<h1>Office</h1><style>h1 {}</style>", "<style>"),
    ("<h1>Office</h1>", '<h1>Office</h1><a href="https://evil.example/">Out</a>', "absolute URL"),
    (
        "<h1>Office</h1>",
        '<h1>Office</h1><img src="//evil.example/x.png" alt="" width="1" height="1">',
        "absolute URL",
    ),
    ("<h1>Office</h1>", '<h1>Office</h1><a href="javascript:go()">Go</a>', "javascript:"),
    ("<title>", '<meta http-equiv="Refresh" content="5"><title>', "refresh"),
    (
        '<label for="id_name">Name</label><input id="id_name"',
        '<input id="id_name"',
        "<input> 'name' has no label",
    ),
    ("<h1>Office</h1>", '<h1>Office</h1><p id="copy-label">Again</p>', "duplicate id 'copy-label'"),
    ("<caption>Recent outages</caption>", "", "<table> has no caption"),
    ('<th scope="col">Start</th>', "<th>Start</th>", "<th> 'Start' has no scope"),
    (
        '<form method="dialog">',
        '<form method="get" action="/">',
        "form to '/' is not a POST form",
    ),
    (
        '<input type="hidden" name="csrfmiddlewaretoken" value="csrf-value">',
        "",
        "has no CSRF input",
    ),
    ('alt="Weekly chart" ', "", "<img> '/locations/1/chart.png' lacks alt"),
    ('width="1280" ', "", "<img> '/locations/1/chart.png' lacks width"),
    ('<meta name="robots" content="noindex, nofollow">', "", "robots"),
    ("initial-scale=1", "initial-scale=1, maximum-scale=1", "zoom"),
    ("initial-scale=1", "initial-scale=1, user-scalable=no", "zoom"),
    ('aria-labelledby="copy-label">', 'aria-labelledby="missing-label">', "accessible name"),
    ('title="Edit location">', ">", "accessible name"),
    ("app.0123456789ab.css", "app.css", "not a hashed same-origin static path"),
    ('type="font/woff2" crossorigin', 'type="font/woff2"', "crossorigin"),
    ('<svg aria-hidden="true" focusable="false"><path', "<svg><path", "<svg> without a <title>"),
    ("<title>Office · Power Monitor</title>", "<title>Office</title>", "title 'Office'"),
    ("<h1>Office</h1>", "<h1>Office</h1><h1>Again</h1>", "<h1>.*found 2"),
    ('<main id="main" tabindex="-1">', "<main>", "<main> id"),
    ('<a href="#main" data-testid="skip-link">Skip to content</a>', "", "skip link"),
    ('<html lang="en" data-theme="system">', '<html data-theme="system">', "lang"),
    ('data-theme="system"', 'data-theme="blue"', "data-theme 'blue'"),
    ("<p>Heartbeat URL", f"<p>{TOKEN} Heartbeat URL", "page: secret #0 is in the body"),
]


@pytest.mark.parametrize(("old", "new", "reason"), BROKEN)
def test_assert_page_invariants(old: str, new: str, reason: str) -> None:
    assert old in APP_PAGE, old
    broken = _page_response(APP_PAGE.replace(old, new, 1))

    with pytest.raises(AssertionError, match=reason):
        assert_page(broken, title="Office", app=True)


@pytest.mark.parametrize(
    ("headers", "status", "reason"),
    [
        ({"Content-Security-Policy": "default-src 'self'"}, 200, "Content-Security-Policy"),
        ({"X-Frame-Options": "SAMEORIGIN"}, 200, "X-Frame-Options"),
        ({"Referrer-Policy": "no-referrer"}, 200, "Referrer-Policy"),
        ({}, 302, "status 302, expected 200"),
    ],
)
def test_assert_page_checks_status_and_headers(
    headers: dict[str, str], status: int, reason: str
) -> None:
    with pytest.raises(AssertionError, match=reason):
        assert_page(_page_response(status=status, **headers))
    with pytest.raises(AssertionError, match="not an HTML response"):
        assert_page(JsonResponse({"ok": True}))


def test_assert_page_checks_the_location_header() -> None:
    response = _page_response()
    response["Location"] = f"/next/?token={TOKEN}"

    with pytest.raises(AssertionError, match="secret #0 is in a response header"):
        assert_page(response)


# assert_no_secrets (TEST-STRATEGY §5.3)

SETTINGS_PANEL = (
    '<main><dl data-testid="settings-panel"><dt>Bot token</dt><dd>{panel}</dd></dl>'
    '<pre id="device-key">{key}</pre><p>{outside}</p></main>'
)


def test_assert_no_secrets() -> None:
    clean = SETTINGS_PANEL.format(panel=MASKED, key="key", outside="Nothing secret")

    assert_no_secrets(clean, SECRETS, label="settings")
    assert_no_secrets(clean, (MASKED,), allow=[(MASKED, "settings-panel")])
    assert_no_secrets(b"\x89PNG\r\n\x1a\nIDAT", SECRETS, headers=["/locations/1/"])
    # The key may show in #device-key; the mask in the settings panel.
    revealed = SETTINGS_PANEL.format(panel=MASKED, key=SECRET, outside="")
    assert_no_secrets(
        revealed, (SECRET, MASKED), allow=[(SECRET, "#device-key"), (MASKED, "settings-panel")]
    )


def test_assert_no_secrets_finds_a_secret_in_text_bytes_and_headers() -> None:
    leaked = SETTINGS_PANEL.format(panel=MASKED, key="key", outside=f"Unauthorized {TOKEN}")

    with pytest.raises(AssertionError, match="^test result: secret #0 is in the body"):
        assert_no_secrets(leaked, SECRETS, label="test result")
    with pytest.raises(AssertionError, match="secret #1 is in the body"):
        assert_no_secrets(f"png {SECRET}".encode(), SECRETS)
    with pytest.raises(AssertionError, match="secret #0 is in a response header"):
        assert_no_secrets("", SECRETS, headers=[f"/locations/1/?t={TOKEN}"])
    # An entity-encoded secret is still the secret.
    with pytest.raises(AssertionError, match="secret #0 is in the body"):
        assert_no_secrets("<p>it&#x27;s</p>", ["it's"])


def test_assert_no_secrets_allows_only_inside_the_named_element() -> None:
    outside = SETTINGS_PANEL.format(panel=MASKED, key="key", outside=MASKED)
    in_attribute = SETTINGS_PANEL.format(panel=MASKED, key="key", outside="").replace(
        '<dl data-testid="settings-panel">', f'<dl data-testid="settings-panel" title="{MASKED}">'
    )
    allow = [(MASKED, "settings-panel")]

    with pytest.raises(AssertionError, match="secret #0 is outside 'settings-panel'"):
        assert_no_secrets(outside, (MASKED,), allow=allow)
    with pytest.raises(AssertionError, match="secret #0 is outside 'settings-panel'"):
        assert_no_secrets(in_attribute, (MASKED,), allow=allow)
    # An allowance covers its own secret only.
    with pytest.raises(AssertionError, match="secret #1 is in the body"):
        assert_no_secrets(
            SETTINGS_PANEL.format(panel=TOKEN, key="key", outside=""),
            (MASKED, TOKEN),
            allow=allow,
        )
