"""The one HTML parsing module of tests/web, imported as ``from pages import ...``.

This is a helper module, not a conftest: test directories have no ``__init__.py``, and a
second conftest would shadow the root one (03-PATTERNS). Web tests read pages only through
the 06-UI-SPEC test hooks and these helpers, never through raw markup, class names,
attribute order or DOM depth (TEST-STRATEGY §1 rule 3, §5.1).

- Parser: beautifulsoup4 with soupsieve for selectors, on the stdlib ``html.parser``
  builder (no lxml, no html5lib). Both are dev-only dependencies, approved at the INV-26
  checkpoint of 06-01; the runtime image never has them.
- Lookups go by element id, ``data-testid``, role, ARIA attributes and tag names. This
  module offers no lookup by class name, and no selector in it uses one (TEST-STRATEGY
  §5.4).
- A ``page`` argument is a parsed page or element, a test-client response (it must be
  HTML) or the markup as ``str`` or ``bytes``.
- What it offers: ``parse``, ``by_testid``, ``all_by_testid``, ``main``, ``h1``,
  ``title``, ``text``, the flashes as ``Message`` through ``messages`` and
  ``message_texts``, and ``assert_no_injected_script``.
- ``text()`` is the text a screen reader gets: whitespace collapsed, visually hidden text
  kept, ``aria-hidden="true"`` subtrees (icons, masked glyphs) skipped.
- ``messages()`` reads the 06-UI-SPEC toasts, and on pages that still extend the old
  base.html the legacy flash callouts, so a flash assertion keeps its meaning whichever
  layout its page uses (UI-09, TEST-STRATEGY §3.3). 06-21 removes the legacy branch.

Assertions here raise ``AssertionError`` with a message that names what was looked for; the
module is not rewritten by pytest, so every message is written out.
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from bs4.element import PreformattedString, Script, Stylesheet, TemplateString
from django.http import HttpResponse
from django.http.response import HttpResponseBase

type Page = Tag | HttpResponseBase | str | bytes

# Strings that are not page text: comments, doctype and declarations, and the bodies of
# script, style and template elements.
_NOT_TEXT = (PreformattedString, Script, Stylesheet, TemplateString)

# Django message level tags (TEST-STRATEGY §4.2 data-level).
LEVELS = frozenset({"success", "info", "warning", "error"})
TOAST_REGIONS = ("toasts-status", "toasts-alert")
ROLES = frozenset({"status", "alert"})

# A manifest-hashed same-origin static path (CompressedManifestStaticFilesStorage).
STATIC_ASSET = re.compile(r"^/static/web/.+\.[0-9a-f]{12}\.[a-z0-9]+$")
INJECTED_SCRIPT = "<script>alert(1)</script>"


def parse(page: HttpResponseBase | str | bytes) -> BeautifulSoup:
    """The parsed page. A response must be an HTML response (``Content-Type: text/html``)."""
    if isinstance(page, HttpResponseBase):
        content_type = str(page.get("Content-Type", ""))
        assert content_type.startswith("text/html"), f"not an HTML response: {content_type!r}"
        assert isinstance(page, HttpResponse), "a streaming response has no page to parse"
        page = page.content
    if isinstance(page, bytes):
        page = page.decode()
    assert isinstance(page, str), f"cannot parse a {type(page).__name__}"
    return BeautifulSoup(page, "html.parser")


def _soup(page: Page) -> Tag:
    """``page`` as a parsed element: a parsed page or element as it is, anything else parsed."""
    return page if isinstance(page, Tag) else parse(page)


def _tags(found: object) -> list[Tag]:
    """The elements of a bs4 result (strings left out)."""
    assert isinstance(found, list), f"not a result list: {type(found).__name__}"
    return [element for element in found if isinstance(element, Tag)]


def _only(found: list[Tag], what: str) -> Tag:
    """The one element of ``found``; fails unless there is exactly one."""
    assert len(found) == 1, f"{what}: expected exactly one element, found {len(found)}"
    return found[0]


def all_by_testid(page: Page, name: str) -> list[Tag]:
    """Every element whose ``data-testid`` is ``name``, in document order (maybe none)."""
    return _tags(_soup(page).find_all(attrs={"data-testid": name}))


def by_testid(page: Page, name: str) -> Tag:
    """The one element whose ``data-testid`` is ``name``; fails on none or several."""
    return _only(all_by_testid(page, name), f"data-testid {name!r}")


def main(page: Page) -> Tag:
    """The page's one ``<main>`` element."""
    return _only(_tags(_soup(page).find_all("main")), "<main>")


def h1(page: Page) -> Tag:
    """The page's one ``<h1>`` element."""
    return _only(_tags(_soup(page).find_all("h1")), "<h1>")


def title(page: Page) -> str:
    """The text of the page's one ``<title>``, whitespace collapsed."""
    return text(_only(_tags(_soup(page).find_all("title")), "<title>"))


def _aria_hidden(element: Tag) -> bool:
    value = element.get("aria-hidden")
    return isinstance(value, str) and value.strip().lower() == "true"


def text(element: Tag) -> str:
    """The element's text as a screen reader gets it.

    Whitespace runs collapse to one space and the ends are trimmed. Visually hidden text
    (an sr-only span) is kept; every ``aria-hidden="true"`` subtree inside ``element``, or
    ``element`` itself when it is aria-hidden, is skipped. Comments and script, style and
    template bodies are never text.
    """
    parts: list[str] = []
    for string in element.find_all(string=True):
        if isinstance(string, _NOT_TEXT):
            continue
        parent = string.parent
        hidden = False
        while parent is not None:
            if _aria_hidden(parent):
                hidden = True
                break
            if parent is element:
                break
            parent = parent.parent
        if not hidden:
            parts.append(str(string))
    return " ".join("".join(parts).split())


# Flashes (UI-09)


@dataclass(frozen=True)
class Message:
    """One flash as the page shows it.

    ``level`` is the toast's ``data-level`` (a Django level tag), or None for a legacy
    callout, which carries no level. ``role`` is the live-region role it is announced
    through, ``status`` or ``alert``. ``text`` is the flash text without the toast's
    visually hidden "Warning: " / "Error: " prefix.
    """

    level: str | None
    role: str
    text: str


def _toast(toast: Tag) -> Message:
    region = toast.find_parent(attrs={"data-testid": list(TOAST_REGIONS)})
    assert region is not None, "a toast outside the two toast regions"
    role = region.get("role")
    assert role in ROLES, f"toast region role {role!r} is not status or alert"
    level = toast.get("data-level")
    assert level in LEVELS, f"toast data-level {level!r} is not a Django level tag"
    return Message(str(level), str(role), text(by_testid(toast, "toast-text")))


def messages(page: Page) -> list[Message]:
    """Every flash on the page, in document order.

    With the 06-UI-SPEC toast regions on the page (both, exactly once each) these are the
    toasts, ``(data-level, role of its region, toast-text)``. Without them the page still
    extends the old base.html, and these are its flash callouts: each ``p`` with
    ``role="status"`` or ``role="alert"`` inside ``<main>``, ``(None, role, text)``.
    """
    soup = _soup(page)
    regions = {name: all_by_testid(soup, name) for name in TOAST_REGIONS}
    if any(regions.values()):
        for name, found in regions.items():
            _only(found, f"toast region {name!r}")
        return [_toast(toast) for toast in all_by_testid(soup, "toast")]
    callouts = soup.select('main p[role="status"], main p[role="alert"]')
    return [Message(None, str(callout["role"]), text(callout)) for callout in callouts]


def message_texts(page: Page) -> list[str]:
    """The text of every flash on the page, in document order."""
    return [message.text for message in messages(page)]


# Scripts (UI-13, R5; TEST-STRATEGY §3.5)


def assert_no_injected_script(html: str, label: str = "") -> None:
    """The page carries no injected or inline script and no inline event handler.

    Fails when the markup contains the payload ``<script>alert(1)</script>``, when a
    ``<script>`` element has a body, when a ``<script>`` has no ``src`` or one that is not
    a manifest-hashed same-origin static path, when a script tag in the markup is not a
    parsed script element, or when any element has an ``on*`` attribute.
    """
    where = f"{label}: " if label else ""
    assert INJECTED_SCRIPT not in html.lower(), f"{where}the injected script payload is present"
    soup = parse(html)
    scripts = _tags(soup.find_all("script"))
    opened = len(re.findall(r"<script\b", html, re.IGNORECASE))
    assert opened == len(scripts), (
        f"{where}{opened} script tags in the markup but {len(scripts)} script elements"
    )
    for script in scripts:
        assert script.get_text() == "", f"{where}a script element has a body"
        src = script.get("src")
        assert isinstance(src, str) and STATIC_ASSET.fullmatch(src), (
            f"{where}script src {src!r} is not a hashed same-origin static path"
        )
    for element in _tags(soup.find_all(True)):
        handlers = sorted(name for name in element.attrs if name.lower().startswith("on"))
        assert not handlers, f"{where}<{element.name}> has inline event handlers {handlers}"


# RED stubs (TDD): the rest of the TEST-STRATEGY §5.1 API, not implemented yet.

CSP = ""


def breadcrumbs(page: Page, testid: str = "breadcrumbs") -> list[tuple[str, str | None]]:
    return []


def table(page: Page, testid: str) -> tuple[list[str], list[list[str]]]:
    return [], []


def definitions(page: Page, testid: str) -> list[tuple[str, str]]:
    return []


def field(page: Page, name: str) -> Tag:
    return _soup(page)


def field_error(page: Page, name: str) -> str | None:
    return None


def post_form(page: Page, action: str) -> Tag:
    return _soup(page)


def hidden_value(form: Page, name: str) -> str:
    return ""


def form_values(page: Page, testid: str) -> dict[str, str]:
    return {}


def code_block(page: Page, element_id: str) -> str:
    return ""


def section(page: Page, element_id: str) -> Tag:
    return _soup(page)


def assert_page(
    response: HttpResponseBase,
    *,
    status: int = 200,
    title: str | None = None,
    app: bool | None = None,
) -> BeautifulSoup:
    return parse(response)


def assert_no_secrets(
    body: str | bytes,
    secrets: object,
    *,
    label: str = "",
    allow: object = (),
    headers: object = (),
) -> None:
    return None
