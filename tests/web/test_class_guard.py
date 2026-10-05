"""The class-assertion ratchet (TEST-STRATEGY §5.4; 06-RESEARCH Pitfall 17; D6-01).

No web test may assert on CSS classes. Tests read the rebuilt pages through the 06-UI-SPEC
hooks (ids, ``data-testid``, roles, ARIA attributes, tag names) and ``pages.py``, so a
class rename never breaks a behaviour test.

- The guard tokenises every ``tests/web/**/*.py`` file with the stdlib ``tokenize``
  module and fails on any string-literal token that contains a class attribute (the word
  ``class`` then ``=``, in any case): plain and bytes strings, and the literal text of
  f-strings and t-strings, which Python 3.12+ splits into their own tokens. Comments and
  code tokens are never flagged.
- A file that still asserted on classes carried the exact line
  ``# class-guard: pending migration`` right after its module docstring, and the guard
  skipped it. Each page plan removed the marker from the files it migrated (the owners are
  in the 06-09 plan's interfaces and SUMMARY). The migration is over (06-20): no file
  carries the marker, checked with the exact-line rule of ``is_pending`` (this file holds
  the marker text in a constant, so a substring search would list it). The per-file skip
  stays as the mechanism and now skips nothing; a new file never gets the marker.
- Three more ways to couple a test to a class fail in every web test file:
  - bs4's ``class_`` keyword (any code name ``class_``);
  - a class selector in a literal, plain or f-string, passed straight to ``.select(`` or
    ``.select_one(``;
  - a class name of the old stylesheet in a string literal: one of its element and
    modifier names or its compound block names (``OLD_CLASSES``), anywhere in the literal,
    or one of its plain block names (``OLD_BLOCKS``) as a whole quoted value.
  Two files may still name old classes, each for a stated reason (``OLD_MARKUP_FILES``):
  ``test_pages.py``, whose tests feed ``pages.py`` legacy markup to pin how ``messages()``
  treats the old flash callouts, and ``test_css.py``, but only while the old stylesheet it
  tests exists (06-21 deletes both together).
- ``pages.py`` offers no lookup by class: no ``class_`` keyword, no ``"class"`` attribute
  key and no class selector in a ``select`` call.

This file builds its samples, the attribute it looks for and the old class names by
concatenation, so it never holds one as a single literal and passes its own scan.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

WEB_TESTS = Path(__file__).resolve().parent
OLD_STYLESHEET = WEB_TESTS.parents[1] / "powermon" / "web" / "static" / "web" / "app.css"
MARKER = "# class-guard: pending migration"
# A class attribute: the whole word "class", optional spaces, then "=" ("klass=",
# "subclass=" and the keyword "class_=" are not one).
CLASS_ATTRIBUTE = re.compile(r"\bclass\s*=", re.IGNORECASE)
# The token types that carry string-literal text. Python 3.12 splits an f-string into
# FSTRING_START / FSTRING_MIDDLE / FSTRING_END, and 3.14 a t-string the same way.
LITERAL_TOKENS = frozenset(
    getattr(tokenize, name)
    for name in ("STRING", "FSTRING_MIDDLE", "TSTRING_MIDDLE")
    if hasattr(tokenize, name)
)
STRING_STARTS = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_START", "TSTRING_START")
    if hasattr(tokenize, name)
)
STRING_ENDS = frozenset(
    getattr(tokenize, name) for name in ("FSTRING_END", "TSTRING_END") if hasattr(tokenize, name)
)
# A class selector: a dot then a name start, outside [attribute] parts and quotes.
CLASS_SELECTOR = re.compile(r"\.[A-Za-z_-]")
SELECT_CALLS = frozenset({"select", "select_one"})

# The old web/app.css class names no new page has (the element, modifier and compound
# block names), each spelled in two parts. Its plain block names are ordinary words except
# two, which count only as a whole quoted value.
OLD_CLASSES = tuple(
    head + tail
    for head, tail in (
        ("bt", "n--danger"),
        ("bt", "n--primary"),
        ("bt", "n--secondary"),
        ("call", "out--error"),
        ("main", "--single"),
        ("panel", "--empty"),
        ("status", "--failing"),
        ("status", "--maintenance"),
        ("status", "--off"),
        ("status", "--on"),
        ("status", "--waiting"),
        ("status", "-cell"),
        ("site", "-header"),
        ("site-hea", "der__main"),
        ("site", "-nav"),
        ("switch", "__text"),
        ("page", "-head"),
        ("table", "-wrap"),
        ("visually", "-hidden"),
    )
)
OLD_BLOCKS = ("bt" + "n", "call" + "out")
_OLD_CLASS = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(name) for name in OLD_CLASSES) + r")(?![\w-])"
)
_OLD_BLOCK = re.compile(r"[\"'](?:" + "|".join(OLD_BLOCKS) + r")[\"']")


def _old_markup_files() -> dict[str, str]:
    """The files that may name old classes, with the reason each may."""
    files = {"test_pages.py": "feeds pages.py legacy flash markup (messages() behaviour)"}
    if OLD_STYLESHEET.exists():
        files["test_css.py"] = "tests the old stylesheet, deleted with it by 06-21"
    return files


OLD_MARKUP_FILES = _old_markup_files()

# Samples for the guard's own tests, built so this file never holds the pattern as one
# literal: a class attribute and an f-string that writes one.
SAMPLE_ATTRIBUTE = "cla" + 'ss="box"'
SAMPLE_DOCSTRING = '"""A sample web test module."""'


def _tokens(source: str) -> list[tokenize.TokenInfo]:
    return list(tokenize.generate_tokens(io.StringIO(source).readline))


def class_literals(source: str) -> list[tuple[int, str]]:
    """(line, token text) of each string-literal token that holds a class attribute."""
    return [
        (token.start[0], token.string)
        for token in _tokens(source)
        if token.type in LITERAL_TOKENS and CLASS_ATTRIBUTE.search(token.string)
    ]


def is_pending(source: str) -> bool:
    """The file carries the exact pending-migration marker line."""
    return MARKER in source.splitlines()


def violations(source: str) -> list[tuple[int, str]]:
    """The class-attribute literals the guard reports; none in a pending file."""
    return [] if is_pending(source) else class_literals(source)


def class_lookups(source: str) -> list[tuple[int, str]]:
    """(line, text) of each lookup by class: a ``class_`` name, a ``"class"`` string, or a
    class selector in a string passed straight to ``.select(`` / ``.select_one(``."""
    tokens = [
        token
        for token in _tokens(source)
        if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE)
    ]
    found: list[tuple[int, str]] = []
    for index, token in enumerate(tokens):
        if token.type == tokenize.NAME and token.string == "class_":
            found.append((token.start[0], token.string))
        if token.type == tokenize.STRING and ast.literal_eval(token.string) == "class":
            found.append((token.start[0], token.string))
        called = (
            token.type == tokenize.NAME
            and token.string in SELECT_CALLS
            and index > 0
            and tokens[index - 1].string == "."
            and index + 2 < len(tokens)
            and tokens[index + 1].string == "("
        )
        if called and tokens[index + 2].type == tokenize.STRING:
            selector = str(ast.literal_eval(tokens[index + 2].string))
            bare = re.sub(r"\[[^\]]*\]|\"[^\"]*\"|'[^']*'", "", selector)
            if CLASS_SELECTOR.search(bare):
                found.append((tokens[index + 2].start[0], tokens[index + 2].string))
    return found


def class_couplings(source: str, *, old_names: bool = True) -> list[tuple[int, str]]:
    """(line, rule) of each other coupling to a class: the ``class_`` keyword, a class
    selector in a literal passed to ``.select(``/``.select_one(``, an old class name."""
    return []


def _sample(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_class_guard_flags_unmarked_files() -> None:
    page_line = f"PAGE = '<p {SAMPLE_ATTRIBUTE}>OK</p>'"
    fstring_line = "ROW = f'<td " + SAMPLE_ATTRIBUTE + ">{value}</td>'"
    unmarked = _sample(SAMPLE_DOCSTRING, "", page_line, fstring_line)

    # Failure: a plain string and an f-string's literal text, each with its line.
    assert violations(unmarked) == [
        (3, page_line.removeprefix("PAGE = ")),
        (4, "<td " + SAMPLE_ATTRIBUTE + ">"),
    ]
    # Expected: the same file with the marker line after its docstring is accepted.
    marked = _sample(SAMPLE_DOCSTRING, "", MARKER, "", page_line, fstring_line)
    assert is_pending(marked)
    assert violations(marked) == []
    # A near miss of the marker (other text on the line, or no space) does not count.
    for near in (MARKER + " (06-15)", MARKER.replace("# ", "#"), "    " + MARKER):
        assert violations(_sample(SAMPLE_DOCSTRING, near, page_line)) != [], near
    # Edge: the attribute only in a comment and spelled by code tokens is never flagged,
    # though a raw text search finds both; other words ending in "class=" are not it.
    comment_line = "# the old page had <p " + SAMPLE_ATTRIBUTE + ">"
    code_line = "node.cla" + "ss = 'box'"
    words = "WORDS = ['k" + "lass=1', 'subcla" + "ss=2', 'cla" + "ss_=3']"
    edge = _sample(SAMPLE_DOCSTRING, comment_line, code_line, words)
    assert CLASS_ATTRIBUTE.search(edge)
    assert violations(edge) == []


def test_no_class_assertions_outside_pending_files() -> None:
    files = sorted(WEB_TESTS.rglob("*.py"))
    assert WEB_TESTS / "test_class_guard.py" in files
    found = {
        path.relative_to(WEB_TESTS).as_posix(): lines
        for path in files
        if (lines := violations(path.read_text(encoding="utf-8")))
    }

    assert found == {}, "class attributes in string literals of unmarked files: " + "; ".join(
        f"{name} lines {[line for line, _ in lines]}" for name, lines in found.items()
    )


def test_class_guard_finished() -> None:
    files = sorted(WEB_TESTS.rglob("*.py"))

    # Expected: the migration is over, so no web test file carries the exact marker line.
    # This file holds the marker text (a constant and its docstring) and is not pending.
    assert [path.name for path in files if is_pending(path.read_text(encoding="utf-8"))] == []
    assert MARKER in (WEB_TESTS / "test_class_guard.py").read_text(encoding="utf-8")
    # Failure: a file with the marker line would be skipped, and is listed here.
    assert is_pending(_sample(SAMPLE_DOCSTRING, "", MARKER, "", "x = 1"))
    # Edge: the marker text inside a string or after code is not the marker line.
    assert not is_pending(_sample(SAMPLE_DOCSTRING, f"TEXT = {MARKER!r}", "x = 1  " + MARKER))


def test_class_coupling_rules() -> None:
    old = OLD_CLASSES[3]
    block = OLD_BLOCKS[1]
    bad = _sample(
        "soup.find_all('p', " + "class_" + "='box')",
        "page.select('main p.box')",
        "page.select_one(f'[data-id=\"{n}\"] .box')",
        "assert '" + old + "' in html",
        "HTML = '<p data-x=\"" + block + "\">'",
        "NAME = '" + block + "'",
    )

    # Failure: each coupling is found, with its line and rule.
    assert class_couplings(bad) == [
        (1, "class_ keyword"),
        (2, "class selector"),
        (3, "class selector"),
        (4, "old class name"),
        (5, "old class name"),
        (6, "old class name"),
    ]
    assert class_couplings(bad, old_names=False) == [
        (1, "class_ keyword"),
        (2, "class selector"),
        (3, "class selector"),
    ]
    # Expected: hooks, attribute selectors with dots, dotted paths, words that hold a
    # block name and plain docstring prose are not couplings.
    good = _sample(
        '"""Each flash, toast or legacy ' + block + ' alike."""',
        "page.select('[data-relative]')",
        "page.select(f'[data-relative=\"{stamp}\"]')",
        "soup.select('link[href$=\".css\"]')",
        "builder = 'html.parser'",
        "NAME = '" + block + "s'",
        "TESTID = 'status-panel'",
    )
    assert class_couplings(good) == []
    # Edge: a selector held in a variable cannot be read statically and is not flagged.
    assert class_couplings(_sample("page.select(selector)")) == []


def test_no_class_coupling_in_web_tests() -> None:
    files = sorted(WEB_TESTS.rglob("*.py"))
    found = {
        path.name: lines
        for path in files
        if (
            lines := class_couplings(
                path.read_text(encoding="utf-8"), old_names=path.name not in OLD_MARKUP_FILES
            )
        )
    }

    # Expected: no web test reaches a class another way, and only the listed files name
    # an old class, each for its stated reason.
    assert found == {}, found
    assert set(OLD_MARKUP_FILES) <= {path.name for path in files}


def test_pages_has_no_class_lookup() -> None:
    # Expected: pages.py looks elements up by id, data-testid, role, ARIA and tag only.
    assert class_lookups((WEB_TESTS / "pages.py").read_text(encoding="utf-8")) == []
    # Failure: each way to look up by class is found.
    bad = _sample(
        "soup.find_all('p', class_='box')",
        "soup.find_all(attrs={'class': 'box'})",
        "soup.select('main p.box')",
        "soup.select_one('.messages > p')",
    )
    assert [line for line, _ in class_lookups(bad)] == [1, 2, 3, 4]
    # Edge: attribute selectors whose values hold dots, and a dotted module path, are not.
    good = _sample(
        'soup.select(\'main p[role="status"], main p[role="alert"]\')',
        "soup.select('link[href$=\".css\"]')",
        "soup.select_one(\"script[src='/static/web/admin.js']\")",
        "builder = 'html.parser'",
    )
    assert class_lookups(good) == []
