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
  javascript: URL; no {{ or {% inside an x-*, @* or :* attribute, and no arrow function,
  template literal or browser global in a directive value (the @alpinejs/csp grammar); no
  http(s):// except the SVG namespace and no protocol-relative URL; no hard-coded /static/
  path; a <script> only in the two exact empty-body forms and only in layouts/app.html and
  layouts/auth.html; every {% icon %} literal name has a file and every {% icon %} a class;
  every {% static %} literal path has a manifest entry (comments are skipped for those two).
- Icon SVGs (templates/icons/*.svg): no <script, on*=, style= or <style, href (xlink:href
  too) or foreignObject.
- admin.js: no eval(, new Function, string timers, document.write, innerHTML, outerHTML,
  insertAdjacentHTML, createContextualFragment, sessionStorage, indexedDB, caches.,
  serviceWorker, pushState, replaceState, window.name, XMLHttpRequest, sendBeacon, confirm(,
  alert(, prompt(, import/export, http(s)://, hard-coded /static/ path, FormData or the bot
  token field; localStorage only inside a try block that has a catch, and only as
  getItem/setItem/removeItem with the literal key powermon.sidebar.rail; document.cookie only
  as an assignment of a string starting with theme= (R4).
- admin.js components (06-11): the Alpine.data names are the 15 of the binding contract,
  each component names its contract hooks, the theme cookie carries exactly the attributes
  ThemeView sets (Secure only on https), and the relative-time floors and units are timefmt's.
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

Later plans extend the rule tables through ``violations(text, rules)``: 06-11 (admin.js
components) and 06-20 (the Alpine directive check: every x-data name registered in admin.js
and every registered name used).

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

import pytest
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.test import RequestFactory

from powermon.web.context_processors import THEME_COOKIE, THEMES
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
# Pattern 4): arrow functions, template literals, browser globals and the keywords below.
_OUTSIDE_CSP_GRAMMAR = re.compile(
    r"=>|`|(?<![\w$.])(?:window|document|globalThis|console|JSON|Math|eval|Function)\b"
    r"|\b(?:new|typeof|function)\b"
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
    return (match.group(1) for match in _DIRECTIVE.finditer(text))


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


def component_bodies(source: str) -> dict[str, str]:
    """The text of each ``Alpine.data(...)`` call, by name (the first one for a repeat)."""
    code = _mask_js(source)
    bodies: dict[str, str] = {}
    for match in _REGISTRATION.finditer(source):
        if code[match.start()] != " ":
            end = _closing(code, match.start())
            bodies.setdefault(match.group("name"), source[match.start() : end + 1])
    return bodies


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


def test_admin_js_registers_names() -> None:
    names = registered_names(ADMIN_JS.read_text(encoding="utf-8"))

    # Expected: no name twice, every name from the contract, the shell components present.
    assert sorted(Counter(names).values())[-1] == 1
    assert set(names) <= CONTRACT_NAMES
    assert {"toasts", "theme", "sidebar", "relative"} <= set(names)
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


@pytest.mark.parametrize("name", sorted(CONTRACT_HOOKS))
def test_admin_js_components_name_their_contract_hooks(name: str) -> None:
    bodies = component_bodies(ADMIN_JS.read_text(encoding="utf-8"))

    # Expected: the component exists and names every hook of its contract row.
    assert missing_hooks(bodies[name], CONTRACT_HOOKS[name]) == []
    # Failure: a body without the hooks reports every one of them.
    stub = 'Alpine.data("' + name + '", function () { return {}; })'
    assert missing_hooks(component_bodies(stub)[name], CONTRACT_HOOKS[name]) == list(
        CONTRACT_HOOKS[name]
    )


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
