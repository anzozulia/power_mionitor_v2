"""Frontend lint: static checks that stand in for browser tests (R1, R4, R5, UI-13;
TEST-STRATEGY §3.5 and §5.5).

This file replaces test_security.py's old template lint (test_no_template_disables_escaping),
which banned every script for the old no-JS front end. Its rules were rewritten on purpose
for the Tailwind + Alpine CSP stack, not deleted (TEST-STRATEGY §3.5, §5.5): the |safe,
autoescape off, <style, style= and on*= bans stay, script tags are allowed in exactly two
forms, the URL rule allows the SVG namespace, and mark_safe stays confined to the icon tag.

One file walk per surface. Each rule is proven against a bad and a good in-memory sample, and
the real tree must pass:

- Templates (powermon/web/templates/**/*.html): no |safe, safeseq, {% filter safe %} or
  autoescape off; no <style, style= attribute or :style binding; no on*= attribute, x-html or
  javascript: URL; no {{ or {% inside an x-*, @* or :* attribute, and nothing outside the
  @alpinejs/csp grammar in a directive value (arrow function, template literal, browser
  global, the keywords new, typeof, function, void, delete, in and instanceof, a second
  statement, an assignment to a dotted path); no http(s):// except the SVG namespace and no
  protocol-relative URL; no hard-coded /static/ path; a <script> only in the two exact
  empty-body forms and only in layouts/app.html and layouts/auth.html; every {% icon %}
  literal name has a file and every {% icon %} a class; every {% static %} literal path has
  a manifest entry (comments are skipped for those two).
- Icon SVGs (templates/icons/*.svg): no <script, on*=, style= or <style, href (xlink:href
  too) or foreignObject.
- admin.js: no eval(, new Function, string timers, document.write, innerHTML, outerHTML,
  insertAdjacentHTML, createContextualFragment, setHTMLUnsafe, parseHTMLUnsafe, srcdoc,
  sessionStorage, indexedDB, caches., serviceWorker, pushState, replaceState, window.name,
  XMLHttpRequest, sendBeacon, confirm(, alert(, prompt(, import/export, http(s)://,
  hard-coded /static/ path, FormData or the bot token field; localStorage only inside a try
  block that has a catch, and only as getItem/setItem/removeItem with the literal key
  powermon.sidebar.rail; document.cookie only as an assignment of a string starting with
  theme= (R4).
- admin.js components (06-11): the Alpine.data names are exactly the 15 of the binding
  contract and each component names its contract hooks; the theme cookie carries exactly
  the attributes ThemeView sets (Secure only on https); the relative-time floors and units
  are timefmt's; every fetch is a GET of a data-* value or a link's href with redirect:
  "manual" and no body; the confirm dialog listens on document, finds the dialog by its
  testid and injects only a 200 X-PM-Fragment response with one confirm root parsed by
  DOMParser, else navigates; the clipboard is only written, in the copy click listener;
  the reveal guard empties the key on pagehide and replaces a restored page; the only style
  write is the fleet bar's flexGrow; the OFF-after bounds are the model's.
- admin.js wave-4 audit pins: setOpen keeps aria-expanded of every drawer control the shell
  templates render (the hamburger and the close button) in step with the drawer (W4-A1),
  and retries the first nav link's focus once on the next frame while still open (W4-A3);
  the poll writes S3's count number only into its mono [data-count-value] span and the noun
  into [data-count-noun] (W4-A2); fillInstant never fills a "Never" wrapper, which has no
  <time>, so the poll shows the changed chip instead.
- admin.js wave-5 audit pins: a plain primary click on a same-page link (this page's URL but
  for a non-empty hash) in the confirm dialog closes it, clears the focus return first and
  is prevented only while the submit is pending; in a [popover] menu it hides the open menu
  through the guarded Popover API, never prevented (W5-A1); the poll records S5's rendered
  [data-power] and shows the changed chip on the location page when the JSON's power
  differs, and a power outside the engine's vocabulary makes an entry invalid (W5-A2); the
  poll writes the JSON's location total into the sidebar's [data-live="sidebar-count"].
- admin.js wave-6 audit pins: S8 step 5 flips on the JSON's power, as the server's rule
  does, never on its status (W6-A1); fleetFilter writes S3's shown count and total only
  into the description's mono spans and the noun into its own, and the whole sentence only
  when the spans are absent (W6-A3); it writes a text or a hidden flag of that polite
  region only when the value differs (W6-A4).
- admin.js code-review pins (06-REVIEW WR-02, IN-05): the submit guard releases a submit
  whose navigation never completed GUARD_RELEASE_MS (30 s, at least twice the test
  message's connect + read timeouts) after it started, through one idempotent release that
  clears the entry's timer and undoes every mark; pageshow releases a copy of every pending
  entry before it runs the registered handlers (WR-02); a submit is refused while a pending
  entry's form has the same non-null action, which S5's two test-message forms share while
  delivery fails (IN-05).
- CSS entries (powermon/web/assets/css/*.css): @import only "tailwindcss" or a ./ or ../ path;
  every url() relative or data: (comments skipped).
- Python (powermon/web/**/*.py), read with the stdlib ast module: SafeString, SafeText,
  html_safe and __html__ appear in no code; mark_safe only in templatetags/icons.py; the
  format string of format_html (and format_html_join) is literal: a string constant (an
  implicit concatenation is one constant) or a name bound exactly once in the module, by a
  module-level assignment of such a constant; format_html is always called by its own name.

Not scanned: the vendored files under static/web/vendor/, which test_vendor_manifest.py
verifies by sha256 (the Alpine build legitimately holds https:// warning strings), and the
licence texts under vendor/LICENSES/ (06-RESEARCH Pitfall 5).

The Alpine names cross-check (06-20, TEST-STRATEGY §5.5): every x-data value in the templates
(comments skipped) is a bare component name registered with Alpine.data in admin.js, and every
registered name is bound by some template; no directive holds a Django tag or variable.
Later plans extend the rule tables through ``violations(text, rules)``.

No string literal in this file (this docstring included) spells the class attribute with its
equals sign, which 06-09's class guard rejects: patterns and samples that name the class
attribute are built from CLASS.
"""

import ast
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import transaction
from django.test import Client, RequestFactory
from django.urls import reverse
from pages import by_testid, parse

from powermon.alerts import delivery
from powermon.engine.models import STATUSES
from powermon.locations.models import MAX_SECONDS, MIN_SECONDS
from powermon.web.context_processors import THEME_COOKIE, THEMES
from powermon.web.live import LiveRow, status_payload
from powermon.web.templatetags import timefmt
from powermon.web.templatetags.icons import ICONS
from powermon.web.theme import THEME_MAX_AGE, ThemeView

WEB = Path(settings.BASE_DIR) / "powermon" / "web"
TEMPLATES = WEB / "templates"
ICON_DIR = TEMPLATES / "icons"
ADMIN_JS = WEB / "static" / "web" / "admin.js"
CSS_ENTRIES = WEB / "assets" / "css"
# R1: the one web module that may call mark_safe, relative to WEB (06-06's {% icon %} tag,
# which marks only repository SVG markup chosen by an allowlisted name).
MARK_SAFE_MODULE = "templatetags/icons.py"
# The tag keyword, kept out of every string literal of this file (06-09's class guard).
CLASS = "class"
# The one absolute URL a template may hold: the namespace of an inline svg.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
# The two script tags of the app and auth layouts, in their exact form (06-UI-SPEC
# Interaction Contract > JavaScript rules): admin.js render-blocking, Alpine deferred.
ALLOWED_SCRIPTS = (
    "<script src=\"{% static 'web/admin.js' %}\"></script>",
    "<script src=\"{% static 'web/vendor/alpine-csp-3.17.4.min.js' %}\" defer></script>",
)
SCRIPT_LAYOUTS = frozenset({"layouts/app.html", "layouts/auth.html"})

type Rule = re.Pattern[str] | Callable[[str], bool]
type Rules = Mapping[str, Rule]


def violations(text: str, rules: Rules) -> list[str]:
    """The names of the rules that fire on ``text``, sorted.

    A pattern fires when it is found anywhere in the text; a callable fires when it returns
    True for the text.
    """
    return sorted(
        name
        for name, rule in rules.items()
        if (rule.search(text) is not None if isinstance(rule, re.Pattern) else rule(text))
    )


# Templates (R1, R5, UI-13)

_I = re.IGNORECASE
# An Alpine directive attribute (x-*, @* or :*) and its value. The name must not follow a
# letter, digit or hyphen, so data-x-*, Tailwind variants such as md:pl-0 and URLs never match.
_DIRECTIVE = re.compile(
    r"(?<![\w-])(?:x-[\w:.-]+|@[\w:.-]+|:[\w:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>\"']+)"
)
# What the @alpinejs/csp parser rejects or the project bans in a directive value (06-RESEARCH
# Pattern 4): arrow functions, template literals, browser globals, the keywords new, typeof,
# function, void, delete, in and instanceof (whole words: a Tailwind token such as ease-in
# in an x-transition value is not one), a second statement (a ";" followed by more text; one
# trailing ";" is allowed) and an assignment to a dotted path (user.name = x, a.b += 1).
_OUTSIDE_CSP_GRAMMAR = re.compile(
    r"=>|`|(?<![\w$.])(?:window|document|globalThis|console|JSON|Math|eval|Function)\b"
    r"|\b(?:new|typeof|function)\b"
    r"|(?<![\w$.-])(?:void|delete|in|instanceof)(?![\w$-])"
    r"|;\s*\S"
    r"|[\w$]+\.[\w$.]+\s*(?:[-+*/%&|^]|\*\*|<<|>>>?|&&|\|\||\?\?)?=(?!=)"
)
_URL_TOKEN = re.compile(r"https?://[^\s\"'<>()]*", _I)
_PROTOCOL_RELATIVE = re.compile(r"(?:=|url\()\s*[\"']?\s*//", _I)
_TEMPLATE_COMMENT = re.compile(r"{%\s*comment\b.*?{%\s*endcomment\s*%}|{#.*?#}", re.DOTALL)
_SCRIPT_TAG = re.compile(r"<script\b", _I)
_ICON_TAG = re.compile(r"{%\s*icon\b(?P<args>.*?)%}", re.DOTALL)
_ICON_NAME = re.compile(r"\s*([\"'])(?P<name>[^\"']*)\1")
_ICON_CLASS = re.compile(rf"\b{CLASS}\s*=")
_STATIC_TAG = re.compile(r"{%\s*static\s+([\"'])(?P<path>[^\"']+)\1")


def _directive_values(text: str) -> Iterator[str]:
    """Every directive value, without its quotes."""
    for match in _DIRECTIVE.finditer(text):
        value = match.group(1)
        yield value[1:-1] if value[:1] in "\"'" else value


def _django_in_directive(text: str) -> bool:
    return any("{{" in value or "{%" in value for value in _directive_values(text))


def _outside_csp_grammar(text: str) -> bool:
    return any(_OUTSIDE_CSP_GRAMMAR.search(value) for value in _directive_values(text))


def _other_origin(text: str) -> bool:
    urls = (match.group(0) for match in _URL_TOKEN.finditer(text))
    return any(url != SVG_NAMESPACE for url in urls) or bool(_PROTOCOL_RELATIVE.search(text))


TEMPLATE_RULES: Rules = {
    "safe filter": re.compile(r"\|\s*safe\b"),
    "safeseq filter": re.compile(r"\bsafeseq\b"),
    "filter safe block": re.compile(r"{%\s*filter\b[^%]*\bsafe\b"),
    "autoescape off": re.compile(r"{%\s*autoescape\s+off\b", _I),
    "style element": re.compile(r"<style\b", _I),
    "style attribute": re.compile(r"(?<![\w:-])style\s*=", _I),
    "style binding": re.compile(r"(?<![\w-])(?:x-bind)?:style\s*=", _I),
    "on* handler": re.compile(r"(?<![\w:@.-])on[a-z]+\s*=", _I),
    "x-html": re.compile(r"\bx-html\b", _I),
    "javascript: URL": re.compile(r"javascript\s*:", _I),
    "Django inside a directive": _django_in_directive,
    "directive outside the CSP grammar": _outside_csp_grammar,
    "URL to another origin": _other_origin,
    "hard-coded /static/ path": re.compile(r"[\"'(=]\s*/static/"),
}


def script_violations(relpath: str, text: str) -> list[str]:
    """Script tags: only the two exact empty-body forms, once each, only in the two layouts."""
    found: set[str] = set()
    seen: Counter[str] = Counter()
    for match in _SCRIPT_TAG.finditer(text):
        form = next((f for f in ALLOWED_SCRIPTS if text.startswith(f, match.start())), None)
        if form is None:
            found.add("script other than the two allowed forms")
        elif relpath not in SCRIPT_LAYOUTS:
            found.add("script outside the app and auth layouts")
        else:
            seen[form] += 1
    if any(count > 1 for count in seen.values()):
        found.add("script tag repeated")
    return sorted(found)


def icon_violations(text: str) -> list[str]:
    """{% icon %} tags (comments skipped): a literal name needs a file, every tag a class."""
    found: set[str] = set()
    for match in _ICON_TAG.finditer(_TEMPLATE_COMMENT.sub("", text)):
        args = match.group("args")
        name = _ICON_NAME.match(args)
        if name is not None and name.group("name") not in ICONS:
            found.add("icon with no file")
        if _ICON_CLASS.search(args) is None:
            found.add("icon without class")
    return sorted(found)


def static_violations(text: str) -> list[str]:
    """{% static %} literal paths (comments skipped) that the manifest does not hold."""
    for match in _STATIC_TAG.finditer(_TEMPLATE_COMMENT.sub("", text)):
        try:
            staticfiles_storage.stored_name(match.group("path"))
        except ValueError:
            return ["static path with no manifest entry"]
    return []


def template_violations(relpath: str, text: str) -> list[str]:
    """Every template rule for the template at ``relpath`` (relative to the templates dir)."""
    return sorted(
        {
            *violations(text, TEMPLATE_RULES),
            *script_violations(relpath, text),
            *icon_violations(text),
            *static_violations(text),
        }
    )


def template_files() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


# Icon SVGs (R1, UI-13)

ICON_RULES: Rules = {
    "script": re.compile(r"<script\b", _I),
    "on* handler": re.compile(r"(?<![\w:-])on[a-z]+\s*=", _I),
    "style attribute": re.compile(r"(?<![\w:-])style\s*=", _I),
    "style element": re.compile(r"<style\b", _I),
    "href": re.compile(r"href", _I),
    "foreignObject": re.compile(r"foreignObject", _I),
}


def icon_files() -> list[Path]:
    return sorted(ICON_DIR.glob("*.svg"))


# admin.js (R1, R4, R5)

_LOCAL_STORAGE = re.compile(r"\blocalStorage\b")
_RAIL_ACCESS = re.compile(
    r"\s*\.\s*(?:getItem|setItem|removeItem)\s*\(\s*([\"'])powermon\.sidebar\.rail\1"
)
_COOKIE = re.compile(r"\bdocument\s*\.\s*cookie\b")
_THEME_COOKIE_WRITE = re.compile(r"\s*=(?!=)\s*([\"'])theme=")
_TRY_BEFORE = re.compile(r"(?<![\w$])try\s*$")
_CATCH_AFTER = re.compile(r"\s*catch\b")


def _mask_js(source: str) -> str:
    """``source`` with comment, string and template-literal contents blanked, same length.

    Regular-expression literals are not recognised; admin.js keeps quotes and braces out of
    them.
    """
    out = list(source)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if out[index] != "\n":
                out[index] = " "

    index, size = 0, len(source)
    while index < size:
        pair, char = source[index : index + 2], source[index]
        if pair in ("//", "/*"):
            close = source.find("\n" if pair == "//" else "*/", index + 2)
            end = size if close < 0 else close + (0 if pair == "//" else 2)
            blank(index, end)
            index = end
        elif char in "\"'`":
            end = index + 1
            while end < size and source[end] != char:
                end += 2 if source[end] == "\\" else 1
            blank(index + 1, min(end, size))
            index = end + 1
        else:
            index += 1
    return "".join(out)


def _try_catch_blocks(source: str) -> list[tuple[int, int]]:
    """(open brace, close brace) offsets of every try block followed by a catch clause."""
    code = _mask_js(source)
    blocks: list[tuple[int, int]] = []
    stack: list[tuple[int, bool]] = []
    for index, char in enumerate(code):
        if char == "{":
            stack.append((index, bool(_TRY_BEFORE.search(code[max(0, index - 64) : index]))))
        elif char == "}" and stack:
            start, is_try = stack.pop()
            if is_try and _CATCH_AFTER.match(code, index + 1):
                blocks.append((start, index))
    return blocks


def _local_storage_misuse(source: str) -> bool:
    blocks = _try_catch_blocks(source)
    for match in _LOCAL_STORAGE.finditer(source):
        if _RAIL_ACCESS.match(source, match.end()) is None:
            return True
        if not any(start < match.start() < end for start, end in blocks):
            return True
    return False


def _cookie_misuse(source: str) -> bool:
    return any(
        _THEME_COOKIE_WRITE.match(source, match.end()) is None for match in _COOKIE.finditer(source)
    )


def _global_call(name: str) -> re.Pattern[str]:
    """A call of the browser dialog ``name``: bare or through window, self or globalThis."""
    return re.compile(rf"(?:\b(?:window|self|globalThis)\s*\.\s*|(?<![\w$.])){name}\s*\(")


ADMIN_JS_RULES: Rules = {
    "eval": re.compile(r"\beval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b|\bFunction\s*\("),
    "string timer": re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]"),
    "document.write": re.compile(r"\bdocument\s*\.\s*write"),
    "innerHTML": re.compile(r"\binnerHTML\b"),
    "outerHTML": re.compile(r"\bouterHTML\b"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "createContextualFragment": re.compile(r"\bcreateContextualFragment\b"),
    "setHTMLUnsafe": re.compile(r"\bsetHTMLUnsafe\b"),
    "parseHTMLUnsafe": re.compile(r"\bparseHTMLUnsafe\b"),
    "srcdoc": re.compile(r"\bsrcdoc\b", _I),
    "sessionStorage": re.compile(r"\bsessionStorage\b"),
    "indexedDB": re.compile(r"\bindexedDB\b"),
    "Cache Storage": re.compile(r"\bcaches\s*\."),
    "service worker": re.compile(r"\bserviceWorker\b"),
    "pushState": re.compile(r"\bpushState\b"),
    "replaceState": re.compile(r"\breplaceState\b"),
    "window.name": re.compile(r"\bwindow\s*\.\s*name\b"),
    "XMLHttpRequest": re.compile(r"\bXMLHttpRequest\b"),
    "sendBeacon": re.compile(r"\bsendBeacon\b"),
    "confirm(": _global_call("confirm"),
    "alert(": _global_call("alert"),
    "prompt(": _global_call("prompt"),
    "import or export": re.compile(r"^\s*(?:import|export)\b|\bimport\s*\(", re.MULTILINE),
    "http(s) URL": re.compile(r"https?://", _I),
    "hard-coded /static/ path": re.compile(r"/static/"),
    "FormData": re.compile(r"\bFormData\b"),
    "bot token field": re.compile(r"id_bot_token"),
    "localStorage": _local_storage_misuse,
    "document.cookie": _cookie_misuse,
}


# CSS entries (R5, UI-13)

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?([\"']?)(?P<target>[^\"')\s;]+)\1", _I)
_CSS_URL = re.compile(r"\burl\(\s*([\"']?)(?P<value>.*?)\1\s*\)", _I | re.DOTALL)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", _I)


def _bad_import(css: str) -> bool:
    targets = (m.group("target") for m in _CSS_IMPORT.finditer(_CSS_COMMENT.sub("", css)))
    return any(t != "tailwindcss" and not t.startswith(("./", "../")) for t in targets)


def _bad_url(css: str) -> bool:
    for match in _CSS_URL.finditer(_CSS_COMMENT.sub("", css)):
        value = match.group("value").strip()
        if value.lower().startswith("data:"):
            continue
        if value.startswith(("/", "\\")) or _SCHEME.match(value):
            return True
    return False


CSS_RULES: Rules = {
    "@import other than tailwindcss or a relative path": _bad_import,
    "url() that is not relative or data:": _bad_url,
}


def css_entries() -> list[Path]:
    return sorted(CSS_ENTRIES.glob("*.css"))


# Python under powermon/web (R1)

_BANNED_NAMES = ("SafeString", "SafeText", "html_safe", "__html__")
# The functions whose format string must be literal, with its position and keyword name.
_FORMAT_FUNCTIONS = {"format_html": 0, "format_html_join": 1}


def _names_of(node: ast.AST) -> Iterator[str]:
    """The names a node refers to or defines in code (strings and comments are not code)."""
    match node:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            yield name
        case ast.alias(name=name, asname=asname):
            yield name.rsplit(".", 1)[-1]
            if asname is not None:
                yield asname
        case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name) | ast.ClassDef(name=name):
            yield name


def _bound_names(tree: ast.AST) -> Iterator[str]:
    """Every binding of a name anywhere in the module, one item per binding."""
    for node in ast.walk(tree):
        match node:
            case ast.Name(id=name, ctx=ast.Store() | ast.Del()):
                yield name
            case ast.arg(arg=name):
                yield name
            case (
                ast.FunctionDef(name=name)
                | ast.AsyncFunctionDef(name=name)
                | ast.ClassDef(name=name)
            ):
                yield name
            case ast.alias(name=name, asname=asname):
                yield asname or name.split(".")[0]
            case ast.ExceptHandler(name=str(name)):
                yield name
            case ast.Global(names=names) | ast.Nonlocal(names=names):
                yield from names
            case ast.MatchAs(name=str(name)) | ast.MatchStar(name=str(name)):
                yield name
            case ast.MatchMapping(rest=str(name)):
                yield name
            case ast.TypeVar(name=name) | ast.ParamSpec(name=name) | ast.TypeVarTuple(name=name):
                yield name


def _string_constant(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _literal_names(tree: ast.Module) -> frozenset[str]:
    """Names bound exactly once in the module, by a module-level string-constant assignment."""
    bindings = Counter(_bound_names(tree))
    names: set[str] = set()
    for statement in tree.body:
        match statement:
            case (
                ast.Assign(targets=[ast.Name(id=name)], value=value)
                | ast.AnnAssign(target=ast.Name(id=name), value=value)
            ) if _string_constant(value) and bindings[name] == 1:
                names.add(name)
    return frozenset(names)


def _format_string(call: ast.Call, position: int) -> ast.expr | None:
    """The call's format-string argument, by position or keyword; None when it is unknown."""
    if any(isinstance(arg, ast.Starred) for arg in call.args[: position + 1]):
        return None
    if len(call.args) > position:
        return call.args[position]
    return next((kw.value for kw in call.keywords if kw.arg == "format_string"), None)


def _callee(call: ast.Call) -> str | None:
    match call.func:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
    return None


def python_violations(relpath: str, source: str) -> list[str]:
    """R1 in the web module at ``relpath`` (relative to powermon/web)."""
    tree = ast.parse(source)
    literal = _literal_names(tree)
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    found: set[str] = set()
    for node in ast.walk(tree):
        names = set(_names_of(node))
        found.update(f"{name} used" for name in _BANNED_NAMES if name in names)
        if "mark_safe" in names and relpath != MARK_SAFE_MODULE:
            found.add("mark_safe outside the icon tag")
        if isinstance(node, ast.alias) and node.name.endswith("format_html"):
            if node.asname not in (None, "format_html"):
                found.add("format_html imported under another name")
        if isinstance(node, ast.Name | ast.Attribute) and "format_html" in names:
            if id(node) not in called:
                found.add("format_html not called directly")
        if isinstance(node, ast.Call) and (callee := _callee(node)) in _FORMAT_FUNCTIONS:
            argument = _format_string(node, _FORMAT_FUNCTIONS[callee])
            is_literal = _string_constant(argument) or (
                isinstance(argument, ast.Name) and argument.id in literal
            )
            if not is_literal:
                found.add(f"{callee} with a non-literal format string")
    return sorted(found)


def web_modules() -> list[Path]:
    return sorted(WEB.rglob("*.py"))


# Shared samples (06-09's class guard: the class keyword comes from CLASS)

ICON_OK = '{% icon "zap" ' + CLASS + '="size-4" %}'
GOOD_TEMPLATE = (
    "{% load static icons %}"
    '<link rel="stylesheet" href="{% static \'web/build/app.css\' %}">'
    '<form method="post" x-data="theme" @submit="choose" x-on:click.prevent="toggle">'
    '<button :aria-pressed="pressed" type="submit">' + ICON_OK + "Dark</button></form>"
    '<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true"></svg>'
    '<p data-x-note="1">{{ name }} options online</p>'
    "{% comment %}{% icon missing %} {% static 'web/missing.css' %}{% endcomment %}"
)


# Templates


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param(GOOD_TEMPLATE, [], id="good"),
        pytest.param("<p>{{ name|safe }}</p>", ["safe filter"], id="safe"),
        pytest.param("<p>{{ name | safe }}</p>", ["safe filter"], id="safe-spaced"),
        pytest.param('{{ items|safeseq|join:", " }}', ["safeseq filter"], id="safeseq"),
        pytest.param(
            "{% filter safe %}{{ x }}{% endfilter %}", ["filter safe block"], id="filter-safe"
        ),
        pytest.param(
            "{% autoescape off %}{{ x }}{% endautoescape %}", ["autoescape off"], id="autoescape"
        ),
        pytest.param("<style>p{}</style>", ["style element"], id="style-element"),
        pytest.param('<p style="color: red">x</p>', ["style attribute"], id="style-attribute"),
        pytest.param('<div :style="bar"></div>', ["style binding"], id="style-binding"),
        pytest.param('<div x-bind:style="bar"></div>', ["style binding"], id="x-bind-style"),
        pytest.param('<button onclick="go()">x</button>', ["on* handler"], id="onclick"),
        pytest.param('<img src="x" ONERROR="go()" alt="">', ["on* handler"], id="onerror"),
        pytest.param('<div x-html="body"></div>', ["x-html"], id="x-html"),
        pytest.param('<a href="javascript:go()">x</a>', ["javascript: URL"], id="javascript"),
        pytest.param(
            '<div x-data="{{ component }}"></div>', ["Django inside a directive"], id="x-data-var"
        ),
        pytest.param(
            '<form @submit="{% if a %}choose{% endif %}"></form>',
            ["Django inside a directive"],
            id="at-tag",
        ),
        pytest.param(
            "<p :title='{{ name }}'></p>", ["Django inside a directive"], id="bind-single-quoted"
        ),
        pytest.param(
            '<button x-on:click="() => go()"></button>',
            ["directive outside the CSP grammar"],
            id="arrow",
        ),
        pytest.param(
            '<p x-text="window.location"></p>',
            ["directive outside the CSP grammar"],
            id="global",
        ),
        *(
            pytest.param(sample, ["directive outside the CSP grammar"], id=name)
            for name, sample in (
                ("two-statements", '<button x-on:click="a(); b()"></button>'),
                ("in", """<p x-show="'k' in obj"></p>"""),
                ("delete", '<button x-on:click="delete items.x"></button>'),
                ("void", '<button x-on:click="void go()"></button>'),
                ("instanceof", '<p x-show="el instanceof Element"></p>'),
                ("dotted-assignment", """<button x-on:click="user.name = 'x'"></button>"""),
                ("dotted-compound", '<button @click="item.count += 1"></button>'),
            )
        ),
        pytest.param(
            '<button x-on:click="go();" x-transition:enter="transition ease-in duration-150"'
            ' :aria-pressed="a.b === c" x-show="item.count <= 3 && a.b !== d"'
            ' x-text="label"></button>',
            [],
            id="grammar-ok",
        ),
        pytest.param(
            '<a href="https://cdn.example.com/x.js">x</a>', ["URL to another origin"], id="https"
        ),
        pytest.param(
            '<img src="//cdn.example/x.png" alt="">',
            ["URL to another origin"],
            id="protocol-relative",
        ),
        pytest.param(
            '<svg xmlns="http://www.w3.org/2000/svg.evil.example"></svg>',
            ["URL to another origin"],
            id="namespace-lookalike",
        ),
        pytest.param(
            '<link rel="stylesheet" href="/static/web/app.css">',
            ["hard-coded /static/ path"],
            id="hard-coded-static",
        ),
        pytest.param(
            "<script>alert(1)</script>", ["script other than the two allowed forms"], id="script"
        ),
        pytest.param(
            '{% icon "no-such-icon" ' + CLASS + '="size-4" %}', ["icon with no file"], id="icon"
        ),
        pytest.param('{% icon "zap" %}', ["icon without class"], id="icon-class"),
        pytest.param(
            "{% static 'web/missing.css' %}", ["static path with no manifest entry"], id="static"
        ),
    ],
)
def test_frontend_lint_templates(sample: str, expected: list[str]) -> None:
    assert template_violations("web/sample.html", sample) == expected


def test_frontend_lint_templates_real_tree() -> None:
    files = template_files()
    names = {path.relative_to(TEMPLATES).as_posix() for path in files}
    # The new tree replaces the old fixed template-name set (TEST-STRATEGY §3.5).
    assert {
        "layouts/error.html",
        "partials/_button.html",
        "partials/_toasts.html",
        "partials/_alert.html",
        "404.html",
        "403_csrf.html",
        "500.html",
    } <= names
    report = {
        path.relative_to(TEMPLATES).as_posix(): template_violations(
            path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")
        )
        for path in files
    }
    assert {name: found for name, found in report.items() if found} == {}


def test_lint_allows_only_the_two_script_tags() -> None:
    admin, alpine = ALLOWED_SCRIPTS
    head = "<head>" + admin + alpine + "</head>"

    # Expected: both exact forms, once each, in the app and the auth layout.
    assert script_violations("layouts/app.html", head) == []
    assert script_violations("layouts/auth.html", head) == []
    assert template_violations("layouts/app.html", "{% load static %}" + head) == []
    # Edge: either form in any other template.
    for relpath in ("layouts/error.html", "web/location_list.html", "partials/_toasts.html"):
        assert script_violations(relpath, admin) == ["script outside the app and auth layouts"]
    # Failure: a body, a third script, another attribute order, a missing defer, a repeat.
    other = "script other than the two allowed forms"
    assert script_violations("layouts/app.html", admin.replace("></", ">x</")) == [other]
    third = "<script src=\"{% static 'web/other.js' %}\"></script>"
    assert script_violations("layouts/app.html", head + third) == [other]
    deferred_admin = admin.replace("></script>", " defer></script>")
    assert script_violations("layouts/app.html", deferred_admin) == [other]
    assert script_violations("layouts/app.html", alpine.replace(" defer", "")) == [other]
    assert script_violations("layouts/app.html", admin.upper()) == [other]
    assert script_violations("layouts/app.html", head + admin) == ["script tag repeated"]


def test_lint_allows_the_svg_namespace() -> None:
    namespace = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"></svg>'

    assert violations(namespace, TEMPLATE_RULES) == []
    assert violations("{# " + SVG_NAMESPACE + " #}", TEMPLATE_RULES) == []
    for url in (
        "http://www.w3.org/1999/xlink",
        "https://www.w3.org/2000/svg",
        "HTTP://www.w3.org/2000/svg",
        "http://example.com/",
        "https://fonts.googleapis.com/css2",
    ):
        sample = namespace.replace(SVG_NAMESPACE, url)
        assert violations(sample, TEMPLATE_RULES) == ["URL to another origin"], url


# Icon SVGs


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param(
            '<svg xmlns="http://www.w3.org/2000/svg" stroke-width="2"><path d="M4 14h6"/></svg>',
            [],
            id="good",
        ),
        pytest.param("<svg><script>go()</script></svg>", ["script"], id="script"),
        pytest.param('<svg onload="go()"></svg>', ["on* handler"], id="onload"),
        pytest.param('<svg><path style="fill:red"/></svg>', ["style attribute"], id="style"),
        pytest.param("<svg><style>path{}</style></svg>", ["style element"], id="style-element"),
        pytest.param('<svg><a href="/x"><path/></a></svg>', ["href"], id="href"),
        pytest.param('<svg><use xlink:href="#x"/></svg>', ["href"], id="xlink-href"),
        pytest.param(
            "<svg><foreignObject><div>x</div></foreignObject></svg>",
            ["foreignObject"],
            id="foreign-object",
        ),
    ],
)
def test_frontend_lint_icons(sample: str, expected: list[str]) -> None:
    assert violations(sample, ICON_RULES) == expected


def test_frontend_lint_icons_real_tree() -> None:
    files = icon_files()

    # Every allowlisted icon of the tag is a file the lint read.
    assert {path.stem for path in files} == set(ICONS)
    report = {path.name: violations(path.read_text(encoding="utf-8"), ICON_RULES) for path in files}
    assert {name: found for name, found in report.items() if found} == {}


# admin.js

GOOD_JS = """(function () {
  "use strict";
  try {
    if (window.localStorage.getItem("powermon.sidebar.rail") === "1") {
      document.documentElement.setAttribute("data-rail", "collapsed");
    }
  } catch (error) {
    // Blocked storage.
  }
  function save(collapsed) {
    try {
      if (collapsed) { localStorage.setItem('powermon.sidebar.rail', "1"); }
      else { window.localStorage.removeItem("powermon.sidebar.rail"); }
    } catch (error) {}
  }
  document.addEventListener("alpine:init", function () {
    window.Alpine.data("theme", function () {
      return { choose: function (value) { document.cookie = "theme=" + value + "; Path=/"; } };
    });
  });
  var parsed = new DOMParser().parseFromString("<p></p>", "text/html");
  window.setTimeout(function () { save(true); }, 10);
  this.confirmDialog(parsed); element.textContent = "Copied";
})();
"""


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param(GOOD_JS, [], id="good"),
        pytest.param("eval('1');", ["eval"], id="eval"),
        pytest.param("var f = new Function('return 1');", ["new Function"], id="new-function"),
        pytest.param("var f = Function('return 1');", ["new Function"], id="function-call"),
        pytest.param("setTimeout('go()', 10);", ["string timer"], id="string-timer"),
        pytest.param("document.write('<p>');", ["document.write"], id="document-write"),
        pytest.param("el.innerHTML = text;", ["innerHTML"], id="inner-html"),
        pytest.param("el.outerHTML = text;", ["outerHTML"], id="outer-html"),
        pytest.param("el.insertAdjacentHTML('beforeend', t);", ["insertAdjacentHTML"], id="iah"),
        pytest.param(
            "range.createContextualFragment(t);", ["createContextualFragment"], id="fragment"
        ),
        pytest.param("el.setHTMLUnsafe(t);", ["setHTMLUnsafe"], id="set-html-unsafe"),
        pytest.param("Document.parseHTMLUnsafe(t);", ["parseHTMLUnsafe"], id="parse-unsafe"),
        pytest.param("frame.srcdoc = t;", ["srcdoc"], id="srcdoc"),
        pytest.param("sessionStorage.setItem('k', key);", ["sessionStorage"], id="session"),
        pytest.param("indexedDB.open('db');", ["indexedDB"], id="indexed-db"),
        pytest.param("caches.open('v1');", ["Cache Storage"], id="caches"),
        pytest.param("navigator.serviceWorker.register('/sw.js');", ["service worker"], id="sw"),
        pytest.param("history.pushState({key: k}, '');", ["pushState"], id="push-state"),
        pytest.param("history.replaceState({key: k}, '');", ["replaceState"], id="replace"),
        pytest.param("window.name = key;", ["window.name"], id="window-name"),
        pytest.param("new XMLHttpRequest();", ["XMLHttpRequest"], id="xhr"),
        pytest.param("navigator.sendBeacon(url, data);", ["sendBeacon"], id="beacon"),
        pytest.param("if (confirm('Delete?')) { go(); }", ["confirm("], id="confirm"),
        pytest.param("window.confirm('Delete?');", ["confirm("], id="window-confirm"),
        pytest.param("alert('x');", ["alert("], id="alert"),
        pytest.param("prompt('x');", ["prompt("], id="prompt"),
        pytest.param("import { x } from './x.js';", ["import or export"], id="import"),
        pytest.param("export function x() {}", ["import or export"], id="export"),
        pytest.param("import('./x.js');", ["import or export"], id="dynamic-import"),
        pytest.param("fetch('https://api.example.com/');", ["http(s) URL"], id="url"),
        pytest.param("fetch('/static/web/x.json');", ["hard-coded /static/ path"], id="static"),
        pytest.param("new FormData(form);", ["FormData"], id="form-data"),
        pytest.param("document.getElementById('id_bot_token');", ["bot token field"], id="token"),
        pytest.param(
            'window.localStorage.getItem("powermon.sidebar.rail");', ["localStorage"], id="no-try"
        ),
        pytest.param(
            "try { localStorage.setItem('other.key', '1'); } catch (e) {}",
            ["localStorage"],
            id="other-key",
        ),
        pytest.param(
            "try { localStorage.getItem(KEY); } catch (e) {}", ["localStorage"], id="variable-key"
        ),
        pytest.param("try { localStorage.clear(); } catch (e) {}", ["localStorage"], id="clear"),
        pytest.param(
            'try { localStorage.getItem("powermon.sidebar.rail"); } finally {}',
            ["localStorage"],
            id="no-catch",
        ),
        pytest.param(
            'var s = "try {"; localStorage.getItem("powermon.sidebar.rail"); var t = "}";',
            ["localStorage"],
            id="try-in-a-string",
        ),
        pytest.param('document.cookie = "sessionid=x";', ["document.cookie"], id="other-cookie"),
        pytest.param("var all = document.cookie;", ["document.cookie"], id="cookie-read"),
        pytest.param('document.cookie += "theme=dark";', ["document.cookie"], id="cookie-append"),
    ],
)
def test_frontend_lint_admin_js(sample: str, expected: list[str]) -> None:
    assert violations(sample, ADMIN_JS_RULES) == expected


def test_frontend_lint_admin_js_real_tree() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert violations(source, ADMIN_JS_RULES) == []
    # The real file uses the storage it is allowed: the rail flag, inside a try/catch.
    assert len(_LOCAL_STORAGE.findall(source)) >= 1
    assert _try_catch_blocks(source) != []


# admin.js components (06-11 binding contract)

# The 15 Alpine.data names of the 06-11 binding contract: toasts from 06-07, the rest 06-11.
CONTRACT_NAMES = frozenset(
    {
        "toasts",
        "sidebar",
        "theme",
        "poll",
        "relative",
        "copy",
        "tabs",
        "confirmDialog",
        "chartImage",
        "fleetFilter",
        "offAfterHint",
        "errorSummary",
        "throttleCountdown",
        "revealGuard",
        "sectionNav",
    }
)
_REGISTRATION = re.compile(r"\bAlpine\s*\.\s*data\s*\(\s*([\"'])(?P<name>[^\"']*)\1")
_COOKIE_WRITE = re.compile(r"\bdocument\s*\.\s*cookie\s*=(?!=)")
_JS_STRING = re.compile(r"\"([^\"\\]*)\"|'([^'\\]*)'")
# The one allowed form of the Secure attribute: appended only on an https page.
_SECURE_ON_HTTPS = re.compile(
    r"\(\s*(?:window\s*\.\s*)?location\s*\.\s*protocol\s*===\s*([\"'])https:\1"
    r"\s*\?\s*([\"']); Secure\2\s*:\s*([\"'])\3\s*\)"
)


def _closing(code: str, start: int, opening: str = "(", closing: str = ")") -> int:
    """Offset of the bracket that closes the first ``opening`` at or after ``start``."""
    depth = 0
    for index in range(code.index(opening, start), len(code)):
        if code[index] == opening:
            depth += 1
        elif code[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return len(code) - 1


def registered_names(source: str) -> list[str]:
    """Every Alpine.data("<name>" registration in code order (comments and strings skipped)."""
    code = _mask_js(source)
    return [m.group("name") for m in _REGISTRATION.finditer(source) if code[m.start()] != " "]


def component_spans(source: str) -> dict[str, tuple[int, int]]:
    """(start, end) offsets of each ``Alpine.data(...)`` call, by name (the first one)."""
    code = _mask_js(source)
    spans: dict[str, tuple[int, int]] = {}
    for match in _REGISTRATION.finditer(source):
        if code[match.start()] != " ":
            spans.setdefault(match.group("name"), (match.start(), _closing(code, match.start())))
    return spans


def component_bodies(source: str) -> dict[str, str]:
    """The text of each ``Alpine.data(...)`` call, by name (the first one for a repeat)."""
    return {name: source[start : end + 1] for name, (start, end) in component_spans(source).items()}


def _code_matches(pattern: re.Pattern[str], source: str) -> Iterator[re.Match[str]]:
    """Matches of ``pattern`` in the raw source that start in code (not a comment/string)."""
    code = _mask_js(source)
    return (m for m in pattern.finditer(source) if code[m.start()] != " ")


_FETCH_CALL = re.compile(r"(?<![\w$])fetch\s*\(")
_FETCH_NAME = re.compile(r"(?<![\w$])fetch\b")
# A fetch URL is a value the server rendered into the page: a data-* value or a link's href.
_FETCH_URL = re.compile(r"[\w$]+(?:\.[\w$]+)*\.(?:dataset\.[\w$]+|href)")


def fetch_calls(source: str) -> list[tuple[str, str]]:
    """(first argument, rest of the arguments) of every fetch(...) call in code."""
    code = _mask_js(source)
    calls = []
    for match in _code_matches(_FETCH_CALL, source):
        opening = code.index("(", match.start())
        closing = _closing(code, match.start())
        depth, comma = 0, closing
        for index in range(opening + 1, closing):
            if code[index] in "([{":
                depth += 1
            elif code[index] in ")]}":
                depth -= 1
            elif code[index] == "," and depth == 0:
                comma = index
                break
        calls.append((source[opening + 1 : comma].strip(), source[comma + 1 : closing].strip()))
    return calls


def fetch_violations(source: str) -> list[str]:
    """TEST-STRATEGY §5.5: every fetch is a GET of a server-rendered same-origin URL that
    never follows a redirect (T-06-35, T-06-37)."""
    found: set[str] = set()
    calls = fetch_calls(source)
    if sum(1 for _ in _code_matches(_FETCH_NAME, source)) != len(calls):
        found.add("fetch used other than as a call")
    for url, options in calls:
        if _FETCH_URL.fullmatch(url) is None:
            found.add("fetch URL not from a data-* value or a link's href")
        if re.search(r"\bredirect\s*:\s*([\"'])manual\1", options) is None:
            found.add('fetch without redirect: "manual"')
        if re.search(r"\bmethod\s*:", options) and not re.search(
            r"\bmethod\s*:\s*([\"'])GET\1", options
        ):
            found.add("fetch method other than GET")
        if re.search(r"\bbody\s*:", options):
            found.add("fetch with a body")
    return sorted(found)


_CLIPBOARD_WRITE = re.compile(r"\bnavigator\s*\.\s*clipboard\s*\.\s*writeText\s*\(")
_CLICK_LISTENER = re.compile(r"\.\s*addEventListener\s*\(\s*([\"'])click\1")


def clipboard_violations(source: str) -> list[str]:
    """UI-08 / R4: the clipboard is only written, only by the copy component, only inside a
    click listener; it is never read."""
    code = _mask_js(source)
    found: set[str] = set()
    if re.search(r"\bclipboard\s*\.\s*read|\bexecCommand\b", code):
        found.add("clipboard read or execCommand")
    start, end = component_spans(source).get("copy", (-1, -1))
    clicks = [
        (m.start(), _closing(code, m.start()))
        for m in _code_matches(_CLICK_LISTENER, source)
        if start < m.start() < end
    ]
    writes = [m.start() for m in _code_matches(_CLIPBOARD_WRITE, source)]
    if not writes:
        found.add("no clipboard write")
    if any(not any(a < w < b for a, b in clicks) for w in writes):
        found.add("clipboard write outside the copy component's click listener")
    return sorted(found)


_FRAGMENT_HEADER = "X-PM-Fragment"


def fragment_violations(source: str) -> list[str]:
    """UI-07 / T-06-32: the confirm dialog injects the server's confirmation only from a 200
    response carrying X-PM-Fragment: 1 with exactly one confirm root parsed by DOMParser,
    and falls back to a full navigation for every other outcome."""
    start, end = component_spans(source).get("confirmDialog", (0, -1))
    body = source[start : end + 1]
    found: set[str] = set()
    outside = source[:start] + source[end + 1 :]
    if _FRAGMENT_HEADER in outside:
        found.add("X-PM-Fragment outside confirmDialog")
    fragment_fetches = [
        options
        for _, options in fetch_calls(body)
        if re.search(r"([\"'])X-PM-Fragment\1\s*:\s*([\"'])1\2", options)
    ]
    if len(fragment_fetches) != 1:
        found.add("no single fetch sending X-PM-Fragment: 1")
    checks = {
        "no status 200 check": r"\.status\s*!==?\s*200|\.status\s*===?\s*200",
        "no response header check": (
            r"headers\s*\.\s*get\s*\(\s*([\"'])X-PM-Fragment\1\s*\)\s*[!=]==?\s*([\"'])1\2"
        ),
        "no DOMParser": r"new\s+DOMParser\s*\(\s*\)\s*\.\s*parseFromString\s*\([^)]*text/html",
        "no single-root check": (
            r"querySelectorAll\s*\(\s*'\[data-testid=\"confirm\"\]'\s*\)"
            r"[\s\S]*?\.length\s*[!=]==?\s*1"
        ),
        "no opaque-redirect fallback": r"([\"'])opaqueredirect\1",
        "no location.assign fallback": r"\blocation\s*\.\s*assign\s*\(",
        "no showModal": r"\.showModal\s*\(",
    }
    found.update(name for name, pattern in checks.items() if re.search(pattern, body) is None)
    return sorted(found)


_DOCUMENT_CLICK = re.compile(r"\bdocument\s*\.\s*addEventListener\s*\(\s*([\"'])click\1")


def confirm_scope_violations(source: str) -> list[str]:
    """06-12 / 06-15: the dialog block renders after main and the kebab entries sit outside
    the [data-confirm-scope] wrapper, so confirmDialog listens on document and finds the
    dialog with document.querySelector, never inside its own element."""
    body = component_bodies(source).get("confirmDialog", "")
    code = _mask_js(body)
    found: set[str] = set()
    if not any(True for _ in _code_matches(_DOCUMENT_CLICK, body)):
        found.add("a[data-confirm] click listener not on document")
    if re.search(r"\$el\s*\.\s*addEventListener\s*\(\s*([\"'])click\1", body):
        found.add("click listener on the component's element")
    lookups = [
        body[m.start() : _closing(code, m.start()) + 1]
        for m in _code_matches(re.compile(r"\bdocument\s*\.\s*querySelector\s*\("), body)
    ]
    if not any('[data-testid="confirm-dialog"]' in lookup for lookup in lookups):
        found.add("dialog not found with document.querySelector")
    if re.search(r"\$el\s*\.\s*(?:querySelector|querySelectorAll|closest)\s*\(", body):
        found.add("lookup inside the component's element")
    return sorted(found)


_STYLE_ACCESS = re.compile(r"\.\s*style\b")
_FLEX_GROW_WRITE = re.compile(r"\.\s*style\s*\.\s*flexGrow\s*=(?!=)")


def style_violations(source: str) -> list[str]:
    """CSP style-src 'self': the one style write is the fleet bar's el.style.flexGrow."""
    code = _mask_js(source)
    found: set[str] = set()
    for match in _STYLE_ACCESS.finditer(code):
        if _FLEX_GROW_WRITE.match(code, match.start()) is None:
            found.add("style access other than style.flexGrow =")
    if re.search(r"setAttribute\s*\(\s*([\"'])style\1", source):
        found.add("style attribute")
    if re.search(r"\b(?:cssText|insertRule|adoptedStyleSheets|CSSStyleSheet)\b", code):
        found.add("stylesheet or cssText write")
    if re.search(r"createElement\s*\(\s*([\"'])style\1", source):
        found.add("style element")
    return sorted(found)


def poll_schedule(source: str) -> dict[str, object]:
    """The poll component's interval, backoff steps and failure threshold, from its text."""
    body = component_bodies(source).get("poll", "")
    return {
        "interval": int(js_var(body, "POLL_INTERVAL_MS")),
        "backoff": [int(n) for n in re.findall(r"\d+", js_var(body, "POLL_BACKOFF_MS"))],
        "pause_after": int(js_var(body, "POLL_PAUSE_AFTER")),
    }


def missing_hooks(body: str, hooks: tuple[str, ...]) -> list[str]:
    """The hooks of the binding contract that a component's text never names."""
    return [hook for hook in hooks if hook not in body]


def cookie_writes(source: str) -> list[str]:
    """The right-hand side of every ``document.cookie = ...`` statement."""
    code = _mask_js(source)
    writes = []
    for match in _COOKIE_WRITE.finditer(code):
        end = code.find(";", match.end())
        writes.append(source[match.end() : len(source) if end < 0 else end])
    return writes


def theme_cookie_violations(source: str, server: Mapping[str, str]) -> list[str]:
    """How the theme cookie admin.js writes differs from the ``server``'s attributes.

    ``server`` maps the lower-case attribute names path, max-age and samesite to the values
    ThemeView sets. The JS write must set exactly those, name the cookie theme, and add
    Secure only on an https page (the server sets it exactly in production, behind TLS).
    """
    writes = cookie_writes(source)
    if len(writes) != 1:
        return ["not exactly one cookie write"]
    expression = writes[0]
    secure = _SECURE_ON_HTTPS.search(expression)
    if secure is not None:
        expression = expression[: secure.start()] + expression[secure.end() :]
    text = "".join(a or b for a, b in _JS_STRING.findall(expression))
    found: set[str] = set()
    if not text.startswith(THEME_COOKIE + "="):
        found.add("cookie other than theme")
    attributes: dict[str, str] = {}
    for part in text.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attributes[name.lower()] = value
    for name, value in server.items():
        if attributes.get(name) != value:
            found.add(f"{name} differs from the server")
    if "secure" in attributes:
        found.add("Secure without the https condition")
    if set(attributes) - set(server) - {"secure"}:
        found.add("attribute the server does not set")
    if secure is None:
        found.add("no Secure on https")
    return sorted(found)


def server_theme_cookie() -> dict[str, str]:
    """Path, Max-Age and SameSite of the cookie ThemeView sets for a valid POST."""
    response = ThemeView.as_view()(RequestFactory().post("/theme/", {"theme": "dark"}))
    morsel = response.cookies[THEME_COOKIE]
    return {
        "path": morsel["path"],
        "max-age": str(morsel["max-age"]),
        "samesite": morsel["samesite"],
    }


def js_var(source: str, name: str) -> str:
    """The initializer text of ``var <name> = ...;`` in admin.js."""
    match = re.search(rf"\bvar\s+{name}\s*=\s*(?P<value>[^;]*);", source)
    assert match is not None, name
    return match.group("value").strip()


def js_object(source: str, name: str) -> dict[str, int | str]:
    """A flat ``var <name> = {key: 60, key: "text"}`` literal as a dict."""
    body = js_var(source, name)
    assert body.startswith("{") and body.endswith("}"), name
    pairs = re.findall(r"([\w$]+)\s*:\s*(\"[^\"]*\"|\d+)", body)
    return {key: int(value) if value.isdigit() else value[1:-1] for key, value in pairs}


def relative_mismatches(source: str) -> list[str]:
    """Ages at which admin.js's floors and units disagree with powermon's timefmt.

    The JS constants (MS_PER_SECOND, AGE_FLOORS, JUST_NOW, UNIT_AGO, UNIT_WORDS,
    UNIT_COMPACT) drive a port of admin.js's ageParts; its texts must equal timefmt's
    relative_text, compact_age and age_words at every floor boundary.
    """
    ms_per_second = int(js_var(source, "MS_PER_SECOND"))
    floors = js_object(source, "AGE_FLOORS")
    just_now = js_var(source, "JUST_NOW").strip("\"'")
    ago, words, compact = (js_object(source, n) for n in ("UNIT_AGO", "UNIT_WORDS", "UNIT_COMPACT"))
    minute, hour, limit, day = (int(floors[k]) for k in ("minute", "hour", "hoursLimit", "day"))

    def js_age(age: timedelta) -> tuple[int, str]:
        seconds = math.floor(age / timedelta(milliseconds=1) / ms_per_second)
        if seconds < 1:
            return 0, "s"
        if seconds < minute:
            return seconds, "s"
        if seconds < hour:
            return seconds // minute, "min"
        if seconds < limit:
            return seconds // hour, "h"
        return seconds // day, "d"

    now = datetime(2026, 10, 25, 3, 30, tzinfo=UTC)
    found = []
    for seconds in (-90, 0, 0.5, 0.999, 1, 59, 59.999, 60, 3599, 3600, 172_799, 172_800, 10**7):
        value = now - timedelta(seconds=seconds)
        count, unit = js_age(now - value)
        relative = just_now if (count, unit) == (0, "s") else f"{count}{ago[unit]}"
        texts = (relative, f"{count}{compact[unit]}", f"{count}{words[unit]}")
        expected = (
            timefmt.relative_text(value, now),
            timefmt.compact_age(value, now),
            timefmt.age_words(value, now),
        )
        if texts != expected:
            found.append(f"{seconds} s: {texts} != {expected}")
    return found


# Hooks of the binding contract each component must name (a pin against renames; 06-20
# cross-checks the x-data names against the templates).
CONTRACT_HOOKS: dict[str, tuple[str, ...]] = {
    "theme": ('button[name="theme"]', "aria-pressed", '"data-theme"', "preventDefault"),
    "sidebar": (
        'getElementById("sidebar")',
        '[data-testid="sidebar-toggle"]',
        '[data-testid="drawer-close"]',
        '[data-testid="rail-toggle"]',
        "[data-drawer-overlay]",
        "[data-shell-body]",
        '[data-testid="skip-link"]',
        '"data-drawer"',
        '"data-rail"',
        '"inert"',
        'matchMedia("(min-width: 64rem)")',
        '"powermon.sidebar.rail"',
        "Open navigation",
        "Close navigation",
        "Collapse sidebar",
        "Expand sidebar",
        "[data-label]",
        "toggleDrawer",
        "closeDrawer",
        "toggleRail",
    ),
    "relative": (
        '"data-now"',
        "[data-relative]",
        'a[data-testid="sidebar-location"]',
        "[data-live-age]",
        '"pm:status"',
        "RELATIVE_REFRESH_MS",
    ),
    "poll": (
        "dataset.pollUrl",
        "dataset.pollPage",
        "dataset.reloadUrl",
        '[data-testid="live-status"]',
        '"data-live-state"',
        '[data-testid="live-chip"]',
        "[data-chip]",
        '"Live"',
        '"Paused"',
        "[data-live][data-location-id]",
        'a[data-testid="sidebar-location"]',
        '[data-live="sidebar-fail"]',
        '[data-delivery-variant="failing"] [data-label]',
        "[data-since-label]",
        '[data-fh="waiting"]',
        '[data-fh="received"]',
        '[data-live="summary"]',
        '[data-live="summary-sr"]',
        '[data-live="sidebar-count"]',
        '[data-live="count"]',
        "[data-count-value]",
        "[data-count-noun]",
        "[data-power]",
        '"data-power"',
        '[data-testid="fleet-tile"]',
        '[data-testid="fleet-count"]',
        '[data-testid="fleet-bar"]',
        '"opaqueredirect"',
        '"visibilitychange"',
        '"pm:status"',
    ),
    "copy": (
        '"data-copy-target"',
        '"data-copied-msg"',
        "[data-copy-status]",
        '"data-copied"',
        '"Copied"',
        '"Copy failed. Select the text and copy it by hand."',
        "navigator.clipboard.writeText",
    ),
    "tabs": (
        '[role="tablist"][data-testid="example-tabs"]',
        '[role="tab"]',
        '"aria-controls"',
        '"aria-selected"',
        '"tabindex"',
        '"ArrowLeft"',
        '"ArrowRight"',
        '"Home"',
        '"End"',
    ),
    "revealGuard": (
        '"pagehide"',
        "onPageshow(",
        "persisted",
        "location.replace(",
        "dataset.maskedUrl",
        '"device-key"',
        '"example-curl"',
        '"example-cron"',
        '"example-wget-gnu"',
        '"example-wget-busybox"',
    ),
    "chartImage": ('"error"', '[data-testid="weekly-chart-error"]', "naturalWidth"),
    "sectionNav": (
        'a[href^="#"]',
        "(prefers-reduced-motion: reduce)",
        "scrollIntoView",
        '"tabindex"',
        "preventScroll",
    ),
    "confirmDialog": (
        "a[data-confirm]",
        '[data-testid="confirm-dialog"]',
        "[data-dialog-body]",
        "[data-dialog-loading]",
        "[data-dialog-close]",
        '[data-testid="keep"]',
        '[data-testid="confirm-submit"]',
        '"aria-busy"',
        '"cancel"',
        '"Escape"',
        '"close"',
        "[popover]",
        "popovertarget",
        "preventDefault",
        '"[popover] a[href]"',
        '"a[href]"',
        "samePageJump(",
        "hidePopover",
    ),
    "fleetFilter": (
        'button[data-testid="filter-chip"][data-filter]',
        '[data-testid="fleet-bar"]',
        "style.flexGrow",
        'tr[data-testid="location-row"]',
        "li[data-status][data-delivery]",
        '[data-testid="fleet-showing"]',
        '"data-total"',
        '[data-testid="no-match"]',
        "[data-filter-reset]",
        '"data-filtered"',
        '"aria-pressed"',
        '"pm:status"',
        '"Showing all "',
        '"Showing 1 location"',
        "[data-showing-all]",
        "[data-showing-shown]",
        "[data-showing-of]",
        "[data-showing-total]",
        "[data-showing-noun]",
    ),
    "offAfterHint": (
        '"id_period_s"',
        '"id_grace_s"',
        "[data-off-after-value]",
        "[data-off-after-fallback]",
        "OFF_AFTER_MIN",
        "OFF_AFTER_MAX",
        '"input"',
    ),
    "errorSummary": (".focus(",),
    "throttleCountdown": (
        "[data-retry-after]",
        '[data-testid="throttle-countdown"]',
        'button[type="submit"]',
        '"aria-disabled"',
        '"(you can try again now)"',
        '" left)"',
    ),
}


def test_admin_js_theme_cookie_matches_server() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    server = server_theme_cookie()

    # Expected: the server sets Path=/, Max-Age=THEME_MAX_AGE and SameSite=Lax, and the one
    # cookie admin.js writes carries exactly those, plus Secure only on https (UI-02).
    assert server == {"path": "/", "max-age": str(THEME_MAX_AGE), "samesite": "Lax"}
    assert theme_cookie_violations(source, server) == []
    assert [w for w in cookie_writes(source) if "Max-Age=31536000" in w] != []
    # The theme component only writes values of the server's allowlist.
    assert set(re.findall(r"\"(\w+)\"", js_var(source, "THEMES"))) == THEMES


@pytest.mark.parametrize(
    ("write", "expected"),
    [
        # Edge: Secure under the https test, with or without window.
        pytest.param(
            '"theme=" + v + "; Path=/; Max-Age=31536000; SameSite=Lax" + '
            '(location.protocol === "https:" ? "; Secure" : "")',
            [],
            id="no-window",
        ),
        # Failure: every attribute that drifts from the server.
        pytest.param(
            '"theme=" + v + "; Path=/; Max-Age=0; SameSite=Lax" + '
            '(window.location.protocol === "https:" ? "; Secure" : "")',
            ["max-age differs from the server"],
            id="max-age-zero",
        ),
        pytest.param(
            '"theme=" + v + "; Path=/; Max-Age=31536000" + '
            '(window.location.protocol === "https:" ? "; Secure" : "")',
            ["samesite differs from the server"],
            id="no-samesite",
        ),
        pytest.param(
            '"theme=" + v + "; Path=/locations/; Max-Age=31536000; SameSite=Strict" + '
            '(window.location.protocol === "https:" ? "; Secure" : "")',
            ["path differs from the server", "samesite differs from the server"],
            id="path-and-samesite",
        ),
        pytest.param(
            '"theme=" + v + "; Path=/; Max-Age=31536000; SameSite=Lax; Secure"',
            ["Secure without the https condition", "no Secure on https"],
            id="secure-always",
        ),
        pytest.param(
            '"theme=" + v + "; Path=/; Max-Age=31536000; SameSite=Lax"',
            ["no Secure on https"],
            id="never-secure",
        ),
        pytest.param(
            '"theme=" + v + "; Path=/; Max-Age=31536000; SameSite=Lax; Domain=example" + '
            '(window.location.protocol === "https:" ? "; Secure" : "")',
            ["attribute the server does not set"],
            id="domain",
        ),
        pytest.param(
            '"mode=" + v + "; Path=/; Max-Age=31536000; SameSite=Lax" + '
            '(window.location.protocol === "https:" ? "; Secure" : "")',
            ["cookie other than theme"],
            id="other-name",
        ),
    ],
)
def test_theme_cookie_rule(write: str, expected: list[str]) -> None:
    server = {"path": "/", "max-age": "31536000", "samesite": "Lax"}
    sample = "function choose(v) {\n  document.cookie = " + write + ";\n}\n"

    assert theme_cookie_violations(sample, server) == expected
    # Failure: no write, or two writes.
    assert theme_cookie_violations("var x = 1;", server) == ["not exactly one cookie write"]
    assert theme_cookie_violations(sample + sample, server) == ["not exactly one cookie write"]


def test_admin_js_all_names_registered() -> None:
    names = registered_names(ADMIN_JS.read_text(encoding="utf-8"))

    # Expected: exactly the 15 names of the binding contract, each registered once (06-20
    # checks them against the templates' x-data values).
    assert sorted(names) == sorted(CONTRACT_NAMES)
    # Edge: a repeat is seen; a registration inside a comment or a string is not one.
    assert Counter(registered_names('Alpine.data("theme", a);\nAlpine.data("theme", b);')) == {
        "theme": 2
    }
    assert registered_names('// Alpine.data("x", f)\nvar s = \'Alpine.data("y"\';') == []


def test_admin_js_relative_floors_match_timefmt() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: the JS floors are timefmt's, in seconds, with 1000 ms to the second.
    assert int(js_var(source, "MS_PER_SECOND")) == timefmt.SECOND // timedelta(milliseconds=1)
    assert js_object(source, "AGE_FLOORS") == {
        "minute": timefmt.MINUTE // timefmt.SECOND,
        "hour": timefmt.HOUR // timefmt.SECOND,
        "hoursLimit": timefmt.HOURS_LIMIT // timefmt.SECOND,
        "day": timefmt.DAY // timefmt.SECOND,
    }
    assert js_object(source, "UNIT_COMPACT") == timefmt.COMPACT_UNITS
    for text in ('"just now"', '" s ago"', '" min ago"', '" h ago"', '" d ago"'):
        assert text in source, text
    # Edge: every floor boundary, a future instant and a sub-second age agree.
    assert relative_mismatches(source) == []
    # Failure: a drifted floor or unit is caught.
    assert relative_mismatches(source.replace("hour: 3600", "hour: 3601")) != []
    assert relative_mismatches(source.replace('min: " min ago"', 'min: " m ago"')) != []


# An x-data attribute and its quoted value; the CSP build takes a registered name only.
_X_DATA = re.compile(r"(?<![\w:@.-])x-data\s*=\s*(\"[^\"]*\"|'[^']*')")
_COMPONENT_NAME = re.compile(r"[A-Za-z_$][\w$]*")


def template_components(text: str) -> list[str]:
    """Every x-data value of a template, in order (template comments skipped)."""
    return [m.group(1)[1:-1] for m in _X_DATA.finditer(_TEMPLATE_COMMENT.sub("", text))]


def alpine_name_gaps(templates: Mapping[str, str], source: str) -> list[str]:
    """Where the templates' x-data values and admin.js's Alpine.data names disagree: a
    value that is not a bare name, a name nobody registered, a registered name nobody binds."""
    registered = set(registered_names(source))
    used: set[str] = set()
    gaps: list[str] = []
    for relpath, text in sorted(templates.items()):
        for name in template_components(text):
            used.add(name)
            if not _COMPONENT_NAME.fullmatch(name):
                gaps.append(f"{relpath}: x-data {name!r} is not a component name")
            elif name not in registered:
                gaps.append(f"{relpath}: x-data {name!r} is not registered")
    gaps += [f"Alpine.data {name!r} is bound by no template" for name in sorted(registered - used)]
    return gaps


def test_alpine_names_cross_check() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    templates = {
        path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
        for path in template_files()
    }
    used = {name for text in templates.values() for name in template_components(text)}

    # Expected: every x-data value names a registered component and every registered
    # component is bound by some template (FRONTEND-STACK §3; the render matrix checks the
    # set each page binds). Server values never enter a directive.
    assert alpine_name_gaps(templates, source) == []
    assert used == set(registered_names(source)) == CONTRACT_NAMES
    assert [name for name, text in templates.items() if _django_in_directive(text)] == []
    # Failure: a template binding an unregistered name, and a registered name nobody binds.
    sample = {**templates, "web/sample.html": '<div x-data="ghost" hidden></div>'}
    assert alpine_name_gaps(sample, source) == ["web/sample.html: x-data 'ghost' is not registered"]
    orphan = source + '\nwindow.Alpine.data("orphan", function () { return {}; });\n'
    assert alpine_name_gaps(templates, orphan) == ["Alpine.data 'orphan' is bound by no template"]
    # Edge: a binding inside a template comment is none; a value that is not a bare
    # component name (an object literal or a call) is refused.
    commented = '{# <div x-data="ghost"> #}{% comment %}<p x-data="g2"></p>{% endcomment %}'
    assert template_components(commented) == []
    called = {**templates, "web/called.html": '<div x-data="copy()"></div>'}
    assert alpine_name_gaps(called, source) == [
        "web/called.html: x-data 'copy()' is not a component name"
    ]


@pytest.mark.parametrize("name", sorted(CONTRACT_HOOKS))
def test_admin_js_components_name_their_contract_hooks(name: str) -> None:
    bodies = component_bodies(ADMIN_JS.read_text(encoding="utf-8"))

    # Expected: the component exists and names every hook of its contract row.
    assert missing_hooks(bodies.get(name, ""), CONTRACT_HOOKS[name]) == []
    # Failure: a body without the hooks reports every one of them.
    stub = 'Alpine.data("' + name + '", function () { return {}; })'
    assert missing_hooks(component_bodies(stub)[name], CONTRACT_HOOKS[name]) == list(
        CONTRACT_HOOKS[name]
    )


FETCH_URL_RULE = "fetch URL not from a data-* value or a link's href"
NO_MANUAL = 'fetch without redirect: "manual"'


def test_admin_js_fetch_rules() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: every fetch passes the rules, and the poll fetches its data-poll-url.
    assert fetch_violations(source) == []
    assert "main.dataset.pollUrl" in [url for url, _ in fetch_calls(source)]


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param('fetch(main.dataset.pollUrl, { redirect: "manual" });', [], id="dataset"),
        pytest.param(
            "window.fetch(link.href, {redirect: 'manual', method: \"GET\", "
            'headers: {"X-PM-Fragment": "1"}});',
            [],
            id="href-get",
        ),
        # Edge: a fetch in a comment or a string is not code.
        pytest.param(
            '// fetch("/x")\nvar s = "fetch(1)";\n'
            'fetch(main.dataset.pollUrl, {redirect: "manual"});',
            [],
            id="comment",
        ),
        # Failure: a literal or built URL, a followed redirect, a write, an alias.
        pytest.param(
            'fetch("/locations/status.json", { redirect: "manual" });',
            [FETCH_URL_RULE],
            id="literal",
        ),
        pytest.param(
            'fetch(main.dataset.pollUrl + "?all=1", { redirect: "manual" });',
            [FETCH_URL_RULE],
            id="built",
        ),
        pytest.param("fetch(main.dataset.pollUrl);", [NO_MANUAL], id="no-options"),
        pytest.param(
            'fetch(main.dataset.pollUrl, { redirect: "follow" });', [NO_MANUAL], id="follow"
        ),
        pytest.param(
            'fetch(link.href, { redirect: "manual", method: "POST" });',
            ["fetch method other than GET"],
            id="post",
        ),
        pytest.param(
            'fetch(link.href, { redirect: "manual", body: data });',
            ["fetch with a body"],
            id="body",
        ),
        pytest.param(
            "var get = fetch; get(link.href);", ["fetch used other than as a call"], id="alias"
        ),
    ],
)
def test_fetch_rule(sample: str, expected: list[str]) -> None:
    assert fetch_violations(sample) == expected


def test_admin_js_poll_schedule() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    body = component_bodies(source).get("poll", "")
    expected = {
        "interval": 30_000,
        "backoff": [60_000, 120_000, 240_000, 300_000],
        "pause_after": 3,
    }

    # Expected: 30 s while visible; 60 -> 120 -> 240 -> 300 s after failures; paused after
    # 3 failures in a row; polls at once on becoming visible; stops on an opaque redirect.
    assert poll_schedule(source) == expected
    for hook in ('"visibilitychange"', "document.visibilityState", '"opaqueredirect"'):
        assert hook in body, hook
    # Failure: a drifted constant is seen.
    drifted = source.replace("POLL_INTERVAL_MS = 30000", "POLL_INTERVAL_MS = 5000")
    assert poll_schedule(drifted) != expected


def test_admin_js_copy_uses_click_only() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: one clipboard write, inside the copy component's click listener; no read.
    assert clipboard_violations(source) == []
    assert len(list(_code_matches(_CLIPBOARD_WRITE, source))) == 1


COPY_OK = (
    'Alpine.data("copy", function () { return { init: function () {'
    ' b.addEventListener("click", function () { navigator.clipboard.writeText(t.textContent); });'
    " } }; });"
)
OUTSIDE_CLICK = "clipboard write outside the copy component's click listener"


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param(COPY_OK, [], id="click"),
        # Failure: a write on init (an automatic copy of a revealed key), a write in another
        # component, any read, execCommand, no write at all.
        pytest.param(
            'Alpine.data("copy", function () { return { init: function () {'
            " navigator.clipboard.writeText(t.textContent); } }; });",
            [OUTSIDE_CLICK],
            id="on-init",
        ),
        pytest.param(
            COPY_OK.replace('"copy"', '"revealGuard"'), [OUTSIDE_CLICK], id="no-copy-component"
        ),
        pytest.param(
            COPY_OK + 'Alpine.data("revealGuard", function () { return { init: function () {'
            ' b.addEventListener("click", function () { navigator.clipboard.writeText(k); });'
            " } }; });",
            [OUTSIDE_CLICK],
            id="other-component",
        ),
        pytest.param(
            COPY_OK + " navigator.clipboard.readText();",
            ["clipboard read or execCommand"],
            id="read",
        ),
        pytest.param(
            COPY_OK + ' document.execCommand("copy");',
            ["clipboard read or execCommand"],
            id="exec-command",
        ),
        pytest.param("var x = 1;", ["no clipboard write"], id="none"),
    ],
)
def test_clipboard_rule(sample: str, expected: list[str]) -> None:
    assert clipboard_violations(sample) == expected


def test_admin_js_reveal_guard() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    body = component_bodies(source).get("revealGuard", "")
    replaced = re.findall(r"\blocation\s*\.\s*replace\s*\(\s*([^)]*)\)", body)

    # Expected: pagehide empties the key and the four examples; pageshow with persisted
    # replaces the page with the masked setup URL from data-masked-url (R4).
    assert missing_hooks(body, CONTRACT_HOOKS["revealGuard"]) == []
    assert re.search(r"\.textContent\s*=\s*\"\"", body) is not None
    assert replaced != [] and all("maskedUrl" in argument for argument in replaced)
    # Edge: admin.js keeps one window pageshow listener (06-07's reset), which also runs
    # the handlers components register through onPageshow.
    listener = re.search(r"window\.addEventListener\(\"pageshow\", function \(event\) \{", source)
    assert listener is not None
    assert "pageshowHandlers.forEach" in source[listener.end() :]
    # Failure: a guard that never checks persisted, or replaces with another URL, is caught.
    assert missing_hooks(body.replace("persisted", "loaded"), ("persisted",)) == ["persisted"]


GOOD_CONFIRM = """window.Alpine.data("confirmDialog", function () {
  return { init: function () {
    var dialog = document.querySelector('dialog[data-testid="confirm-dialog"]');
    document.addEventListener("click", function (event) {
      var link = event.target.closest("a[data-confirm]");
      dialog.showModal();
      fetch(link.href, { redirect: "manual", headers: { "X-PM-Fragment": "1" } })
        .then(function (response) {
          if (response.type === "opaqueredirect" || response.status !== 200 ||
              response.headers.get("X-PM-Fragment") !== "1") { throw new Error("full"); }
          return response.text();
        })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var roots = doc.querySelectorAll('[data-testid="confirm"]');
          if (roots.length !== 1) { throw new Error("full"); }
          body.appendChild(roots[0]);
        })
        .catch(function () { window.location.assign(link.href); });
    });
  } };
});
"""


def test_admin_js_fragment_protocol() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: one fragment fetch, every injection condition checked, every other outcome
    # a full navigation; the header is named nowhere else (UI-07, T-06-32).
    assert fragment_violations(source) == []
    assert "link.href" in [url for url, _ in fetch_calls(source)]
    assert fragment_violations(GOOD_CONFIRM) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param(
            "|| response.status !== 200 ", "", ["no status 200 check"], id="no-status-check"
        ),
        pytest.param(
            '||\n              response.headers.get("X-PM-Fragment") !== "1"',
            "",
            ["no response header check"],
            id="no-header-check",
        ),
        pytest.param(
            'if (roots.length !== 1) { throw new Error("full"); }',
            "",
            ["no single-root check"],
            id="no-root-count",
        ),
        pytest.param(
            ".catch(function () { window.location.assign(link.href); });",
            ";",
            ["no location.assign fallback"],
            id="no-fallback",
        ),
        pytest.param(
            ', headers: { "X-PM-Fragment": "1" }',
            "",
            ["no single fetch sending X-PM-Fragment: 1"],
            id="no-request-header",
        ),
        pytest.param(
            'var doc = new DOMParser().parseFromString(html, "text/html");',
            "var doc = document.implementation.createHTMLDocument(); doc.body.textContent = html;",
            ["no DOMParser"],
            id="no-domparser",
        ),
    ],
)
def test_fragment_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_CONFIRM
    assert fragment_violations(GOOD_CONFIRM.replace(old, new)) == expected


def test_fragment_rule_flags_the_header_elsewhere_and_innerhtml() -> None:
    poll = 'Alpine.data("poll", function () { var h = { "X-PM-Fragment": "1" }; });\n'
    injected = GOOD_CONFIRM.replace("body.appendChild(roots[0]);", "body.innerHTML = html;")

    # Failure: the fragment header in another component; markup injected as HTML.
    assert fragment_violations(GOOD_CONFIRM + poll) == ["X-PM-Fragment outside confirmDialog"]
    assert "innerHTML" in violations(injected, ADMIN_JS_RULES)


def test_admin_js_no_confirm_call() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    dialogs = {name: ADMIN_JS_RULES[name] for name in ("confirm(", "alert(", "prompt(")}

    # Expected: the confirmation is the server's page in a native dialog, never confirm().
    assert violations(source, dialogs) == []
    assert ".showModal(" in component_bodies(source).get("confirmDialog", "")
    # Failure: a browser dialog in any form is caught (06-07's rule).
    assert violations("if (window.confirm('Delete?')) { go(); }", dialogs) == ["confirm("]
    assert violations("self.alert(1);", dialogs) == ["alert("]


def test_admin_js_confirm_dialog_is_document_wide() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: one click listener on document (the kebab entries are outside the scope
    # wrapper) and the dialog found by its testid on document (it renders after main).
    assert confirm_scope_violations(source) == []
    assert confirm_scope_violations(GOOD_CONFIRM) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param(
            'document.addEventListener("click"',
            'this.$el.addEventListener("click"',
            [
                "a[data-confirm] click listener not on document",
                "click listener on the component's element",
            ],
            id="listener-on-el",
        ),
        pytest.param(
            "document.querySelector('dialog[data-testid=\"confirm-dialog\"]')",
            "this.$el.querySelector('dialog[data-testid=\"confirm-dialog\"]')",
            [
                "dialog not found with document.querySelector",
                "lookup inside the component's element",
            ],
            id="dialog-in-el",
        ),
    ],
)
def test_confirm_scope_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_CONFIRM
    assert confirm_scope_violations(GOOD_CONFIRM.replace(old, new)) == expected


def test_admin_js_fleet_bar_style_only_flexgrow() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    body = component_bodies(source).get("fleetFilter", "")

    # Expected: the only style write is the fleet bar's flexGrow, inside fleetFilter.
    assert style_violations(source) == []
    assert len(_FLEX_GROW_WRITE.findall(_mask_js(source))) == 1
    assert _FLEX_GROW_WRITE.search(_mask_js(body)) is not None


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param("segment.style.flexGrow = String(count);", [], id="flex-grow"),
        # Edge: style in a comment or a string is not code.
        pytest.param('// el.style.color\nvar s = "x.style.color = 1";', [], id="comment"),
        # Failure: any other style write.
        pytest.param(
            'el.style.color = "red";', ["style access other than style.flexGrow ="], id="color"
        ),
        pytest.param(
            'el.setAttribute("style", "color: red");', ["style attribute"], id="attribute"
        ),
        pytest.param(
            'el.style.cssText = "";',
            ["style access other than style.flexGrow =", "stylesheet or cssText write"],
            id="css-text",
        ),
        pytest.param(
            'el.style.setProperty("--w", "1");',
            ["style access other than style.flexGrow ="],
            id="set-property",
        ),
        pytest.param(
            'document.head.appendChild(document.createElement("style"));',
            ["style element"],
            id="style-element",
        ),
    ],
)
def test_style_rule(sample: str, expected: list[str]) -> None:
    assert style_violations(sample) == expected


def test_admin_js_off_after_bounds_match_the_model() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    body = component_bodies(source).get("offAfterHint", "")

    # Expected: the hint accepts exactly the whole numbers the form accepts (N8).
    assert int(js_var(body, "OFF_AFTER_MIN")) == MIN_SECONDS
    assert int(js_var(body, "OFF_AFTER_MAX")) == MAX_SECONDS


def test_component_bodies_follow_the_brackets() -> None:
    source = (
        'window.Alpine.data("a", function () { var s = ")"; return { f: g(1) }; });\n'
        '// Alpine.data("c", nothing)\n'
        'window.Alpine.data("b", function () { return {}; });'
    )
    bodies = component_bodies(source)

    # Expected: each call up to its own closing parenthesis; a bracket in a string is text.
    assert bodies == {
        "a": 'Alpine.data("a", function () { var s = ")"; return { f: g(1) }; })',
        "b": 'Alpine.data("b", function () { return {}; })',
    }
    # Edge and failure: an unclosed call runs to the end; no registration gives nothing.
    assert component_bodies('Alpine.data("x", f(') == {"x": 'Alpine.data("x", f('}
    assert component_bodies("var x = 1;") == {}


def test_js_masking_keeps_offsets() -> None:
    source = "a = 'x{y}' + \"}\" + `{`; // { not a brace\n/* } */ try { b(); } catch (e) {}"
    masked = _mask_js(source)

    # Expected: same length and newlines; edge: braces in strings and comments are blanked.
    assert len(masked) == len(source) and masked.count("\n") == source.count("\n")
    assert masked.count("{") == 2 and masked.count("}") == 2
    assert _try_catch_blocks(source) == [(masked.index("{"), masked.index("}"))]
    # Failure: an unterminated string blanks to the end instead of raising.
    assert _mask_js("x = 'open {") == "x = '      "


# admin.js wave-4 audit pins (W4-A1, W4-A2, W4-A3 and the "Never" fill)


def function_body(source: str, name: str) -> str:
    """The text of the first ``function <name>(...) {...}`` declaration in code, or ""."""
    code = _mask_js(source)
    match = next(_code_matches(re.compile(rf"\bfunction\s+{name}\s*\("), source), None)
    if match is None:
        return ""
    end = _closing(code, code.index(")", match.start()) + 1, "{", "}")
    return source[match.start() : end + 1]


def text_writes(block: str, target: str) -> list[str]:
    """The right-hand side of every ``<target>.textContent = ...;`` in code, whitespace
    collapsed."""
    code = _mask_js(block)
    pattern = re.compile(rf"(?<![\w$.]){re.escape(target)}\s*\.\s*textContent\s*=(?!=)")
    writes = []
    for match in pattern.finditer(code):
        end = code.find(";", match.end())
        writes.append(" ".join(block[match.end() : len(block) if end < 0 else end].split()))
    return writes


# The drawer controls: the templates that render them and the one control with
# aria-controls="sidebar" whose aria-expanded reports the rail, not the drawer.
DRAWER_TEMPLATES = ("partials/sidebar.html", "layouts/app.html")
RAIL_TOGGLE = "rail-toggle"
_BUTTON_TAG = re.compile(r"<button\b[^>]*>")
_TESTID_ATTRIBUTE = re.compile(r"\bdata-testid=\"([\w-]+)\"")
_TESTID_LOOKUP = re.compile(
    r"([\w$]+)\s*=\s*[\w$]+\s*\.\s*querySelector\(\s*'\[data-testid=\"([\w-]+)\"\]'\s*\)"
)
_EXPANDED_FROM_OPEN = (
    r"(?<![\w$.]){control}\s*\.\s*setAttribute\(\s*\"aria-expanded\"\s*,"
    r"\s*open\s*\?\s*\"true\"\s*:\s*\"false\"\s*\)"
)


def drawer_expanded_controls() -> list[str]:
    """The testids of the buttons the shell templates render with aria-controls="sidebar"
    and aria-expanded, the rail toggle aside: the controls that report the drawer's state."""
    found = set()
    for name in DRAWER_TEMPLATES:
        for tag in _BUTTON_TAG.findall((TEMPLATES / name).read_text(encoding="utf-8")):
            testid = _TESTID_ATTRIBUTE.search(tag)
            if 'aria-controls="sidebar"' in tag and "aria-expanded=" in tag and testid:
                found.add(testid.group(1))
    return sorted(found - {RAIL_TOGGLE})


def drawer_synced_controls(source: str) -> list[str]:
    """The testids whose element the sidebar component's setOpen gives aria-expanded from
    the drawer state ("true" open, "false" closed), in code (not a comment)."""
    body = component_bodies(source).get("sidebar", "")
    set_open = function_body(body, "setOpen")
    controls = {variable: testid for variable, testid in _TESTID_LOOKUP.findall(body)}
    return sorted(
        testid
        for variable, testid in controls.items()
        if any(
            True
            for _ in _code_matches(
                re.compile(_EXPANDED_FROM_OPEN.format(control=re.escape(variable))), set_open
            )
        )
    )


def drawer_focus_violations(source: str) -> list[str]:
    """W4-A3: on open, setOpen focuses the first nav link; when the drawer refused it (still
    hidden at that instant), it tries once more on the next frame, only while still open."""
    code = _mask_js(function_body(component_bodies(source).get("sidebar", ""), "setOpen"))
    focus = re.compile(r"\bfirst\s*\.\s*focus\s*\(")
    found = set()
    if focus.search(code) is None:
        found.add("first nav link never focused")
    if re.search(r"\bdocument\s*\.\s*activeElement\s*!==?\s*first\b", code) is None:
        found.add("focus never checked")
    frames = [
        code[match.start() : _closing(code, match.start()) + 1]
        for match in re.finditer(r"\brequestAnimationFrame\s*\(", code)
    ]
    retries = [frame for frame in frames if focus.search(frame)]
    if not retries:
        found.add("no retry on the next frame")
    elif not all(re.search(r"\bif\s*\(\s*open\s*\)", frame) for frame in retries):
        found.add("retry while the drawer is closed")
    return sorted(found)


_COUNT_LOOP = re.compile(
    r"\bquerySelectorAll\(\s*'\[data-live=\"count\"\]'\s*\)\s*\.\s*forEach\s*\("
)
_COUNT_HOOK = re.compile(
    r"\bvar\s+([\w$]+)\s*=\s*[\w$]+\s*\.\s*querySelector\(\s*\"(\[data-count-(?:value|noun)\])\"\s*\)"
)
COUNT_VALUE = "[data-count-value]"
COUNT_NOUN = "[data-count-noun]"


def count_loop(source: str) -> str:
    """The poll's ``[data-live="count"]`` forEach call, or ""."""
    body = component_bodies(source).get("poll", "")
    code = _mask_js(body)
    match = next(_code_matches(_COUNT_LOOP, body), None)
    if match is None:
        return ""
    start = code.index("forEach", match.start())
    return body[start : _closing(code, start) + 1]


def count_write_violations(source: str) -> list[str]:
    """W4-A2 and the mono rule: a poll writes the S3 count's number, and nothing else, into
    its mono [data-count-value] span and the noun into [data-count-noun]; the whole text of
    the [data-live="count"] element is written only when it has no number span."""
    loop = count_loop(source)
    if not loop:
        return ["no count loop"]
    code = _mask_js(loop)
    parameter = re.match(r"forEach\s*\(\s*function\s*\(\s*([\w$]+)\s*\)", code)
    element = parameter.group(1) if parameter else "element"
    hooks = {hook: variable for variable, hook in _COUNT_HOOK.findall(loop)}
    value, noun = hooks.get(COUNT_VALUE), hooks.get(COUNT_NOUN)
    found = set()
    if value is None:
        found.add(f"no {COUNT_VALUE} hook")
    elif text_writes(loop, value) != ["String(total)"]:
        found.add("number span gets more than the number")
    if noun is None:
        found.add(f"no {COUNT_NOUN} hook")
    else:
        writes = text_writes(loop, noun)
        words = {a or b for write in writes for a, b in _JS_STRING.findall(write)}
        if words != {"location", "locations"} or any("+" in write for write in writes):
            found.add("noun span gets more than the noun")
    guards = []
    if value is not None:
        for match in re.finditer(rf"\bif\s*\(\s*!\s*{re.escape(value)}\s*\)\s*\{{", code):
            guards.append((match.end() - 1, _closing(code, match.end() - 1, "{", "}")))
    whole = re.compile(rf"(?<![\w$.]){re.escape(element)}\s*\.\s*textContent\s*=(?!=)")
    if any(not any(a < m.start() < b for a, b in guards) for m in whole.finditer(code)):
        found.add("whole text written over the number span")
    return sorted(found)


_NO_TIME_GUARD = re.compile(r"\bif\s*\(\s*!\s*time\s*\)\s*\{\s*return\s+false\s*;\s*\}")
_DOM_WRITE = re.compile(r"\.\s*textContent\s*=(?!=)|\.\s*setAttribute\s*\(")


def never_fill_violations(source: str) -> list[str]:
    """A wrapper rendered as "Never" has no <time>: fillInstant cannot show an instant there,
    so it returns false (the poll then shows the changed chip) before any write, and no
    element is marked to hold an instant."""
    body = function_body(source, "fillInstant")
    if not body:
        return ["no fillInstant"]
    code = _mask_js(body)
    no_instant = re.search(r"\bif\s*\(\s*!\s*instant\s*\)\s*\{", code)
    after = _closing(code, no_instant.end() - 1, "{", "}") + 1 if no_instant else 0
    guard = _NO_TIME_GUARD.search(code, after)
    writes = [match.start() for match in _DOM_WRITE.finditer(code, after)]
    found = set()
    if guard is None or (writes and writes[0] < guard.start()):
        found.add("instant written into a wrapper without <time>")
    if "data-instant-text" in source:
        found.add("Never holder marked to hold an instant")
    return sorted(found)


DRAWER_SAMPLE = """Alpine.data("sidebar", function () {
  function setOpen(value) {
    open = value;
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    // closer.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var first = aside.querySelector("nav a[href]");
      first.focus();
      if (document.activeElement !== first) {
        window.requestAnimationFrame(function () {
          if (open) {
            first.focus();
          }
        });
      }
    }
  }
  return { init: function () {
    toggle = shell.querySelector('[data-testid="sidebar-toggle"]');
    closer = shell.querySelector('[data-testid="drawer-close"]');
  } };
})
"""


def test_W4A1_both_drawer_controls_report_the_drawer_state() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    rendered = drawer_expanded_controls()

    # Expected: the hamburger and the drawer's close button carry aria-expanded for the
    # drawer, and setOpen writes it on both from the drawer state ("true" open, "false"
    # closed); the rail toggle reports the rail and is synced by syncRail.
    assert rendered == ["drawer-close", "sidebar-toggle"]
    assert drawer_synced_controls(source) == rendered
    # Edge: a write in a comment is not one.
    assert drawer_synced_controls(DRAWER_SAMPLE) == ["sidebar-toggle"]
    # Failure: the wave-4 setOpen, which synced only the hamburger, misses the close button.
    fixed = DRAWER_SAMPLE.replace("// closer.", "closer.")
    assert drawer_synced_controls(fixed) == ["drawer-close", "sidebar-toggle"]
    assert drawer_synced_controls(fixed.replace('open ? "true"', 'value ? "true"')) == []


def test_W4A3_drawer_focus_retries_once_on_the_next_frame() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    retry = (
        "      if (document.activeElement !== first) {\n"
        "        window.requestAnimationFrame(function () {\n"
        "          if (open) {\n"
        "            first.focus();\n"
        "          }\n"
        "        });\n"
        "      }\n"
    )
    assert retry in DRAWER_SAMPLE

    # Expected: the real setOpen and the sample focus the first link and retry once.
    assert drawer_focus_violations(source) == []
    assert drawer_focus_violations(DRAWER_SAMPLE) == []
    # Edge: a retry that ignores a drawer closed in between is caught.
    unguarded = DRAWER_SAMPLE.replace("if (open) {\n            first", "{\n            first")
    assert drawer_focus_violations(unguarded) == ["retry while the drawer is closed"]
    # Failure: the wave-4 setOpen focused once and never checked.
    assert drawer_focus_violations(DRAWER_SAMPLE.replace(retry, "")) == [
        "focus never checked",
        "no retry on the next frame",
    ]


GOOD_COUNT = """Alpine.data("poll", function () {
  function updateCounts(counts, total) {
    main.querySelectorAll('[data-live="count"]').forEach(function (element) {
      var value = element.querySelector("[data-count-value]");
      var noun = element.querySelector("[data-count-noun]");
      if (!value) {
        element.textContent = total === 1 ? "1 location" : total + " locations";
        return;
      }
      value.textContent = String(total);
      if (noun) {
        noun.textContent = total === 1 ? "location" : "locations";
      }
    });
  }
})
"""
WHOLE_OVER_SPAN = "whole text written over the number span"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Edge: a whole-text write in a comment is not one.
        pytest.param(
            "      value.textContent = String(total);\n",
            "      value.textContent = String(total);\n      // element.textContent = x;\n",
            [],
            id="comment",
        ),
        # Failure: the whole text written after the spans, the noun in the number span, a
        # number in the noun span, no spans at all (the wave-4 shape).
        pytest.param(
            "      if (noun) {",
            '      element.textContent = total + " locations";\n      if (noun) {',
            [WHOLE_OVER_SPAN],
            id="whole-after-spans",
        ),
        pytest.param(
            "value.textContent = String(total);",
            'value.textContent = total + " locations";',
            ["number span gets more than the number"],
            id="noun-in-number",
        ),
        pytest.param(
            'noun.textContent = total === 1 ? "location" : "locations";',
            'noun.textContent = total + " locations";',
            ["noun span gets more than the noun"],
            id="number-in-noun",
        ),
        pytest.param(
            '      var value = element.querySelector("[data-count-value]");\n'
            '      var noun = element.querySelector("[data-count-noun]");\n',
            "",
            [f"no {COUNT_NOUN} hook", f"no {COUNT_VALUE} hook", WHOLE_OVER_SPAN],
            id="no-spans",
        ),
    ],
)
def test_count_write_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_COUNT
    assert count_write_violations(GOOD_COUNT.replace(old, new)) == expected


def test_W4A2_poll_keeps_the_count_number_in_its_mono_span() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: S3's "{N} locations" keeps N in the mono [data-count-value] span and the
    # noun in [data-count-noun] after a poll; an element without the spans gets the whole
    # text as before.
    assert count_write_violations(source) == []
    assert count_loop(source) != ""
    # Failure: no poll component, or no count loop in it.
    assert count_write_violations("var x = 1;") == ["no count loop"]


GOOD_FILL = """function fillInstant(wrapper, instant) {
  var time = wrapper.querySelector("time");
  if (!instant) {
    if (time) {
      return false;
    }
    return true;
  }
  if (typeof instant.iso !== "string") {
    return false;
  }
  if (!time) {
    // A "Never" wrapper: nothing to fill in place.
    return false;
  }
  time.setAttribute("datetime", instant.iso);
  return true;
}
"""


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Failure: the wave-4 shape, which wrote the display text into the "Never" span and
        # marked it; a guard placed after the first write.
        pytest.param(
            '  if (!time) {\n    // A "Never" wrapper: nothing to fill in place.\n'
            "    return false;\n  }\n",
            '  if (!time) {\n    var holder = wrapper.querySelector("[data-instant-text]");\n'
            '    holder.setAttribute("data-instant-text", "");\n'
            "    holder.textContent = instant.display;\n    return true;\n  }\n",
            [
                "Never holder marked to hold an instant",
                "instant written into a wrapper without <time>",
            ],
            id="never-filled",
        ),
        pytest.param(
            '  time.setAttribute("datetime", instant.iso);\n',
            "",
            [],
            id="no-write-after",
        ),
        pytest.param(
            '  if (!time) {\n    // A "Never" wrapper: nothing to fill in place.\n'
            "    return false;\n  }\n",
            '  wrapper.setAttribute("data-x", "");\n  if (!time) {\n    return false;\n  }\n',
            ["instant written into a wrapper without <time>"],
            id="write-before-guard",
        ),
    ],
)
def test_never_fill_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_FILL
    assert never_fill_violations(GOOD_FILL.replace(old, new)) == expected


def test_never_time_shows_the_changed_chip_instead_of_a_half_time() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    poll = component_bodies(source).get("poll", "")

    # Expected: a wrapper rendered as "Never" (a waiting location's first heartbeat) is
    # not patched with a bare absolute time; fillInstant returns false and the poll turns
    # that into the changed chip, as for an id-set change.
    assert never_fill_violations(source) == []
    assert re.search(r"if\s*\(\s*!\s*fillInstant\s*\(", poll) is not None
    assert "changed = true" in poll
    # Failure: no fillInstant at all.
    assert never_fill_violations("var x = 1;") == ["no fillInstant"]


# admin.js wave-5 audit pins (W5-A1, W5-A2 and the sidebar count)


def call_texts(source: str, pattern: re.Pattern[str]) -> list[str]:
    """The text of every call in code whose start ``pattern`` matches, to its closing
    parenthesis."""
    code = _mask_js(source)
    return [
        source[match.start() : _closing(code, match.start()) + 1]
        for match in _code_matches(pattern, source)
    ]


def if_conditions(block: str) -> list[tuple[int, int]]:
    """(start, end) offsets of the condition of each ``if (...)`` in code."""
    code = _mask_js(block)
    return [(m.end(), _closing(code, m.start())) for m in re.finditer(r"\bif\s*\(", code)]


def if_blocks(block: str, condition: re.Pattern[str]) -> list[tuple[int, int]]:
    """(open brace, close brace) offsets of each ``if (...) {...}`` in code whose condition
    matches ``condition``, read in the raw text (string values count)."""
    code = _mask_js(block)
    found = []
    for start, end in if_conditions(block):
        opening = code.find("{", end)
        if opening >= 0 and condition.search(block, start, end):
            found.append((opening, _closing(code, opening, "{", "}")))
    return found


def inside(offset: int, blocks: list[tuple[int, int]]) -> bool:
    return any(start < offset < end for start, end in blocks)


_SAME_PAGE_JUMP = re.compile(r"\bsamePageJump\s*\(")
_MODIFIER_KEYS = ("metaKey", "ctrlKey", "shiftKey", "altKey")
_DIALOG_CLICK = re.compile(r"\bdialog\s*\.\s*addEventListener\s*\(\s*([\"'])click\1")
_PENDING_CALL = re.compile(r"\bpending\s*\(\s*\)")
_PREVENT = re.compile(r"\.\s*preventDefault\s*\(")
_CLOSE_CALL = re.compile(r"(?<![\w$])(?:dialog\s*\.\s*)?close\s*\(")
_RETURN_TO_CLEARED = re.compile(r"(?<![\w$.])returnTo\s*=\s*null\b")
_HIDE_POPOVER = re.compile(r"\.\s*hidePopover\s*\(")
_MENU_HIDE = re.compile(r"\bhideMenu\s*\(|\.\s*hidePopover\s*\(")


def jump_predicate_violations(source: str) -> list[str]:
    """W5-A1: samePageJump(event, link) holds only for a plain primary click (button 0, no
    modifier key) on a link with no other target and no download whose URL is this page's
    (origin, path and query) with a non-empty hash: the browser's own in-page jump."""
    body = function_body(source, "samePageJump")
    if not body:
        return ["no samePageJump"]
    code = _mask_js(body)
    found = set()
    plain = re.search(r"\bevent\s*\.\s*button\s*!==\s*0\b", code) is not None and all(
        re.search(rf"\bevent\s*\.\s*{key}\b", code) for key in _MODIFIER_KEYS
    )
    if not plain:
        found.add("not only a plain primary click")
    if (
        re.search(r"\blink\s*\.\s*target\s*!==\s*([\"'])_self\1", body) is None
        or re.search(r"\blink\s*\.\s*hasAttribute\(\s*([\"'])download\1\s*\)", body) is None
    ):
        found.add("a link to another target or a download")
    if re.search(r"\blink\s*\.\s*hash\s*!==\s*([\"'])\1", body) is None:
        found.add("an empty hash")
    if not all(
        re.search(rf"\blink\s*\.\s*{part}\s*===\s*window\s*\.\s*location\s*\.\s*{part}\b", code)
        for part in ("origin", "pathname", "search")
    ):
        found.add("a link to another page")
    return sorted(found)


def jump_branches(listener: str) -> list[str]:
    """The block of each ``if`` in a listener whose condition calls samePageJump."""
    return [listener[start : end + 1] for start, end in if_blocks(listener, _SAME_PAGE_JUMP)]


def dialog_jump_violations(source: str) -> list[str]:
    """W5-A1 (dialog): confirmDialog's dialog click listener has a samePageJump branch (S11's
    "Recent outages" on S5). It prevents the click only inside its ``if (pending())`` guard,
    and otherwise clears returnTo before it closes the dialog, so the close handler, which
    runs later, never sends the focus (and the scroll) back to the trigger."""
    body = component_bodies(source).get("confirmDialog", "")
    listeners = call_texts(body, _DIALOG_CLICK)
    branches = [branch for listener in listeners for branch in jump_branches(listener)]
    if not branches:
        return ["dialog stays open on a same-page link"]
    found = set()
    for branch in branches:
        code = _mask_js(branch)
        guards = if_blocks(branch, _PENDING_CALL)
        prevents = [match.start() for match in _PREVENT.finditer(code)]
        if not any(inside(offset, guards) for offset in prevents):
            found.add("jump runs while the submit is pending")
        if any(not inside(offset, guards) for offset in prevents):
            found.add("jump prevented while not pending")
        closes = [m.start() for m in _CLOSE_CALL.finditer(code) if not inside(m.start(), guards)]
        cleared = _RETURN_TO_CLEARED.search(code)
        if not closes:
            found.add("dialog stays open on a same-page link")
        elif cleared is None or cleared.start() > closes[0]:
            found.add("focus sent back to the trigger after the jump")
    return sorted(found)


def menu_jump_violations(source: str) -> list[str]:
    """W5-A1 (popover menu): confirmDialog's document click listener has a samePageJump
    branch for a link in a [popover] menu (the kebab's "Reset history…" while a reset is
    unavailable) that hides the menu and never prevents the click; every hidePopover call
    of the component sits in a try block with a catch (no Popover API: a plain list)."""
    body = component_bodies(source).get("confirmDialog", "")
    listeners = call_texts(body, _DOCUMENT_CLICK)
    branches = [branch for listener in listeners for branch in jump_branches(listener)]
    code = _mask_js(body)
    found = set()
    hiding = [branch for branch in branches if _MENU_HIDE.search(_mask_js(branch))]
    if not hiding or _HIDE_POPOVER.search(code) is None:
        found.add("menu stays open after a same-page link")
    if any(_PREVENT.search(_mask_js(branch)) for branch in branches):
        found.add("menu jump prevented")
    guarded = _try_catch_blocks(body)
    if any(not inside(match.start(), guarded) for match in _HIDE_POPOVER.finditer(code)):
        found.add("Popover API used without a guard")
    return sorted(found)


# The S5 overlays: the confirmation fragments the location page opens and its kebab menu.
S5_OVERLAYS = (
    "web/_confirm_delete.html",
    "web/_confirm_remove_outage.html",
    "web/_confirm_reset.html",
    "partials/_menu.html",
)
_HASH_REFERENCE = re.compile(r"add:\"#([\w-]+)\"|href=\"[^\"]*?#([\w-]+)")


def overlay_jump_ids() -> set[str]:
    """The ids the S5 overlays link to on the location page (template comments skipped)."""
    found: set[str] = set()
    for name in S5_OVERLAYS:
        text = _TEMPLATE_COMMENT.sub("", (TEMPLATES / name).read_text(encoding="utf-8"))
        found.update(a or b for a, b in _HASH_REFERENCE.findall(text))
    return found


def jump_target_gaps(ids: set[str], page: str) -> list[str]:
    """The ids that no tag of ``page`` carries together with tabindex="-1"."""
    gaps = []
    for element_id in sorted(ids):
        tag = re.search(rf"<[a-z]+\b[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", page)
        if tag is None or 'tabindex="-1"' not in tag.group(0):
            gaps.append(element_id)
    return gaps


DIALOG_JUMP = (
    "      } else if (jump && dialog.contains(jump) && samePageJump(event, jump)) {\n"
    "        if (pending()) {\n"
    "          event.preventDefault();\n"
    "          return;\n"
    "        }\n"
    "        returnTo = null;\n"
    "        close();\n"
    "        focusJumpTarget(jump);\n"
)
MENU_JUMP = (
    "      if (jump && samePageJump(event, jump)) {\n"
    '        hideMenu(jump.closest("[popover]"));\n'
    "        focusJumpTarget(jump);\n"
    "        return;\n"
    "      }\n"
)
MENU_HIDE_GUARD = (
    "      try {\n"
    '        if (popover.matches(":popover-open")) {\n'
    "          popover.hidePopover();\n"
    "        }\n"
    "      } catch (error) {\n"
    "        // No popover API: the menu is a plain list.\n"
    "      }\n"
)
JUMP_SAMPLE = (
    """function samePageJump(event, link) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return false;
  }
  if ((link.target && link.target !== "_self") || link.hasAttribute("download")) {
    return false;
  }
  return (
    link.hash !== "" &&
    link.origin === window.location.origin &&
    link.pathname === window.location.pathname &&
    link.search === window.location.search
  );
}
window.Alpine.data("confirmDialog", function () {
  return { init: function () {
    var hideMenu = function (popover) {
"""
    + MENU_HIDE_GUARD
    + """    };
    document.addEventListener("click", function (event) {
      var jump = closestTo(event, "[popover] a[href]");
"""
    + MENU_JUMP
    + """      var link = closestTo(event, "a[data-confirm]");
      event.preventDefault();
    });
    dialog.addEventListener("click", function (event) {
      var keep = closestTo(event, '[data-testid="keep"]');
      var jump = closestTo(event, "a[href]");
      if (keep && dialog.contains(keep)) {
        event.preventDefault();
        close();
"""
    + DIALOG_JUMP
    + """      } else if (pressOnBackdrop && outside(event)) {
        close();
      }
    });
  } };
});
"""
)
DIALOG_OPEN = "dialog stays open on a same-page link"
FOCUS_BACK = "focus sent back to the trigger after the jump"
JUMP_WHILE_PENDING = "jump runs while the submit is pending"
MENU_OPEN = "menu stays open after a same-page link"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Failure: the hash, the page, the plain click or the target is not checked.
        pytest.param('    link.hash !== "" &&\n', "", ["an empty hash"], id="empty-hash"),
        pytest.param(
            "    link.pathname === window.location.pathname &&\n",
            "",
            ["a link to another page"],
            id="other-page",
        ),
        pytest.param(" || event.metaKey", "", ["not only a plain primary click"], id="modifier"),
        pytest.param("event.button !== 0 || ", "", ["not only a plain primary click"], id="button"),
        pytest.param(
            ' || link.hasAttribute("download")',
            "",
            ["a link to another target or a download"],
            id="download",
        ),
    ],
)
def test_jump_predicate_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in JUMP_SAMPLE
    assert jump_predicate_violations(JUMP_SAMPLE.replace(old, new)) == expected


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Edge: a clear in a comment is not one.
        pytest.param(
            "        returnTo = null;\n",
            "        // returnTo = null;\n",
            [FOCUS_BACK],
            id="comment",
        ),
        # Failure: the wave-5 shape (no branch), the focus return cleared after the close or
        # never, the click always prevented, or never while pending, and no close at all.
        pytest.param(DIALOG_JUMP, "", [DIALOG_OPEN], id="no-branch"),
        pytest.param(
            "        returnTo = null;\n        close();\n",
            "        close();\n        returnTo = null;\n",
            [FOCUS_BACK],
            id="cleared-after-close",
        ),
        pytest.param("        returnTo = null;\n", "", [FOCUS_BACK], id="never-cleared"),
        pytest.param(
            "        if (pending()) {\n          event.preventDefault();\n          return;\n"
            "        }\n",
            "        event.preventDefault();\n",
            ["jump prevented while not pending", JUMP_WHILE_PENDING],
            id="always-prevented",
        ),
        pytest.param(
            "        if (pending()) {\n          event.preventDefault();\n          return;\n"
            "        }\n",
            "",
            [JUMP_WHILE_PENDING],
            id="no-pending-guard",
        ),
        pytest.param(
            "        close();\n        focusJumpTarget",
            "        focusJumpTarget",
            [DIALOG_OPEN],
            id="no-close",
        ),
    ],
)
def test_dialog_jump_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in JUMP_SAMPLE
    assert dialog_jump_violations(JUMP_SAMPLE.replace(old, new)) == expected


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Failure: the wave-5 shape (no branch), a branch that never hides the menu or
        # prevents the jump, and the Popover API called without its guard.
        pytest.param(MENU_JUMP, "", [MENU_OPEN], id="no-branch"),
        pytest.param(
            '        hideMenu(jump.closest("[popover]"));\n', "", [MENU_OPEN], id="no-hide"
        ),
        pytest.param(
            "        return;\n      }\n      var link",
            "        event.preventDefault();\n        return;\n      }\n      var link",
            ["menu jump prevented"],
            id="prevented",
        ),
        pytest.param(
            MENU_HIDE_GUARD,
            '      if (popover.matches(":popover-open")) {\n        popover.hidePopover();\n'
            "      }\n",
            ["Popover API used without a guard"],
            id="unguarded",
        ),
    ],
)
def test_menu_jump_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in JUMP_SAMPLE
    assert menu_jump_violations(JUMP_SAMPLE.replace(old, new)) == expected


def test_W5A1_same_page_links_close_their_overlay() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    body = component_bodies(source).get("confirmDialog", "")
    target = function_body(source, "focusJumpTarget")
    listeners = call_texts(body, _DIALOG_CLICK) + call_texts(body, _DOCUMENT_CLICK)
    branches = [branch for listener in listeners for branch in jump_branches(listener)]

    # Expected: one predicate decides a same-page jump. In the dialog (S11's "Recent
    # outages" on S5) the dialog closes without the focus return and the click is prevented
    # only while the submit is pending; in the kebab (the unavailable "Reset history…") the
    # open menu hides. Both let the browser jump, and the focus goes to the card or row.
    assert jump_predicate_violations(source) == []
    assert dialog_jump_violations(source) == []
    assert menu_jump_violations(source) == []
    assert len(branches) == 2 and all("focusJumpTarget(jump)" in branch for branch in branches)
    assert 'hasAttribute("tabindex")' in target and "preventScroll: true" in target
    # Failure: no predicate, no component.
    assert jump_predicate_violations("var x = 1;") == ["no samePageJump"]
    assert dialog_jump_violations("var x = 1;") == [DIALOG_OPEN]
    assert menu_jump_violations("var x = 1;") == [MENU_OPEN]


def test_W5A1_overlay_links_point_at_focusable_targets() -> None:
    detail = (TEMPLATES / "web" / "location_detail.html").read_text(encoding="utf-8")
    menu = _TEMPLATE_COMMENT.sub(
        "", (TEMPLATES / "partials" / "_menu.html").read_text(encoding="utf-8")
    )

    # Expected: on S5 the overlays link to the Recent outages card and the Reset history
    # row, both tabindex -1 targets of the location page, so the focus can follow the jump.
    assert overlay_jump_ids() == {"recent-outages", "reset-history"}
    assert jump_target_gaps(overlay_jump_ids(), detail) == []
    # Edge: the kebab's same-page entry sits inside its [popover] menu.
    assert -1 < menu.find("<div popover") < menu.find("#reset-history")
    # Failure: a target without tabindex, or missing from the page, is reported.
    page = '<section id="recent-outages" aria-labelledby="x">'
    assert jump_target_gaps({"recent-outages", "gone"}, page) == ["gone", "recent-outages"]


_POWER_LOOKUP = re.compile(
    r"\bvar\s+([\w$]+)\s*=\s*main\s*\.\s*querySelector\(\s*\"\[data-power\]\"\s*\)"
)
_ONE_ID = re.compile(r"\bids\s*\.\s*length\s*===\s*1\b")
_DETAIL_PAGE = re.compile(r"\bpage\s*===\s*\"detail\"")
_POWER_COMPARED = re.compile(
    r"\bbefore\s*\.\s*power\s*!==\s*undefined\s*&&"
    r"\s*entry\s*\.\s*power\s*!==\s*before\s*\.\s*power\b"
)
_POWER_VALID = re.compile(r"\bPOWER_KEYS\s*\.\s*indexOf\s*\(\s*entry\s*\.\s*power\s*\)\s*>=\s*0")


def power_change_violations(source: str) -> list[str]:
    """W5-A2: under maintenance S5 shows the stored power state only in its [data-power]
    pill (the since rows have no live hook). The poll's snapshot records that value for the
    page's one location id; apply shows the changed chip on the location page when the
    JSON's power differs, and an entry whose power is not in POWER_KEYS is invalid."""
    body = component_bodies(source).get("poll", "")
    snapshot = function_body(body, "snapshot")
    apply = function_body(body, "apply")
    found = set()
    lookup = next(_code_matches(_POWER_LOOKUP, snapshot), None)
    record = None
    if lookup is not None:
        name = re.escape(lookup.group(1))
        pattern = re.compile(rf"\.power\s*=\s*{name}\s*\.\s*getAttribute\(\s*\"data-power\"\s*\)")
        record = next(_code_matches(pattern, snapshot), None)
    if record is None or not inside(record.start(), if_blocks(snapshot, _ONE_ID)):
        found.add("rendered power never recorded")
    conditions = [apply[start:end] for start, end in if_conditions(apply)]
    if not any(_DETAIL_PAGE.search(c) and _POWER_COMPARED.search(c) for c in conditions):
        found.add("power change ignored on the location page")
    if not any(_POWER_VALID.search(condition) for condition in conditions):
        found.add("power outside the vocabulary accepted")
    return sorted(found)


POWER_RECORD = (
    '    var power = main.querySelector("[data-power]");\n'
    "    if (ids.length === 1 && rendered[ids[0]].power === undefined && power) {\n"
    '      rendered[ids[0]].power = power.getAttribute("data-power");\n'
    "    }\n"
)
POWER_COMPARE = " ||\n          (before.power !== undefined && entry.power !== before.power)"
GOOD_POWER = (
    """Alpine.data("poll", function () {
  function snapshot() {
    var ids = Object.keys(rendered);
"""
    + POWER_RECORD
    + """  }
  function apply(payload) {
    Object.keys(locations).forEach(function (id) {
      var entry = locations[id];
      if (
        entry &&
        STATUS_KEYS.indexOf(entry.status) >= 0 &&
        POWER_KEYS.indexOf(entry.power) >= 0
      ) {
        valid[id] = entry;
      }
    });
    renderedIds.forEach(function (id) {
      if (!entry) {
        changed = true;
      } else if (
        page === "detail" &&
        (entry.status !== before.status"""
    + POWER_COMPARE
    + """)
      ) {
        changed = true;
      }
    });
  }
})
"""
)
POWER_NOT_RECORDED = "rendered power never recorded"
POWER_IGNORED = "power change ignored on the location page"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Failure: the wave-5 shape (nothing recorded, nothing compared), a record for any
        # number of ids, a comparison off the location page, and an unchecked power.
        pytest.param(POWER_RECORD, "", [POWER_NOT_RECORDED], id="not-recorded"),
        pytest.param("ids.length === 1 && ", "", [POWER_NOT_RECORDED], id="any-ids"),
        pytest.param(POWER_COMPARE, "", [POWER_IGNORED], id="not-compared"),
        pytest.param('page === "detail"', 'page === "list"', [POWER_IGNORED], id="other-page"),
        pytest.param(
            " &&\n        POWER_KEYS.indexOf(entry.power) >= 0",
            "",
            ["power outside the vocabulary accepted"],
            id="unchecked",
        ),
    ],
)
def test_power_change_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_POWER
    assert power_change_violations(GOOD_POWER.replace(old, new)) == expected


def test_W5A2_power_change_under_maintenance_shows_the_changed_chip() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    detail = (TEMPLATES / "web" / "location_detail.html").read_text(encoding="utf-8")
    content = detail[detail.index("{% block content %}") :]
    others = [
        path.relative_to(TEMPLATES).as_posix()
        for path in sorted(TEMPLATES.rglob("*.html"))
        if "data-power" in path.read_text(encoding="utf-8")
    ]
    row = LiveRow(
        pk=7,
        name="Office",
        status="maintenance",
        status_label="Maintenance",
        last_heartbeat_at=None,
        alerts_off=False,
        router_grace=False,
        delivery=None,
        power="off",
        on_since=None,
        outage_started_at=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
        delivery_failing=False,
    )

    # Expected: S5 renders the stored power state once, in its content, as [data-power];
    # the JSON carries the same value as "power" in the engine's vocabulary, which is the
    # poll's POWER_KEYS; the poll records the rendered value and compares it on S5.
    assert power_change_violations(source) == []
    power_keys = re.findall(r"\"([^\"]*)\"", js_var(source, "POWER_KEYS"))
    assert sorted(power_keys) == sorted(STATUSES)
    assert detail.count("data-power=") == 1
    assert 'data-power="{{ status.power_key }}"' in content
    assert others == ["web/location_detail.html"]
    # Edge: under maintenance the JSON's status stays "maintenance"; its power moves.
    assert status_payload(row)["status"] == "maintenance"
    assert status_payload(row)["power"] == "off"
    # Failure: no poll at all reports every rule.
    assert power_change_violations("var x = 1;") == [
        POWER_IGNORED,
        "power outside the vocabulary accepted",
        POWER_NOT_RECORDED,
    ]


_SIDEBAR_COUNT_LOOP = re.compile(
    r"\b([\w$]+)\s*\.\s*querySelectorAll\(\s*'\[data-live=\"sidebar-count\"\]'\s*\)"
    r"\s*\.\s*forEach\s*\("
)
# apply hands updateCounts the JSON's number of location ids as the total.
_COUNTS_FROM_IDS = re.compile(
    r"\bupdateCounts\(\s*payload\s*\.\s*counts\s*,\s*ids\s*\.\s*length\s*\)"
)


def sidebar_count_violations(source: str) -> list[str]:
    """The sidebar's group count follows the poll: the poll's updateCounts writes
    String(total), the JSON's number of locations, and nothing else, as the text of every
    [data-live="sidebar-count"], looked up on document (the sidebar is outside main)."""
    update = function_body(component_bodies(source).get("poll", ""), "updateCounts")
    match = next(_code_matches(_SIDEBAR_COUNT_LOOP, update), None)
    if match is None:
        return ["sidebar count never written"]
    code = _mask_js(update)
    start = code.index("forEach", match.start())
    loop = update[start : _closing(code, start) + 1]
    parameter = re.match(r"forEach\s*\(\s*function\s*\(\s*([\w$]+)\s*\)", _mask_js(loop))
    element = parameter.group(1) if parameter else "element"
    found = set()
    if match.group(1) != "document":
        found.add("sidebar count looked up inside main")
    if text_writes(loop, element) != ["String(total)"]:
        found.add("sidebar count gets more than the number")
    return sorted(found)


GOOD_SIDEBAR_COUNT = """Alpine.data("poll", function () {
  function updateCounts(counts, total) {
    document.querySelectorAll('[data-live="sidebar-count"]').forEach(function (element) {
      element.textContent = String(total);
    });
  }
})
"""


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Edge: a loop in a comment is not one (the wave-5 shape: never written).
        pytest.param(
            "    document.querySelectorAll",
            "    // document.querySelectorAll",
            ["sidebar count never written"],
            id="comment",
        ),
        # Failure: looked up inside main (the sidebar is outside it), or a noun added.
        pytest.param(
            "document.querySelectorAll",
            "main.querySelectorAll",
            ["sidebar count looked up inside main"],
            id="in-main",
        ),
        pytest.param(
            "element.textContent = String(total);",
            'element.textContent = total + " locations";',
            ["sidebar count gets more than the number"],
            id="noun",
        ),
    ],
)
def test_sidebar_count_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_SIDEBAR_COUNT
    assert sidebar_count_violations(GOOD_SIDEBAR_COUNT.replace(old, new)) == expected


def test_sidebar_count_follows_the_poll() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    poll = component_bodies(source).get("poll", "")
    sidebar = (TEMPLATES / "partials" / "sidebar.html").read_text(encoding="utf-8")
    tag = re.search(r"<p data-live=\"sidebar-count\"[^>]*>(?P<text>[^<]*)</p>", sidebar)

    # Expected: the poll hands the JSON's number of location ids to updateCounts, which
    # writes it as the plain text of the sidebar's mono count, rendered with the number only.
    assert sidebar_count_violations(source) == []
    assert _COUNTS_FROM_IDS.search(poll) is not None
    assert tag is not None and tag.group("text") == "{{ sidebar.rows|length }}"
    assert re.search(rf"\b{CLASS}=\"[^\"]*\bnum\b", tag.group(0)) is not None
    # Failure: no poll at all.
    assert sidebar_count_violations("var x = 1;") == ["sidebar count never written"]


# admin.js wave-6 audit pins (W6-A1 step 5)


_FIRST_HEARTBEAT_BRANCH = re.compile(r"\bkind\s*===\s*\"first-heartbeat\"")
_RECEIVED_FROM_POWER = re.compile(r"\breceived\s*=\s*entry\s*\.\s*power\s*!==\s*\"waiting\"")
# Any read of a status in code: the local copy or entry.status.
_STATUS_READ = re.compile(r"(?<![\w$])status\b")
_LIVE_LOOP = re.compile(
    r"\bquerySelectorAll\(\s*\"\[data-live\]\[data-location-id\]\"\s*\)\s*\.\s*forEach\s*\("
)
_ENTRY_FROM_VALID = re.compile(r"\bvar\s+entry\s*=\s*own\(\s*valid\s*,")
# A call of updateLocation, not its declaration.
_UPDATE_LOCATION_CALL = re.compile(r"(?<![\w$.])(?<!function )updateLocation\s*\(")
# The server's rule (location_setup.html): the received line is hidden while power waits.
SETUP_RECEIVED_RULE = 'data-fh="received"{% if status.power_key == "waiting" %} hidden{% endif %}'
STEP5_STATUS = "step 5 does not follow the power state"
STEP5_READS_STATUS = "step 5 reads the status"
STEP5_UNVALIDATED = "step 5 gets unvalidated entries"
STEP5_POWER_UNCHECKED = "power outside the vocabulary reaches step 5"


def first_heartbeat_violations(source: str) -> list[str]:
    """W6-A1: S8 step 5 follows the power state, as the server's rule does. The poll's
    first-heartbeat branch sets received from the entry's power (every power but waiting)
    and never reads the status, which stays "maintenance" while a location under
    maintenance still waits for its first heartbeat. updateLocation gets only entries of
    the validated set, whose power is one of POWER_KEYS."""
    body = component_bodies(source).get("poll", "")
    update = function_body(body, "updateLocation")
    apply = function_body(body, "apply")
    branches = if_blocks(update, _FIRST_HEARTBEAT_BRANCH)
    if not branches:
        return ["no first-heartbeat branch"]
    start, end = branches[0]
    branch = update[start : end + 1]
    found = set()
    if next(_code_matches(_RECEIVED_FROM_POWER, branch), None) is None:
        found.add(STEP5_STATUS)
    if next(_code_matches(_STATUS_READ, branch), None) is not None:
        found.add(STEP5_READS_STATUS)
    code = _mask_js(apply)
    loops = []
    for match in _code_matches(_LIVE_LOOP, apply):
        each = code.index("forEach", match.start())
        loops.append(apply[each : _closing(code, each) + 1])
    calls = list(_code_matches(_UPDATE_LOCATION_CALL, body))
    fed = [loop for loop in loops if next(_code_matches(_UPDATE_LOCATION_CALL, loop), None)]
    if (
        len(calls) != 1
        or len(fed) != 1
        or next(_code_matches(_ENTRY_FROM_VALID, fed[0]), None) is None
    ):
        found.add(STEP5_UNVALIDATED)
    conditions = [apply[start:end] for start, end in if_conditions(apply)]
    if not any(_POWER_VALID.search(condition) for condition in conditions):
        found.add(STEP5_POWER_UNCHECKED)
    return sorted(found)


STEP5_RECEIVED = '      var received = entry.power !== "waiting";\n'
GOOD_FIRST_HEARTBEAT = (
    """Alpine.data("poll", function () {
  function updateLocation(element, entry) {
    var kind = element.getAttribute("data-live");
    var status = entry.status;
    if (kind === "status") {
      element.setAttribute("data-status", status);
    } else if (kind === "first-heartbeat") {
"""
    + STEP5_RECEIVED
    + """      var waitingLine = element.querySelector('[data-fh="waiting"]');
      if (waitingLine) {
        waitingLine.hidden = received;
      }
    }
  }
  function apply(payload) {
    Object.keys(locations).forEach(function (id) {
      var entry = locations[id];
      if (entry && POWER_KEYS.indexOf(entry.power) >= 0) {
        valid[id] = entry;
      }
    });
    document.querySelectorAll("[data-live][data-location-id]").forEach(function (element) {
      var entry = own(valid, element.getAttribute("data-location-id"));
      if (entry) {
        updateLocation(element, entry);
      }
    });
  }
})
"""
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Edge: the status rule left in a comment is not a read.
        pytest.param(
            STEP5_RECEIVED,
            STEP5_RECEIVED + '      // var received = status !== "waiting";\n',
            [],
            id="comment",
        ),
        # Failure: the wave-6 shape (received from the status), entries that skipped the
        # vocabulary filter, a second caller and an unchecked power.
        pytest.param(
            STEP5_RECEIVED,
            '      var received = status !== "waiting";\n',
            [STEP5_STATUS, STEP5_READS_STATUS],
            id="status",
        ),
        pytest.param(
            STEP5_RECEIVED,
            '      var received = entry.status !== "waiting";\n',
            [STEP5_STATUS, STEP5_READS_STATUS],
            id="entry-status",
        ),
        pytest.param("own(valid,", "own(locations,", [STEP5_UNVALIDATED], id="unvalidated"),
        pytest.param(
            "  function apply(payload) {\n",
            "  function apply(payload) {\n    updateLocation(element, locations[id]);\n",
            [STEP5_UNVALIDATED],
            id="second-caller",
        ),
        pytest.param(
            " && POWER_KEYS.indexOf(entry.power) >= 0", "", [STEP5_POWER_UNCHECKED], id="unchecked"
        ),
    ],
)
def test_first_heartbeat_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_FIRST_HEARTBEAT
    assert first_heartbeat_violations(GOOD_FIRST_HEARTBEAT.replace(old, new)) == expected


def test_W6A1_step5_follows_the_power_state() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    setup = (TEMPLATES / "web" / "location_setup.html").read_text(encoding="utf-8")
    row = LiveRow(
        pk=7,
        name="Office",
        status="maintenance",
        status_label="Maintenance",
        last_heartbeat_at=None,
        alerts_off=False,
        router_grace=False,
        delivery=None,
        power="waiting",
        on_since=None,
        outage_started_at=None,
        delivery_failing=False,
    )

    # Expected: the poll flips step 5 on the JSON's power, the same rule the server renders
    # (the received line is hidden while the power state is waiting), so the first poll
    # never contradicts the page it updates.
    assert first_heartbeat_violations(source) == []
    assert setup.count('data-fh="received"') == 1
    assert SETUP_RECEIVED_RULE in setup
    # Edge: maintenance switched on before the first heartbeat (D-02): the JSON's status is
    # maintenance and its power still waiting, so step 5 keeps the waiting line.
    assert (status_payload(row)["status"], status_payload(row)["power"]) == (
        "maintenance",
        "waiting",
    )
    # Failure: no poll at all.
    assert first_heartbeat_violations("var x = 1;") == ["no first-heartbeat branch"]


# admin.js wave-6 audit pins (W6-A3 and W6-A4: S3's fleet-showing description)


def var_function(source: str, name: str) -> tuple[list[str], str]:
    """(parameter names, text) of the first ``var <name> = function (...) {...}`` in code,
    or ([], "")."""
    code = _mask_js(source)
    pattern = re.compile(rf"\bvar\s+{re.escape(name)}\s*=\s*function\s*\(([^)]*)\)\s*\{{")
    match = next(_code_matches(pattern, source), None)
    if match is None:
        return [], ""
    end = _closing(code, match.end() - 1, "{", "}")
    parameters = [part.strip() for part in match.group(1).split(",") if part.strip()]
    return parameters, source[match.start() : end + 1]


def call_arguments(block: str, name: str) -> list[tuple[int, list[str]]]:
    """(offset, arguments) of every ``<name>(...)`` call in code, each argument's text with
    its whitespace collapsed; brackets and strings stay inside one argument."""
    code = _mask_js(block)
    calls = []
    for match in _code_matches(re.compile(rf"(?<![\w$.]){re.escape(name)}\s*\("), block):
        opening = code.index("(", match.start())
        closing = _closing(code, opening)
        arguments, depth, start = [], 0, opening + 1
        for index in range(opening + 1, closing):
            char = code[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                arguments.append(" ".join(block[start:index].split()))
                start = index + 1
        arguments.append(" ".join(block[start:closing].split()))
        calls.append((match.start(), arguments))
    return calls


# The description's parts (_fleet_health.html), by the key describe writes them under.
SHOWING_HOOKS = {
    "all": "[data-showing-all]",
    "shown": "[data-showing-shown]",
    "of": "[data-showing-of]",
    "total": "[data-showing-total]",
    "noun": "[data-showing-noun]",
}
_SHOWING_LOOKUP = re.compile(r"showing\s*\.\s*querySelector\(\s*\"(\[data-showing-[a-z]+\])\"\s*\)")
_SHOWING_VARIABLE = re.compile(
    r"\bvar\s+([\w$]+)\s*=\s*showing\s*\.\s*querySelector\(\s*\"(\[data-showing-[a-z]+\])\"\s*\)"
)
_DIRECT_WRITE = r"(?<![\w$.])([\w$]+)\s*\.\s*{prop}\s*=(?!=)"
SHOWING_WHOLE = "whole text written over the count spans"
SHOWING_COUNT = "count span gets more than the number"
SHOWING_TOTAL = "total span gets more than the number"
SHOWING_NOUN = "noun span gets more than the noun"
SHOWING_TOGGLES = "the all and of parts are never toggled"
SHOWING_TEXT_UNCOMPARED = "text written without a compare"
SHOWING_HIDDEN_UNCOMPARED = "hidden flag written without a compare"


def _showing_part(target: str, variables: Mapping[str, str]) -> str | None:
    """The part a write target names: "whole" (the description), a SHOWING_HOOKS key, or
    None."""
    if target == "showing":
        return "whole"
    hook = variables.get(target)
    if hook is None:
        lookup = _SHOWING_LOOKUP.fullmatch(target)
        hook = lookup.group(1) if lookup else None
    return next((key for key, value in SHOWING_HOOKS.items() if value == hook), None)


def _direct_writes(block: str, prop: str) -> list[tuple[int, list[str]]]:
    """(offset, [target, value]) of every ``<name>.<prop> = value;`` in code."""
    code = _mask_js(block)
    writes = []
    for match in _code_matches(re.compile(_DIRECT_WRITE.format(prop=prop)), block):
        end = code.find(";", match.end())
        value = block[match.end() : len(block) if end < 0 else end]
        writes.append((match.start(), [match.group(1), " ".join(value.split())]))
    return writes


def _noun_only(value: str, describe: str) -> bool:
    """The value is "location" or "locations": a literal, or a variable of describe whose
    assignment holds only those literals and no concatenation."""
    if re.fullmatch(r"[\w$]+", value):
        assigned = re.search(rf"\bvar\s+{re.escape(value)}\s*=([^;]*);", describe)
        if assigned is None:
            return False
        value = assigned.group(1)
    words = {a or b for a, b in _JS_STRING.findall(value)}
    return bool(words) and words <= {"location", "locations"} and "+" not in value


def showing_write_violations(source: str) -> list[str]:
    """W6-A3 and the mono rule: fleetFilter's describe writes the shown count and the total
    only as String(...) into the mono [data-showing-shown] and [data-showing-total] spans,
    the noun only as "location" or "locations" into [data-showing-noun], and toggles the
    "all " and " of {M}" parts; it writes the whole sentence only when the description has
    no count span (the fallback), and no other fleetFilter code writes the description."""
    body = component_bodies(source).get("fleetFilter", "")
    _, describe = var_function(body, "describe")
    if not describe:
        return ["no describe"]
    variables = {
        match.group(1): match.group(2) for match in _code_matches(_SHOWING_VARIABLE, describe)
    }
    count = next((name for name, hook in variables.items() if hook == SHOWING_HOOKS["shown"]), None)
    found = set()
    guards = []
    if count is None:
        found.add(f"no {SHOWING_HOOKS['shown']} hook")
    else:
        unguarded = re.compile(rf"(?<![\w$!=])!\s*{re.escape(count)}(?![\w$.\[(])")
        guards = if_blocks(describe, unguarded)
    writes: dict[str | None, list[str]] = {}
    for offset, arguments in [
        *call_arguments(describe, "setText"),
        *_direct_writes(describe, "textContent"),
    ]:
        if len(arguments) != 2:
            continue
        part = _showing_part(arguments[0], variables)
        writes.setdefault(part, []).append(arguments[1])
        if part == "whole" and not inside(offset, guards):
            found.add(SHOWING_WHOLE)
    start = body.index(describe)
    for offset, arguments in [
        *call_arguments(body, "setText"),
        *_direct_writes(body, "textContent"),
    ]:
        outside = not start <= offset < start + len(describe)
        if outside and arguments and arguments[0] == "showing":
            found.add(SHOWING_WHOLE)
    if count is not None and any(
        not re.fullmatch(r"String\([^\"'+]*\)", value) for value in writes.get("shown", [""])
    ):
        found.add(SHOWING_COUNT)
    if writes.get("total") != ["String(total)"]:
        found.add(SHOWING_TOTAL)
    if not all(_noun_only(value, describe) for value in writes.get("noun", [""])):
        found.add(SHOWING_NOUN)
    toggled = {
        _showing_part(arguments[0], variables)
        for _, arguments in [
            *call_arguments(describe, "setHidden"),
            *_direct_writes(describe, "hidden"),
        ]
        if arguments
    }
    if not {"all", "of"} <= toggled:
        found.add(SHOWING_TOGGLES)
    return sorted(found)


def showing_announce_violations(source: str) -> list[str]:
    """W6-A4: the description is a polite live region, so fleetFilter writes one of its
    texts or hidden flags only when the value differs. Its text writer setText and its flag
    writer setHidden assign inside an if that compares the same property with !==; no other
    fleetFilter code assigns a textContent, and describe assigns no hidden flag itself."""
    body = component_bodies(source).get("fleetFilter", "")
    found = set()
    helpers = {}
    for helper, prop, message in (
        ("setText", "textContent", SHOWING_TEXT_UNCOMPARED),
        ("setHidden", "hidden", SHOWING_HIDDEN_UNCOMPARED),
    ):
        parameters, text = var_function(body, helper)
        helpers[helper] = text
        if len(parameters) != 2:
            found.add(f"no {helper}")
            continue
        element, value = (re.escape(parameter) for parameter in parameters)
        compare = re.compile(rf"(?<![\w$.]){element}\s*\.\s*{prop}\s*!==\s*{value}(?![\w$])")
        guards = if_blocks(text, compare)
        assigns = list(_code_matches(re.compile(rf"\.\s*{prop}\s*=(?!=)"), text))
        if not assigns or any(not inside(match.start(), guards) for match in assigns):
            found.add(message)
    set_text = helpers["setText"]
    start = body.find(set_text) if set_text else -1
    for match in _code_matches(re.compile(r"\.\s*textContent\s*=(?!=)"), body):
        if not (set_text and start <= match.start() < start + len(set_text)):
            found.add(SHOWING_TEXT_UNCOMPARED)
    _, describe = var_function(body, "describe")
    if next(_code_matches(re.compile(r"\.\s*hidden\s*=(?!=)"), describe), None) is not None:
        found.add(SHOWING_HIDDEN_UNCOMPARED)
    return sorted(found)


SHOWING_SET_TEXT = (
    "    var setText = function (element, value) {\n"
    "      if (element && element.textContent !== value) {\n"
    "        element.textContent = value;\n"
    "      }\n"
    "    };\n"
)
SHOWING_SET_HIDDEN = (
    "    var setHidden = function (element, value) {\n"
    "      if (element && element.hidden !== value) {\n"
    "        element.hidden = value;\n"
    "      }\n"
    "    };\n"
)
SHOWING_COUNT_WRITE = "      setText(count, String(filtered ? shown : total));\n"
SHOWING_NOUN_WRITE = '      setText(showing.querySelector("[data-showing-noun]"), noun);\n'
SHOWING_TOTAL_WRITE = (
    '      setText(showing.querySelector("[data-showing-total]"), String(total));\n'
)
SHOWING_OF_TOGGLE = '      setHidden(showing.querySelector("[data-showing-of]"), !filtered);\n'
SHOWING_DESCRIBED = "        describe(shown, total);\n"
GOOD_SHOWING = (
    """Alpine.data("fleetFilter", function () {
  return { init: function () {
    var showing = root.querySelector('[data-testid="fleet-showing"]');
"""
    + SHOWING_SET_TEXT
    + SHOWING_SET_HIDDEN
    + """    var describe = function (shown, total) {
      var filtered = active !== "all";
      var noun = total === 1 ? "location" : "locations";
      var count = showing.querySelector("[data-showing-shown]");
      if (!count) {
        var sentence = "Showing " + shown + " of " + total + " " + noun;
        if (!filtered) {
          sentence = total === 1 ? "Showing 1 location" : "Showing all " + total + " " + noun;
        }
        setText(showing, sentence);
        return;
      }
      setHidden(showing.querySelector("[data-showing-all]"), filtered || total === 1);
"""
    + SHOWING_COUNT_WRITE
    + SHOWING_OF_TOGGLE
    + SHOWING_TOTAL_WRITE
    + SHOWING_NOUN_WRITE
    + """    };
    var apply = function () {
      if (showing) {
"""
    + SHOWING_DESCRIBED
    + """      }
    };
  } };
})
"""
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Edge: a whole-text write in a comment is not one.
        pytest.param(
            SHOWING_COUNT_WRITE,
            SHOWING_COUNT_WRITE + "      // showing.textContent = sentence;\n",
            [],
            id="comment",
        ),
        # Failure: the wave-6 shape (apply writes describe's sentence as the whole text),
        # the whole text written over the spans, words in a number span, a number in the
        # noun span, no count span, no toggle.
        pytest.param(
            SHOWING_DESCRIBED,
            "        showing.textContent = describe(shown, total);\n",
            [SHOWING_WHOLE],
            id="whole-in-apply",
        ),
        pytest.param(
            SHOWING_NOUN_WRITE,
            SHOWING_NOUN_WRITE + '      setText(showing, "Showing " + shown);\n',
            [SHOWING_WHOLE],
            id="whole-after-spans",
        ),
        pytest.param(
            SHOWING_COUNT_WRITE,
            '      setText(count, "Showing " + shown);\n',
            [SHOWING_COUNT],
            id="words-in-count",
        ),
        pytest.param(
            SHOWING_TOTAL_WRITE,
            '      setText(showing.querySelector("[data-showing-total]"), " of " + total);\n',
            [SHOWING_TOTAL],
            id="words-in-total",
        ),
        pytest.param(
            SHOWING_NOUN_WRITE,
            '      setText(showing.querySelector("[data-showing-noun]"), total + " " + noun);\n',
            [SHOWING_NOUN],
            id="number-in-noun",
        ),
        pytest.param(
            '      var count = showing.querySelector("[data-showing-shown]");\n',
            "      var count = null;\n",
            [f"no {SHOWING_HOOKS['shown']} hook", SHOWING_WHOLE],
            id="no-count-span",
        ),
        pytest.param(SHOWING_OF_TOGGLE, "", [SHOWING_TOGGLES], id="no-toggle"),
    ],
)
def test_showing_write_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_SHOWING
    assert showing_write_violations(GOOD_SHOWING.replace(old, new)) == expected


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param("", "", [], id="good"),
        # Edge: an unguarded write in a comment is not one.
        pytest.param(
            SHOWING_SET_TEXT,
            SHOWING_SET_TEXT + "    // element.textContent = value;\n",
            [],
            id="comment",
        ),
        # Failure: writers that do not compare, a compare of another property, direct
        # writes in describe, the wave-6 shape (apply writes the whole text on every run)
        # and a missing writer.
        pytest.param(
            "element && element.textContent !== value",
            "element",
            [SHOWING_TEXT_UNCOMPARED],
            id="text-uncompared",
        ),
        pytest.param(
            "element && element.hidden !== value",
            "element",
            [SHOWING_HIDDEN_UNCOMPARED],
            id="hidden-uncompared",
        ),
        pytest.param(
            "element && element.textContent !== value",
            "element && element.hidden !== value",
            [SHOWING_TEXT_UNCOMPARED],
            id="other-property",
        ),
        pytest.param(
            SHOWING_COUNT_WRITE,
            "      count.textContent = String(filtered ? shown : total);\n",
            [SHOWING_TEXT_UNCOMPARED],
            id="direct-text",
        ),
        pytest.param(
            SHOWING_OF_TOGGLE,
            '      showing.querySelector("[data-showing-of]").hidden = !filtered;\n',
            [SHOWING_HIDDEN_UNCOMPARED],
            id="direct-hidden",
        ),
        pytest.param(
            SHOWING_DESCRIBED,
            "        showing.textContent = describe(shown, total);\n",
            [SHOWING_TEXT_UNCOMPARED],
            id="whole-in-apply",
        ),
        pytest.param(SHOWING_SET_HIDDEN, "", ["no setHidden"], id="no-set-hidden"),
    ],
)
def test_showing_announce_rule(old: str, new: str, expected: list[str]) -> None:
    assert old in GOOD_SHOWING
    assert showing_announce_violations(GOOD_SHOWING.replace(old, new)) == expected


# A span tag carrying the attribute, template tags inside the tag included.
_SHOWING_SPAN = r"<span\b[^>]*\s{attribute}(?![\w-])[^>]*>"


def test_W6A3_fleet_showing_keeps_its_counts_mono() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    card = (TEMPLATES / "web" / "_fleet_health.html").read_text(encoding="utf-8")

    # Expected: describe writes the shown count and the total only as numbers into the
    # description's mono spans and the noun into its own span, so a filter press or a poll
    # keeps the counts mono and the words Inter; the whole sentence is only the fallback for
    # a description without the spans.
    assert showing_write_violations(source) == []
    # Edge: the card renders each part exactly once, the two counts with the num token.
    for hook in SHOWING_HOOKS.values():
        spans = re.findall(_SHOWING_SPAN.format(attribute=hook[1:-1]), card)
        assert len(spans) == 1, hook
        mono = re.search(rf"\b{CLASS}=\"[^\"]*\bnum\b", spans[0]) is not None
        assert mono == (hook in (SHOWING_HOOKS["shown"], SHOWING_HOOKS["total"])), hook
    # Failure: no fleetFilter at all.
    assert showing_write_violations("var x = 1;") == ["no describe"]


def test_W6A4_fleet_showing_writes_only_what_changed() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    # Expected: fleetFilter compares before every write to the polite description (texts
    # and hidden flags), so a poll that changes nothing announces nothing and a filter press
    # announces the new sentence once.
    assert showing_announce_violations(source) == []
    # Failure: no fleetFilter at all.
    assert showing_announce_violations("var x = 1;") == ["no setHidden", "no setText"]


# admin.js code-review pins (06-REVIEW WR-02 and IN-05: the submit guard)

_SUBMIT_LISTENER = re.compile(r"\bdocument\s*\.\s*addEventListener\s*\(\s*([\"'])submit\1")
_PAGESHOW_LISTENER = re.compile(r"\bwindow\s*\.\s*addEventListener\s*\(\s*([\"'])pageshow\1")
_INDEX_OF_ENTRY = r"\bpending\s*\.\s*indexOf\s*\(\s*entry\s*\)"
# release returns at once for an entry no longer pending: through a variable that holds the
# entry's index, or with the lookup in the condition itself.
_ALREADY_RELEASED = re.compile(
    rf"\bvar\s+(?P<index>[\w$]+)\s*=\s*{_INDEX_OF_ENTRY}\s*;"
    r"\s*if\s*\(\s*(?P=index)\s*===?\s*-1\s*\)\s*\{?\s*return\b"
    rf"|\bif\s*\(\s*{_INDEX_OF_ENTRY}\s*===?\s*-1\s*\)\s*\{{?\s*return\b"
)
_ENTRY_REMOVED = re.compile(r"\bpending\s*\.\s*splice\s*\(|\bpending\s*=\s*pending\s*\.\s*filter\(")
_TIMER_SET = re.compile(r"\bentry\s*\.\s*timer\s*=\s*window\s*\.\s*setTimeout\s*\(")
_TIMER_CLEARED = re.compile(r"\bwindow\s*\.\s*clearTimeout\s*\(\s*entry\s*\.\s*timer\s*\)")
_UNMARKED = re.compile(r"\bentry\s*\.\s*form\s*\.\s*removeAttribute\(\s*\"data-submitted\"\s*\)")
_LABEL_RESTORED = re.compile(r"\bentry\s*\.\s*label\s*\.\s*textContent\s*=\s*entry\s*\.\s*text\b")
_RELEASE_ALL = re.compile(r"\bpending\s*\.\s*slice\s*\(\s*\)\s*\.\s*forEach\s*\(\s*release\s*\)")
_RUN_HANDLERS = re.compile(r"\bpageshowHandlers\s*\.\s*forEach\s*\(")
_PENDING_SOME = re.compile(r"\bpending\s*\.\s*some\s*\(")
_ACTION_READ = re.compile(r"\.\s*getAttribute\s*\(\s*([\"'])action\1\s*\)")
# A pending form's action read and compared for equality with this form's, either way round.
_SAME_ACTION = re.compile(
    r"\.\s*getAttribute\s*\(\s*([\"'])action\1\s*\)\s*===\s*action\b"
    r"|\baction\s*===\s*[\w.]+\s*\.\s*getAttribute\s*\(\s*([\"'])action\2\s*\)"
)
_BUSY = re.compile(r"\bvar\s+busy\s*=")
_ENTRY_PENDING = re.compile(r"\bpending\s*\.\s*push\s*\(\s*entry\s*\)")
# The submitter's two attributes the guard sets, each restored to its value from before.
GUARD_RESTORES = (
    ["entry.button", '"aria-busy"', "entry.ariaBusy"],
    ["entry.button", '"aria-disabled"', "entry.ariaDisabled"],
)

GUARD_SHORT_DELAY = "release delay under twice the test message's timeouts"
GUARD_NO_TIMER = "no release timer per submit"
GUARD_NOT_IDEMPOTENT = "release acts on an entry already released"
GUARD_TIMER_KEPT = "release keeps the entry's timer"
GUARD_MARK_KEPT = "release leaves a mark"
GUARD_PAGESHOW = "pageshow does not release a copy of pending before its handlers"
GUARD_NO_SAME_ACTION = "no same-action check across pending submits"
GUARD_NOT_PENDING = "a submit never records its entry as pending before its timer"


def listener_call(source: str, pattern: re.Pattern[str]) -> str:
    """The text of the first ``addEventListener(...)`` call in code that ``pattern`` finds,
    up to its closing parenthesis, or ""."""
    code = _mask_js(source)
    match = next(_code_matches(pattern, source), None)
    if match is None:
        return ""
    return source[match.start() : _closing(code, match.start()) + 1]


def first_in_code(pattern: re.Pattern[str], block: str) -> int:
    """The offset of the first match of ``pattern`` that starts in code, or -1."""
    match = next(_code_matches(pattern, block), None)
    return -1 if match is None else match.start()


def guard_release_floor_ms() -> float:
    """The least release delay, in ms: twice the test message's connect + read timeouts
    (the Telegram client's DEFAULT_TIMEOUT), the slowest POST the guard holds."""
    from powermon.telegram.client import DEFAULT_TIMEOUT

    return 2 * sum(DEFAULT_TIMEOUT) * 1000


def guard_violations(source: str) -> list[str]:
    """WR-02 / IN-05: how admin.js's submit guard can stay stuck or let a duplicate through.

    Each submit stores a timer that calls release(entry) after GUARD_RELEASE_MS, at least
    twice the test message's timeouts. release returns at once for an entry no longer
    pending, else takes it out of pending, clears its timer and undoes every mark
    (data-submitted, the submitter's aria-busy and aria-disabled, the label's text).
    pageshow releases a copy of pending (release edits the list) before it runs the
    registered handlers. A submit is refused while a pending entry's form has the same
    non-null action, the check being part of the busy test.
    """
    found: set[str] = set()
    delay = re.search(r"\bvar\s+GUARD_RELEASE_MS\s*=\s*(\d+)\s*;", _mask_js(source))
    if delay is None or int(delay.group(1)) < guard_release_floor_ms():
        found.add(GUARD_SHORT_DELAY)
    submit = listener_call(source, _SUBMIT_LISTENER)
    timer_set = first_in_code(_TIMER_SET, submit)
    pushed = first_in_code(_ENTRY_PENDING, submit)
    if pushed < 0 or 0 <= timer_set < pushed:
        found.add(GUARD_NOT_PENDING)
    if timer_set < 0 or not any(
        "release(entry)" in arguments[0] and arguments[1:] == ["GUARD_RELEASE_MS"]
        for _, arguments in call_arguments(submit, "window.setTimeout")
    ):
        found.add(GUARD_NO_TIMER)
    code = _mask_js(submit)
    busy = first_in_code(_BUSY, submit)
    # Only a pending.some(...) inside the "var busy = ..." statement decides the refusal.
    checks = [
        submit[match.start() : _closing(code, match.start()) + 1]
        for match in _code_matches(_PENDING_SOME, submit)
        if 0 <= busy < match.start() and ";" not in code[busy : match.start()]
    ]
    if not any(
        _SAME_ACTION.search(check) and re.search(r"!==?\s*null\b", check) for check in checks
    ):
        found.add(GUARD_NO_SAME_ACTION)
    release = function_body(source, "release")
    released = _ALREADY_RELEASED.search(_mask_js(release))
    removed = first_in_code(_ENTRY_REMOVED, release)
    if released is None or removed < released.start():
        found.add(GUARD_NOT_IDEMPOTENT)
    if first_in_code(_TIMER_CLEARED, release) < 0:
        found.add(GUARD_TIMER_KEPT)
    restores = [arguments for _, arguments in call_arguments(release, "restore")]
    if (
        first_in_code(_UNMARKED, release) < 0
        or first_in_code(_LABEL_RESTORED, release) < 0
        or any(expected not in restores for expected in GUARD_RESTORES)
    ):
        found.add(GUARD_MARK_KEPT)
    pageshow = listener_call(source, _PAGESHOW_LISTENER)
    release_all = first_in_code(_RELEASE_ALL, pageshow)
    handlers = first_in_code(_RUN_HANDLERS, pageshow)
    if release_all < 0 or handlers < 0 or release_all > handlers:
        found.add(GUARD_PAGESHOW)
    return sorted(found)


def test_WR02_submit_guard_releases_a_stopped_navigation() -> None:
    from powermon.telegram.client import DEFAULT_TIMEOUT

    source = ADMIN_JS.read_text(encoding="utf-8")
    submit = listener_call(source, _SUBMIT_LISTENER)
    release = function_body(source, "release")
    pageshow = listener_call(source, _PAGESHOW_LISTENER)

    # Expected: a submit whose navigation never completes while the page stays shown (Stop,
    # Esc, a dropped navigation) is released 30 s after it started: data-submitted removed,
    # aria-busy and aria-disabled back to their values from before, the label's text back.
    # So the confirm dialog, which refuses Keep, Esc and a backdrop click while aria-busy,
    # lets the admin out too.
    assert guard_violations(source) == []
    assert int(js_var(source, "GUARD_RELEASE_MS")) == 30_000
    assert "window.setTimeout(" in submit
    assert "release(entry)" in submit and "GUARD_RELEASE_MS" in submit
    for hook in (
        "indexOf(entry)",
        "window.clearTimeout(entry.timer)",
        'removeAttribute("data-submitted")',
    ):
        assert hook in release, hook
    assert release.count("restore(") == 2
    # pageshow still releases every pending entry first, then runs the handlers.
    assert 0 <= pageshow.find("forEach(release)") < pageshow.find("pageshowHandlers.forEach")
    # Edge: the delay is at least twice the test message's connect + read timeouts (5 s +
    # 10 s), so the guard never lets go of a POST that may still answer.
    assert int(js_var(source, "GUARD_RELEASE_MS")) >= 2 * sum(DEFAULT_TIMEOUT) * 1000
    # Failure: no guard at all breaks every rule.
    assert guard_violations("var x = 1;") == sorted(
        [
            GUARD_SHORT_DELAY,
            GUARD_NO_TIMER,
            GUARD_NOT_IDEMPOTENT,
            GUARD_TIMER_KEPT,
            GUARD_MARK_KEPT,
            GUARD_PAGESHOW,
            GUARD_NO_SAME_ACTION,
            GUARD_NOT_PENDING,
        ]
    )


def test_IN05_submit_guard_refuses_a_second_form_with_the_same_action() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    submit = listener_call(source, _SUBMIT_LISTENER)

    # Expected: until a submit is released, a new one is refused when its form is marked
    # or when a pending entry's form has the same non-null action attribute; forms without
    # an action never match each other.
    assert GUARD_NO_SAME_ACTION not in guard_violations(source)
    assert first_in_code(_PENDING_SOME, submit) >= 0
    assert 'getAttribute("action")' in submit
    assert 'form.hasAttribute("data-submitted")' in submit


# The guard's text the mutations below take out or change.
GUARD_TIMER_JS = (
    "    entry.timer = window.setTimeout(function () {\n"
    "      release(entry);\n"
    "    }, GUARD_RELEASE_MS);\n"
)
GUARD_SAME_ACTION_JS = (
    '      form.hasAttribute("data-submitted") ||\n'
    "      pending.some(function (other) {\n"
    '        return action !== null && other.form.getAttribute("action") === action;\n'
    "      });\n"
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # Edge: a release timer only in a comment is not one.
        pytest.param(
            GUARD_TIMER_JS,
            "    // entry.timer = window.setTimeout(function () { release(entry); }, 30000);\n",
            [GUARD_NO_TIMER],
            id="timer-in-comment",
        ),
        # Failure: a delay the test message can outlast; no clearTimeout; no early return
        # for an entry already released (a stale timer would unmark a newer submit); a mark
        # left in place; pageshow that drops pending without releasing it, or releases the
        # live list (release edits it, so every other entry would stay marked); the
        # same-action check removed, or matching forms without an action (IN-05).
        pytest.param(
            "GUARD_RELEASE_MS = 30000",
            "GUARD_RELEASE_MS = 5000",
            [GUARD_SHORT_DELAY],
            id="short-delay",
        ),
        pytest.param(
            "    window.clearTimeout(entry.timer);\n", "", [GUARD_TIMER_KEPT], id="timer-kept"
        ),
        pytest.param(
            "    if (index === -1) {\n      return;\n    }\n",
            "",
            [GUARD_NOT_IDEMPOTENT],
            id="not-idempotent",
        ),
        pytest.param(
            '    entry.form.removeAttribute("data-submitted");\n',
            "",
            [GUARD_MARK_KEPT],
            id="mark-kept",
        ),
        pytest.param(
            "pending.slice().forEach(release);",
            "pending = [];",
            [GUARD_PAGESHOW],
            id="pageshow-drops-pending",
        ),
        pytest.param(
            "pending.slice().forEach(release);",
            "pending.forEach(release);",
            [GUARD_PAGESHOW],
            id="pageshow-live-list",
        ),
        pytest.param(
            GUARD_SAME_ACTION_JS,
            '      form.hasAttribute("data-submitted");\n',
            [GUARD_NO_SAME_ACTION],
            id="no-same-action",
        ),
        pytest.param(
            "return action !== null && other",
            "return other",
            [GUARD_NO_SAME_ACTION],
            id="null-actions-match",
        ),
        # Failure (fix audit L1/L2): an entry never recorded as pending (release then always
        # returns early, so nothing is ever released); the action comparison inverted; the
        # same-action check computed but no longer part of the busy test.
        pytest.param("    pending.push(entry);\n", "", [GUARD_NOT_PENDING], id="never-pushed"),
        pytest.param(
            'other.form.getAttribute("action") === action',
            'other.form.getAttribute("action") !== action',
            [GUARD_NO_SAME_ACTION],
            id="inverted-equality",
        ),
        pytest.param(
            GUARD_SAME_ACTION_JS,
            '      form.hasAttribute("data-submitted");\n'
            "    pending.some(function (other) {\n"
            '      return action !== null && other.form.getAttribute("action") === action;\n'
            "    });\n",
            [GUARD_NO_SAME_ACTION],
            id="some-unused",
        ),
    ],
)
def test_submit_guard_rule(old: str, new: str, expected: list[str]) -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")

    assert source.count(old) == 1, old
    assert guard_violations(source.replace(old, new)) == expected


# When S5's delivery-failing incident opened (UTC).
FAILING_SINCE = datetime(2026, 10, 1, 7, 58, tzinfo=UTC)


@pytest.mark.django_db
def test_IN05_failing_delivery_renders_two_test_message_forms_with_one_action(
    client: Client, location_factory: Callable[..., Any]
) -> None:
    client.force_login(get_user_model().objects.create_user("admin", password="not-used-here"))
    location = location_factory(name="Office")
    url = reverse("location-test-message", args=[location.pk])
    healthy = parse(client.get(f"/locations/{location.pk}/"))
    with transaction.atomic():
        delivery.open_failing(location.pk, FAILING_SINCE, 403)

    page = parse(client.get(f"/locations/{location.pk}/"))

    # Expected: while delivery fails, the banner and the Controls card each render a
    # test-message form, both with the same action attribute, the one value the guard's
    # same-action check compares, so a press on one while the other is pending is refused.
    banner = by_testid(page, "banner-test-message-form")
    controls = by_testid(page, "test-message-form")
    assert banner["action"] == controls["action"] == url
    # Edge: a healthy page renders only the Controls form, with that same action.
    assert by_testid(healthy, "test-message-form")["action"] == url
    assert [form for form in healthy.find_all("form") if form.get("action") == url] == [
        by_testid(healthy, "test-message-form")
    ]


# CSS entries


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        pytest.param(
            '@import "tailwindcss" source(none);\n@import "./fonts.css";\n'
            '@import "../shared/x.css";\n'
            '@font-face { src: url("../fonts/inter.woff2") format("woff2"); }\n'
            ".a { background: url(fonts/x.svg); }\n"
            ".b { background-image: url(\"data:image/svg+xml,<svg xmlns='http://x'/>\"); }\n"
            "/* see https://tailwindcss.com and url(https://example.com/x) */",
            [],
            id="good",
        ),
        pytest.param(
            '@import "https://fonts.googleapis.com/css2";',
            ["@import other than tailwindcss or a relative path"],
            id="import-url",
        ),
        pytest.param(
            '@import "fonts.css";',
            ["@import other than tailwindcss or a relative path"],
            id="import-bare",
        ),
        pytest.param(
            "@import '/assets/x.css';",
            ["@import other than tailwindcss or a relative path"],
            id="import-absolute",
        ),
        pytest.param(
            '@font-face { src: url("https://cdn.example/x.woff2"); }',
            ["url() that is not relative or data:"],
            id="url-https",
        ),
        pytest.param(
            ".a { background: url(//cdn.example/x.png); }",
            ["url() that is not relative or data:"],
            id="url-protocol-relative",
        ),
        pytest.param(
            ".a { background: url('/static/web/x.png'); }",
            ["url() that is not relative or data:"],
            id="url-absolute",
        ),
    ],
)
def test_frontend_lint_css_entries(sample: str, expected: list[str]) -> None:
    assert violations(sample, CSS_RULES) == expected


def test_frontend_lint_css_entries_real_tree() -> None:
    files = css_entries()

    assert "app.css" in {path.name for path in files}
    report = {path.name: violations(path.read_text(encoding="utf-8"), CSS_RULES) for path in files}
    assert {name: found for name, found in report.items() if found} == {}


# Python under powermon/web

MARK_SAFE = "from django.utils.safestring import mark_safe\n\nmark_safe(x)\n"


@pytest.mark.parametrize(
    ("relpath", "source", "expected"),
    [
        pytest.param(
            "views.py",
            '"""Never mark_safe, SafeString or __html__ here."""\n'
            "from django.utils.html import format_html\n\n"
            "def f(x):\n    return format_html('<b>{}</b>', x)\n",
            [],
            id="good",
        ),
        pytest.param(MARK_SAFE_MODULE, MARK_SAFE, [], id="mark-safe-in-icon-tag"),
        pytest.param("views.py", MARK_SAFE, ["mark_safe outside the icon tag"], id="mark-safe"),
        pytest.param(
            "x/templatetags/icons.py",
            MARK_SAFE,
            ["mark_safe outside the icon tag"],
            id="mark-safe-other-icons",
        ),
        pytest.param(
            "views.py",
            "import django.utils.safestring as s\n\ns.mark_safe(x)\n",
            ["mark_safe outside the icon tag"],
            id="mark-safe-attribute",
        ),
        pytest.param(
            "views.py",
            "from django.utils.safestring import mark_safe as ms\n",
            ["mark_safe outside the icon tag"],
            id="mark-safe-alias",
        ),
        pytest.param(
            "views.py",
            "from django.utils.safestring import SafeString\n\nSafeString(x)\n",
            ["SafeString used"],
            id="safe-string",
        ),
        pytest.param(
            "views.py",
            "from django.utils.safestring import SafeText\n",
            ["SafeText used"],
            id="safe-text",
        ),
        pytest.param(
            "views.py",
            "from django.utils.html import html_safe\n\n@html_safe\nclass A:\n    pass\n",
            ["html_safe used"],
            id="html-safe",
        ),
        pytest.param(
            "views.py",
            "class A:\n    def __html__(self):\n        return ''\n",
            ["__html__ used"],
            id="dunder-html-def",
        ),
        pytest.param("views.py", "y = x.__html__()\n", ["__html__ used"], id="dunder-html-call"),
    ],
)
def test_frontend_lint_python(relpath: str, source: str, expected: list[str]) -> None:
    assert python_violations(relpath, source) == expected


def test_frontend_lint_python_real_tree() -> None:
    modules = web_modules()
    names = {path.relative_to(WEB).as_posix() for path in modules}

    assert {MARK_SAFE_MODULE, "forms.py", "views.py"} <= names
    report = {
        path.relative_to(WEB).as_posix(): python_violations(
            path.relative_to(WEB).as_posix(), path.read_text(encoding="utf-8")
        )
        for path in modules
    }
    assert {name: found for name, found in report.items() if found} == {}
    # The one audited mark_safe is still there, so the exemption is not dead.
    assert python_violations("views.py", (WEB / MARK_SAFE_MODULE).read_text()) == [
        "mark_safe outside the icon tag"
    ]


NON_LITERAL = "format_html with a non-literal format string"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Expected: a constant, an implicit concatenation, a module constant (also annotated),
        # the format_string keyword and a qualified call.
        pytest.param("format_html('<b>{}</b>', x)\n", [], id="constant"),
        pytest.param(
            'HELP = ("<code>{m}</code>" " fixed")\n\n'
            "def f(x):\n    return format_html(HELP, m=x)\n",
            [],
            id="module-constant",
        ),
        pytest.param(
            "HELP: str = '<b>{}</b>'\nformat_html(HELP, x)\n", [], id="annotated-constant"
        ),
        pytest.param("format_html(format_string='<b>{}</b>')\n", [], id="keyword"),
        pytest.param(
            "import django.utils.html\n\ndjango.utils.html.format_html('<b>{}</b>', x)\n",
            [],
            id="qualified",
        ),
        pytest.param(
            "format_html_join('\\n', '<li>{}</li>', ((r,) for r in rows))\n", [], id="join"
        ),
        # Failure: anything the module does not fix as one string constant.
        pytest.param("format_html(f'<b>{x}</b>')\n", [NON_LITERAL], id="f-string"),
        pytest.param(
            "def f(template, x):\n    return format_html(template, x)\n",
            [NON_LITERAL],
            id="parameter",
        ),
        pytest.param(
            "def f(x):\n    template = '<b>{}</b>'\n    return format_html(template, x)\n",
            [NON_LITERAL],
            id="local",
        ),
        pytest.param("format_html(a + b)\n", [NON_LITERAL], id="concatenation"),
        pytest.param("format_html('<b>%s</b>' % x)\n", [NON_LITERAL], id="percent"),
        pytest.param("format_html(obj.attr)\n", [NON_LITERAL], id="attribute"),
        pytest.param("format_html(build())\n", [NON_LITERAL], id="call"),
        pytest.param("format_html(*args)\n", [NON_LITERAL], id="starred"),
        pytest.param("format_html()\n", [NON_LITERAL], id="no-argument"),
        pytest.param(
            "HELP = '<b>{}</b>'\nHELP = '<i>{}</i>'\nformat_html(HELP, x)\n",
            [NON_LITERAL],
            id="assigned-twice",
        ),
        pytest.param(
            "HELP = '<b>{}</b>'\nHELP += '{}'\nformat_html(HELP, x)\n",
            [NON_LITERAL],
            id="augmented",
        ),
        pytest.param(
            "HELP = '<b>{}</b>'\n\ndef f(HELP):\n    return format_html(HELP)\n",
            [NON_LITERAL],
            id="shadowed-by-a-parameter",
        ),
        pytest.param(
            "HELP = '<b>{}</b>'\n\ndef f():\n    global HELP\n    HELP = x\n"
            "    return format_html(HELP)\n",
            [NON_LITERAL],
            id="rebound-through-global",
        ),
        pytest.param("HELP = build()\nformat_html(HELP)\n", [NON_LITERAL], id="non-constant"),
        pytest.param(
            "if x:\n    HELP = '<b>{}</b>'\nformat_html(HELP)\n", [NON_LITERAL], id="not-top-level"
        ),
        pytest.param(
            "format_html_join('\\n', template, rows)\n",
            ["format_html_join with a non-literal format string"],
            id="join-variable",
        ),
        pytest.param(
            "fh = format_html\nfh('<b>{}</b>', x)\n",
            ["format_html not called directly"],
            id="aliased",
        ),
        pytest.param(
            "from django.utils.html import format_html as fh\n",
            ["format_html imported under another name"],
            id="import-alias",
        ),
    ],
)
def test_frontend_lint_format_html_literals(source: str, expected: list[str]) -> None:
    assert python_violations("views.py", source) == expected


def test_format_html_rule_accepts_the_forms_help_constant() -> None:
    source = (WEB / "forms.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    first_arguments = [
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) == "format_html" and node.args
    ]

    # forms.py passes its module constant HELP_NEW_BOT_TOKEN to format_html (the S6 token
    # help), and that name counts as literal; the rule passes the module as it is.
    assert any(isinstance(a, ast.Name) and a.id == "HELP_NEW_BOT_TOKEN" for a in first_arguments)
    assert "HELP_NEW_BOT_TOKEN" in _literal_names(tree)
    assert python_violations("forms.py", source) == []
