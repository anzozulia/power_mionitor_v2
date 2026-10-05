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
  ``title``, ``text``; the flashes as ``Message`` through ``messages`` and
  ``message_texts``; ``breadcrumbs``, ``table``, ``definitions``; ``field``,
  ``field_error``, ``post_form``, ``hidden_value``, ``form_values``; ``code_block``,
  ``section``; and the checks ``assert_page`` (the 15 page invariants of TEST-STRATEGY
  §5.2), ``assert_no_injected_script`` and ``assert_no_secrets`` (§5.3).
- ``text()`` is the text a screen reader gets: whitespace collapsed, visually hidden text
  kept, ``aria-hidden="true"`` subtrees (icons, masked glyphs) skipped.
- ``messages()`` reads the 06-UI-SPEC toasts and nothing else: every flash is a toast with
  its level, and a page without the toast regions (the error layout, R11) has no flash
  (UI-09, TEST-STRATEGY §3.3).

Assertions here raise ``AssertionError`` with a message that names what was looked for; the
module is not rewritten by pytest, so every message is written out.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag
from bs4.element import PreformattedString, Script, Stylesheet, TemplateString
from django.http import HttpResponse
from django.http.response import HttpResponseBase
from secret_fixtures import SECRETS

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


def _title_text(page: Page) -> str:
    """The document title; an inline SVG's ``<title>`` (an icon's name) is not one."""
    titles = [
        found for found in _tags(_soup(page).find_all("title")) if not found.find_parent("svg")
    ]
    return text(_only(titles, "<title>"))


def title(page: Page) -> str:
    """The text of the page's one ``<title>``, whitespace collapsed."""
    return _title_text(page)


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
    """One flash as the page shows it: a toast.

    ``level`` is the toast's ``data-level``, a Django level tag (``LEVELS``). ``role`` is
    the live-region role it is announced through, ``status`` or ``alert``. ``text`` is the
    flash text without the toast's visually hidden "Warning: " / "Error: " prefix.
    """

    level: str
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
    """Every flash on the page, in document order: its 06-UI-SPEC toasts.

    Each is ``(data-level, role of its region, toast-text)``. A page with the toast regions
    has both, exactly once each. A page without them (the error layout, R11) shows no
    flash and gives []; a toast there fails. No other markup is ever read as a flash, so
    every flash has its level.
    """
    soup = _soup(page)
    regions = {name: all_by_testid(soup, name) for name in TOAST_REGIONS}
    if not any(regions.values()):
        assert not all_by_testid(soup, "toast"), "a toast outside the two toast regions"
        return []
    for name, found in regions.items():
        _only(found, f"toast region {name!r}")
    return [_toast(toast) for toast in all_by_testid(soup, "toast")]


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


# Page regions (UI-01): breadcrumbs, tables, description lists


def breadcrumbs(page: Page, testid: str = "breadcrumbs") -> list[tuple[str, str | None]]:
    """The trail ``nav > ol > li`` of the ``testid`` hook, as (text, href) per item.

    A linked item gives its link's href. The current item has no link and gives None; it
    must carry ``aria-current="page"`` (on the item or on the span inside it).
    """
    trail = _only(_tags(by_testid(page, testid).find_all("ol")), f"{testid} <ol>")
    items: list[tuple[str, str | None]] = []
    for item in _tags(trail.find_all("li", recursive=False)):
        link = item.find("a", href=True)
        if isinstance(link, Tag):
            items.append((text(item), str(link["href"])))
            continue
        current = item.get("aria-current") == "page" or bool(
            item.find(attrs={"aria-current": "page"})
        )
        assert current, f"{testid}: item {text(item)!r} has no link and is not aria-current"
        items.append((text(item), None))
    return items


def _cell_texts(cells: list[Tag]) -> list[str]:
    return [text(cell) for cell in cells]


def _row_cells(row: Tag) -> list[Tag]:
    return _tags(row.find_all(["th", "td"], recursive=False))


def table(page: Page, testid: str) -> tuple[list[str], list[list[str]]]:
    """The header texts and the cell texts of each displayed body row of a table hook.

    Headers come from the first row of the ``<thead>``, or, in a table without one, from a
    leading row of ``th`` cells only. Body rows are the rows of each ``<tbody>`` and the
    table's own rows (``html.parser`` adds no implicit tbody). A ``tr`` carrying the
    ``hidden`` attribute is not displayed and is never a row (06-18's no-match row);
    ``by_testid()`` still finds it. A row header (``th[scope=row]``) is a cell.
    """
    element = by_testid(page, testid)
    assert element.name == "table", f"data-testid {testid!r} is a <{element.name}>, not a <table>"
    head = element.find("thead", recursive=False)
    headers: list[str] = []
    if isinstance(head, Tag):
        head_rows = _tags(head.find_all("tr", recursive=False))
        headers = _cell_texts(_row_cells(head_rows[0])) if head_rows else []
    rows: list[list[str]] = []
    for part in _tags(list(element.children)):
        if part.name == "tbody":
            group = _tags(part.find_all("tr", recursive=False))
        elif part.name == "tr":
            group = [part]
        else:
            continue
        for row in group:
            if row.has_attr("hidden"):
                continue
            cells = _row_cells(row)
            leading = head is None and not headers and not rows and bool(cells)
            if leading and all(cell.name == "th" for cell in cells):
                headers = _cell_texts(cells)
                continue
            rows.append(_cell_texts(cells))
    return headers, rows


def _dl_items(dl: Tag) -> list[Tag]:
    """The ``dt`` and ``dd`` children of a ``<dl>``, also those grouped in a ``<div>``."""
    items: list[Tag] = []
    for child in _tags(list(dl.children)):
        if child.name in ("dt", "dd"):
            items.append(child)
        elif child.name == "div":
            items += [item for item in _tags(list(child.children)) if item.name in ("dt", "dd")]
    return items


def definitions(page: Page, testid: str) -> list[tuple[str, str]]:
    """The (term, value) pairs of a ``<dl>`` hook, in order; a term with two values gives two."""
    element = by_testid(page, testid)
    assert element.name == "dl", f"data-testid {testid!r} is a <{element.name}>, not a <dl>"
    pairs: list[tuple[str, str]] = []
    term: str | None = None
    waiting = False
    for item in _dl_items(element):
        if item.name == "dt":
            assert not waiting, f"{testid}: term {term!r} has no value"
            term, waiting = text(item), True
        else:
            assert term is not None, f"{testid}: a value without a term"
            pairs.append((term, text(item)))
            waiting = False
    assert not waiting, f"{testid}: term {term!r} has no value"
    return pairs


# Fields and forms (Django ids: id_<name>, id_<name>_error, _helptext, _note)


def _by_id(page: Page, element_id: str) -> Tag:
    return _only(_tags(_soup(page).find_all(id=element_id)), f"id {element_id!r}")


def _lower(element: Tag, attribute: str) -> str:
    value = element.get(attribute)
    return value.strip().lower() if isinstance(value, str) else ""


def field(page: Page, name: str) -> Tag:
    """The form control ``#id_<name>`` with its ``aria-*`` attributes."""
    return _by_id(page, f"id_{name}")


def field_error(page: Page, name: str) -> str | None:
    """The text of ``#id_<name>_error``, or None when the field shows no error."""
    element_id = f"id_{name}_error"
    found = _tags(_soup(page).find_all(id=element_id))
    assert len(found) <= 1, f"id {element_id!r}: expected at most one element, found {len(found)}"
    return text(found[0]) if found else None


def post_form(page: Page, action: str) -> Tag:
    """The one ``<form>`` that POSTs to ``action`` (the method in any case)."""
    forms = [
        form
        for form in _tags(_soup(page).find_all("form"))
        if _lower(form, "method") == "post" and form.get("action") == action
    ]
    return _only(forms, f"POST form to {action!r}")


def hidden_value(form: Page, name: str) -> str:
    """The value of the form's one hidden input called ``name`` ("" without a value)."""
    inputs = [
        found
        for found in _tags(_soup(form).find_all("input"))
        if _lower(found, "type") == "hidden" and found.get("name") == name
    ]
    value = _only(inputs, f"hidden input {name!r}").get("value")
    return value if isinstance(value, str) else ""


_NOT_POSTED = frozenset({"submit", "button", "reset", "image", "file"})


def _select_value(select: Tag) -> str:
    """The selected option's value, or the first option's; an option without one gives its text."""
    options = _tags(select.find_all("option"))
    chosen = [option for option in options if option.has_attr("selected")] or options[:1]
    if not chosen:
        return ""
    value = chosen[0].get("value")
    return value if isinstance(value, str) else text(chosen[0])


def form_values(page: Page, testid: str) -> dict[str, str]:
    """What the form hook posts as loaded, by control name.

    An input gives its value ("" without one); a checked checkbox or radio gives its value
    or "on", an unchecked one nothing. A select gives its selected option's value, or its
    first option's (an option without a value gives its text). A textarea gives its text
    without the one newline after the start tag. Disabled controls, buttons, file inputs
    and the CSRF token are left out.
    """
    values: dict[str, str] = {}
    for control in _tags(by_testid(page, testid).find_all(["input", "select", "textarea"])):
        name = control.get("name")
        if not isinstance(name, str) or name in ("", "csrfmiddlewaretoken"):
            continue
        if control.has_attr("disabled"):
            continue
        if control.name == "select":
            values[name] = _select_value(control)
            continue
        if control.name == "textarea":
            raw = control.get_text()
            values[name] = raw[2:] if raw.startswith("\r\n") else raw.removeprefix("\n")
            continue
        kind = _lower(control, "type") or "text"
        value = control.get("value")
        if kind in _NOT_POSTED:
            continue
        if kind in ("checkbox", "radio"):
            if control.has_attr("checked"):
                values[name] = value if isinstance(value, str) else "on"
            continue
        values[name] = value if isinstance(value, str) else ""
    return values


def code_block(page: Page, element_id: str) -> str:
    """The exact text of the element with this id: nothing stripped, nothing collapsed."""
    return _by_id(page, element_id).get_text()


def section(page: Page, element_id: str) -> Tag:
    """The element with this id (a card ``<section>`` or a danger-zone row)."""
    return _by_id(page, element_id)


# Secrets (INV-23 #2, SEC-04; TEST-STRATEGY §5.3)


def _contains(haystack: str, secret: str) -> bool:
    """The secret as written, or behind HTML entities or URL escapes."""
    return any(secret in form for form in (haystack, unescape(haystack), unquote(haystack)))


def _without_allowed_text(html: str, targets: list[str]) -> str:
    """The page with the text of each allowed element removed; its attributes stay."""
    soup = BeautifulSoup(html, "html.parser")
    for target in targets:
        if target.startswith("#"):
            found = _tags(soup.find_all(id=target[1:]))
        else:
            found = all_by_testid(soup, target)
        for element in found:
            for string in list(element.find_all(string=True)):
                if not isinstance(string, _NOT_TEXT):
                    string.extract()
    return str(soup)


def assert_no_secrets(
    body: str | bytes,
    secrets: Iterable[str],
    *,
    label: str = "",
    allow: Iterable[tuple[str, str]] = (),
    headers: Iterable[str] = (),
) -> None:
    """No secret appears in the body or in any of ``headers`` (pass every ``Location``).

    ``allow`` holds (secret, target) pairs: the text of the element named by ``target``
    (a ``data-testid``, or ``#`` and an element id) may contain that secret, and nothing
    else may, not even that element's attributes. Allowances apply to an HTML ``str``
    body; a ``bytes`` body (the chart PNG) never contains a secret. A secret behind HTML
    entities or URL escapes counts. Failure messages give the secret's position in
    ``secrets``, never its value.
    """
    where = f"{label}: " if label else ""
    allowed: dict[str, list[str]] = {}
    for secret, target in allow:
        allowed.setdefault(secret, []).append(target)
    header_values = [str(value) for value in headers]
    for number, secret in enumerate(secrets):
        assert secret, f"{where}secret #{number} is empty"
        for value in header_values:
            assert not _contains(value, secret), f"{where}secret #{number} is in a response header"
        if isinstance(body, bytes):
            assert secret.encode() not in body, f"{where}secret #{number} is in the body"
            continue
        targets = allowed.get(secret)
        if targets is None:
            assert not _contains(body, secret), f"{where}secret #{number} is in the body"
            continue
        rest = _without_allowed_text(body, targets)
        assert not _contains(rest, secret), f"{where}secret #{number} is outside " + ", ".join(
            repr(t) for t in targets
        )


# Page invariants (TEST-STRATEGY §5.2; UI-01, UI-12, UI-13, R5, R15)

# The brief §8 policy, written out so a changed middleware constant fails a page test.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
THEMES = frozenset({"light", "dark", "system"})
TITLE_SUFFIX = " · Power Monitor"
ROBOTS = "noindex, nofollow"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_UNLABELLED_INPUTS = frozenset({"hidden", "submit", "button", "reset", "image"})


def _values(raw: object) -> list[str]:
    """An attribute's value as strings: the value, or the tokens of a multi-valued one."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(token) for token in raw]
    return []


def _texts_of(soup: Tag, ids: object) -> str:
    """The joined text of the elements an ``aria-labelledby`` value points at."""
    names: list[str] = []
    for element_id in ids.split() if isinstance(ids, str) else []:
        found = _tags(soup.find_all(id=element_id))
        if found:
            names.append(text(found[0]))
    return " ".join(name for name in names if name)


def _accessible_name(control: Tag, soup: Tag) -> str:
    """A button's or link's name: aria-labelledby, aria-label, its text or img alt, title."""
    alts = [str(img.get("alt", "")) for img in _tags(control.find_all("img"))]
    label, title_value = control.get("aria-label"), control.get("title")
    for name in (
        _texts_of(soup, control.get("aria-labelledby")),
        label if isinstance(label, str) else "",
        " ".join([text(control), *alts]),
        title_value if isinstance(title_value, str) else "",
    ):
        if name.strip():
            return name.strip()
    return ""


def _labelled(control: Tag, soup: Tag) -> bool:
    """A control has a label: aria-label, aria-labelledby, label[for] or a label around it."""
    label = control.get("aria-label")
    if isinstance(label, str) and label.strip():
        return True
    if _texts_of(soup, control.get("aria-labelledby")):
        return True
    element_id = control.get("id")
    if isinstance(element_id, str) and element_id:
        labels = _tags(soup.find_all("label", attrs={"for": element_id}))
        if any(text(found) for found in labels):
            return True
    return control.find_parent("label") is not None


def assert_page(
    response: HttpResponseBase,
    *,
    status: int = 200,
    title: str | None = None,
    app: bool | None = None,
) -> BeautifulSoup:
    """The full page keeps the 15 page invariants of TEST-STRATEGY §5.2; returns it parsed.

    ``title`` is the page title before " · Power Monitor" (None: any title of that form).
    ``app=True`` requires the ``sidebar`` and ``topbar`` hooks, ``app=False`` forbids
    both, None does not check them. Masks and keys that §9 allows on a surface are the
    page test's own ``assert_no_secrets`` call; here only the fixture tokens are scanned.
    """
    # 1. The expected status code.
    assert response.status_code == status, f"status {response.status_code}, expected {status}"
    soup = parse(response)
    assert isinstance(response, HttpResponse)
    body = response.content.decode()
    # 2. <title> is "{page title} · Power Monitor".
    heading = _title_text(soup)
    if title is None:
        assert heading.endswith(TITLE_SUFFIX) and heading != TITLE_SUFFIX, (
            f"title {heading!r} is not '<page> · Power Monitor'"
        )
    else:
        expected = f"{title}{TITLE_SUFFIX}"
        assert heading == expected, f"title {heading!r}, expected {expected!r}"
    # 3. Exactly one <h1>.
    h1(soup)
    # 4. Landmarks: <main id="main">, a skip link to it, the app shell hooks.
    main_id = main(soup).get("id")
    assert main_id == "main", f"<main> id is {main_id!r}, expected 'main'"
    links = _tags(soup.find_all("a"))
    assert any(link.get("href") == "#main" for link in links), "no skip link to #main"
    if app is True:
        by_testid(soup, "sidebar")
        by_testid(soup, "topbar")
    elif app is False:
        for hook in ("sidebar", "topbar"):
            assert not all_by_testid(soup, hook), f"app=False: the page has the {hook!r} hook"
    # 5. <html lang="en" data-theme="light|dark|system">.
    root = _only(_tags(soup.find_all("html")), "<html>")
    lang, theme = root.get("lang"), root.get("data-theme")
    assert lang == "en", f"<html lang> is {lang!r}, expected 'en'"
    assert theme in THEMES, f"<html> data-theme {theme!r} is not light, dark or system"
    # 6. robots noindex, nofollow (R15); a viewport that allows zoom.
    metas = _tags(soup.find_all("meta"))
    robots = [str(meta.get("content")) for meta in metas if _lower(meta, "name") == "robots"]
    assert robots == [ROBOTS], f"robots meta {robots!r}, expected [{ROBOTS!r}]"
    viewports = [meta for meta in metas if _lower(meta, "name") == "viewport"]
    viewport = str(_only(viewports, "viewport meta").get("content", ""))
    zoom = "".join(viewport.split()).lower()
    for blocker in ("maximum-scale", "user-scalable=no", "user-scalable=0"):
        assert blocker not in zoom, f"the viewport {viewport!r} blocks zoom"
    # 7. Scripts and links: hashed same-origin static paths; font preloads crossorigin.
    assert_no_injected_script(body, "page")
    for link in _tags(soup.find_all("link")):
        href = link.get("href")
        assert isinstance(href, str) and STATIC_ASSET.fullmatch(href), (
            f"<link> href {href!r} is not a hashed same-origin static path"
        )
        rel = [token.lower() for token in _values(link.get("rel"))]
        if "preload" in rel and _lower(link, "as") == "font":
            assert link.has_attr("crossorigin"), f"font preload {href!r} lacks crossorigin"
    # 8. No style attribute or element, no javascript: URL, no meta refresh (on* is in 7).
    # 9. No attribute value starts with http:, https: or // except the inline SVG xmlns.
    assert not soup.find_all("style"), "the page has a <style> element"
    elements = _tags(soup.find_all(True))
    for element in elements:
        assert not element.has_attr("style"), f"<{element.name}> has a style attribute"
        for attribute, raw in element.attrs.items():
            for value in _values(raw):
                compact = "".join(value.split()).lower()
                assert not compact.startswith("javascript:"), (
                    f"<{element.name} {attribute}> is a javascript: URL"
                )
                if compact.startswith(("http:", "https:", "//")):
                    assert (element.name, attribute, value) == ("svg", "xmlns", SVG_NAMESPACE), (
                        f"<{element.name} {attribute}> is an absolute URL {value!r}"
                    )
    for meta in metas:
        assert _lower(meta, "http-equiv") != "refresh", "the page has a meta refresh"
    # 10. Every form POSTs with a CSRF input holding a token, or is the modal's
    # method="dialog". A partial included with ``only`` loses the context processor's
    # csrf_token unless the caller passes it, and its input then posts an empty token.
    for form in _tags(soup.find_all("form")):
        method, action = _lower(form, "method"), form.get("action")
        if method == "dialog":
            continue
        assert method == "post", f"form to {action!r} is not a POST form"
        tokens = [
            found.get("value")
            for found in _tags(form.find_all("input"))
            if found.get("name") == "csrfmiddlewaretoken"
        ]
        assert tokens, f"POST form to {action!r} has no CSRF input"
        assert all(isinstance(token, str) and token.strip() for token in tokens), (
            f"POST form to {action!r} has an empty CSRF token"
        )
    # 11. img alt/width/height; named buttons and links; labelled controls; hidden icons.
    for img in _tags(soup.find_all("img")):
        for attribute in ("alt", "width", "height"):
            assert img.has_attr(attribute), f"<img> {img.get('src')!r} lacks {attribute}"
    for control in _tags(soup.find_all(["button", "a"])):
        if control.name == "a" and not control.has_attr("href"):
            continue
        where = control.get("href") or control.get("type")
        assert _accessible_name(control, soup), f"<{control.name}> {where!r} has no accessible name"
    for control in _tags(soup.find_all(["input", "select", "textarea"])):
        if control.name == "input" and _lower(control, "type") in _UNLABELLED_INPUTS:
            continue
        name = control.get("name") or control.get("id")
        assert _labelled(control, soup), f"<{control.name}> {name!r} has no label"
    for svg in _tags(soup.find_all("svg")):
        if svg.find("title", recursive=False) is None:
            hidden = (_lower(svg, "aria-hidden"), _lower(svg, "focusable"))
            assert hidden == ("true", "false"), (
                "<svg> without a <title> must be aria-hidden and not focusable"
            )
    # 12. Element ids are unique.
    ids = Counter(str(element["id"]) for element in elements if element.has_attr("id"))
    duplicates = sorted(element_id for element_id, count in ids.items() if count > 1)
    assert not duplicates, "duplicate id " + ", ".join(repr(found) for found in duplicates)
    # 13. Tables have a caption or an ARIA name; header cells have a scope.
    for found in _tags(soup.find_all("table")):
        named = found.find("caption", recursive=False) is not None or any(
            found.has_attr(attribute) for attribute in ("aria-label", "aria-labelledby")
        )
        assert named, "<table> has no caption, aria-label or aria-labelledby"
        for cell in _tags(found.find_all("th")):
            assert cell.has_attr("scope"), f"<th> {text(cell)!r} has no scope"
    # 14. The security headers.
    for header, wanted in SECURITY_HEADERS.items():
        value = response.get(header)
        assert value == wanted, f"header {header} is {value!r}, expected {wanted!r}"
    # 15. No fixture token in the body or the Location header (TEST-STRATEGY §5.3).
    assert_no_secrets(body, SECRETS, label="page", headers=[str(response.get("Location", ""))])
    return soup
