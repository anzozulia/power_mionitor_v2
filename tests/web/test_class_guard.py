"""The class-assertion ratchet (TEST-STRATEGY §5.4; 06-RESEARCH Pitfall 17).

No web test may assert on CSS classes. Tests read the rebuilt pages through the 06-UI-SPEC
hooks (ids, ``data-testid``, roles, ARIA attributes, tag names) and ``pages.py``, so a
class rename never breaks a behaviour test.

- The guard tokenises every ``tests/web/**/*.py`` file with the stdlib ``tokenize``
  module and fails on any string-literal token that contains a class attribute (the word
  ``class`` then ``=``, in any case): plain and bytes strings, and the literal text of
  f-strings and t-strings, which Python 3.12+ splits into their own tokens. Comments and
  code tokens are never flagged.
- A file that still asserts on classes carries the exact line
  ``# class-guard: pending migration`` right after its module docstring, and the guard
  skips it. Each page plan removes the marker from the files it migrates (the owners are
  in the 06-09 plan's interfaces and SUMMARY), and 06-20 asserts that no marker is left.
  A new file never gets the marker, so a new class-coupled assertion fails here.
- ``pages.py`` offers no lookup by class: no ``class_`` keyword, no ``"class"`` attribute
  key and no class selector in a ``select`` call.

This file builds its samples, and the attribute it looks for, by concatenation, so it
never holds the pattern as one literal and passes its own scan without a marker.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

WEB_TESTS = Path(__file__).resolve().parent
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
# A class selector: a dot then a name start, outside [attribute] parts and quotes.
CLASS_SELECTOR = re.compile(r"\.[A-Za-z_-]")
SELECT_CALLS = frozenset({"select", "select_one"})

# Samples for the guard's own tests, built so this file never holds the pattern as one
# literal: a class attribute and an f-string that writes one.
SAMPLE_ATTRIBUTE = "cla" + 'ss="callout"'
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
    code_line = "node.cla" + "ss = 'callout'"
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


def test_pages_has_no_class_lookup() -> None:
    # Expected: pages.py looks elements up by id, data-testid, role, ARIA and tag only.
    assert class_lookups((WEB_TESTS / "pages.py").read_text(encoding="utf-8")) == []
    # Failure: each way to look up by class is found.
    bad = _sample(
        "soup.find_all('p', class_='callout')",
        "soup.find_all(attrs={'class': 'callout'})",
        "soup.select('main p.callout')",
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
