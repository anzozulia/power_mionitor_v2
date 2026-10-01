"""The admin stylesheet's cascade: an invalid input or select shows the danger border.

UI-SPEC › Inputs and selects says "invalid: border --color-danger plus aria-invalid="true"",
and its Color table lists "the border of an invalid input" as a use of the danger colour.
Django 5.2's BoundField adds aria-invalid="true" to every visible widget whose field has
errors, so app.css alone decides whether the admin sees the red border.

There is no browser here. A small parser reads app.css into rules, computes selector
specificity (Selectors Level 4) and resolves which declaration colours each border side of
an element at rest: the more specific selector wins, then the later rule, then the later
declaration. Rules inside @media count as if their condition held. That model is enough
for this flat, hand-written file. Constructs it does not model (!important, unknown
pseudo-classes and attribute operators, statement at-rules, multi-value border-color,
logical border properties) raise instead of being guessed.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from django.conf import settings

CSS_PATH = Path(settings.BASE_DIR) / "powermon" / "web" / "static" / "web" / "app.css"

DANGER = "var(--color-danger)"
CONTROL = "var(--color-control-border)"
# The initial border colour, kept by a side that no rule colours.
UNSET = "currentcolor"
SIDES = ("top", "right", "bottom", "left")
# UI-SPEC Spacing › Exceptions: the only px literals allowed outside :root.
ALLOWED_PX = {"1px", "2px", "4px", "8px", "44px", "400px", "560px", "640px", "960px"}

Token = tuple[str, str, str]  # (kind, name, argument)

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_NAME = re.compile(r"-?[A-Za-z_][-\w]*")
_ATTRIBUTE = re.compile(r"""^\s*([-\w]+)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([-\w]+))\s*)?$""")
_GROUPING_AT_RULES = ("@media", "@supports")
_LEGACY_PSEUDO_ELEMENTS = {"before", "after", "first-line", "first-letter"}
# User-action and history states: an element at rest matches none of them.
_STATES = {"hover", "active", "focus", "focus-visible", "focus-within", "visited", "target"}
_WIDTH = re.compile(r"^(\d+(\.\d+)?(px|em|rem)?|thin|medium|thick)$")
_STYLES = set("none hidden dotted dashed solid double groove ridge inset outset".split())


@dataclass(frozen=True)
class Rule:
    order: int
    selectors: tuple[str, ...]
    declarations: tuple[tuple[str, str], ...]


@dataclass
class Element:
    """An element at rest (no hover, focus or other state), as a tag and its attributes."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)


# --- Parsing ------------------------------------------------------------------------------


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """Index of the bracket that closes the one at ``start``."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"unbalanced {opening!r} in {text!r}")


def split_selector_list(text: str) -> list[str]:
    """Split a selector list at its top-level commas."""
    parts: list[str] = []
    depth = start = 0
    for index, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    if depth != 0 or not all(parts):
        raise ValueError(f"malformed selector list: {text!r}")
    return parts


def _declarations(body: str) -> tuple[tuple[str, str], ...]:
    if "{" in body:
        raise ValueError(f"nested rules are not modelled: {body.strip()!r}")
    pairs = []
    for item in body.split(";"):
        if not item.strip():
            continue
        name, colon, value = item.partition(":")
        if not colon or not name.strip():
            raise ValueError(f"not a declaration: {item.strip()!r}")
        pairs.append((name.strip().lower(), " ".join(value.split())))
    return tuple(pairs)


def _collect(text: str, rules: list[Rule]) -> None:
    position = 0
    while (opening := text.find("{", position)) != -1:
        prelude = text[position:opening].strip()
        closing = _closing(text, opening, "{", "}")
        body = text[opening + 1 : closing]
        if prelude.startswith(_GROUPING_AT_RULES):
            _collect(body, rules)
        elif not prelude or prelude.startswith("@") or ";" in prelude or "}" in prelude:
            raise ValueError(f"not a style rule: {prelude!r}")
        else:
            selectors = tuple(split_selector_list(prelude))
            rules.append(Rule(len(rules), selectors, _declarations(body)))
        position = closing + 1
    if text[position:].strip():
        raise ValueError(f"text outside any rule: {text[position:].strip()!r}")


def parse_rules(css: str) -> list[Rule]:
    """The style rules of ``css`` in source order, including those inside @media."""
    rules: list[Rule] = []
    _collect(_COMMENT.sub("", css), rules)
    return rules


# --- Selectors ----------------------------------------------------------------------------


def _tokens(selector: str) -> list[Token]:
    """Split one complex selector into simple selectors and combinators."""
    text = selector.strip()
    tokens: list[Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char in ">+~":
            end = index
            while end < len(text) and (text[end].isspace() or text[end] in ">+~"):
                end += 1
            combinator = "".join(text[index:end].split())
            if combinator not in ("", ">", "+", "~"):
                raise ValueError(f"bad combinator {combinator!r} in {selector!r}")
            tokens.append(("combinator", combinator or " ", ""))
            index = end
        elif char == "*":
            tokens.append(("universal", "*", ""))
            index += 1
        elif char == "[":
            end = text.find("]", index)
            if end == -1:
                raise ValueError(f"unterminated attribute selector in {selector!r}")
            tokens.append(("attribute", text[index + 1 : end], ""))
            index = end + 1
        elif char == ":":
            double = text.startswith("::", index)
            name = _NAME.match(text, index + (2 if double else 1))
            if name is None:
                raise ValueError(f"bad pseudo selector in {selector!r}")
            index = name.end()
            argument = ""
            if index < len(text) and text[index] == "(":
                end = _closing(text, index, "(", ")")
                argument, index = text[index + 1 : end], end + 1
            lowered = name.group().lower()
            if double or lowered in _LEGACY_PSEUDO_ELEMENTS:
                tokens.append(("pseudo-element", lowered, argument))
            else:
                tokens.append(("pseudo-class", lowered, argument))
        else:
            prefix = char if char in "#." else ""
            name = _NAME.match(text, index + len(prefix))
            if name is None:
                raise ValueError(f"unexpected {char!r} in selector {selector!r}")
            kind = {"#": "id", ".": "class", "": "type"}[prefix]
            tokens.append((kind, name.group() if prefix else name.group().lower(), ""))
            index = name.end()
    if not tokens or tokens[0][0] == "combinator" or tokens[-1][0] == "combinator":
        raise ValueError(f"empty selector or dangling combinator: {selector!r}")
    return tokens


def specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes + attributes + pseudo-classes, types + pseudo-elements), Selectors 4.

    :not(), :is() and :has() count as their most specific argument; :where() counts nothing.
    """
    ids = classes = types = 0
    for kind, name, argument in _tokens(selector):
        if kind == "id":
            ids += 1
        elif kind in ("class", "attribute"):
            classes += 1
        elif kind in ("type", "pseudo-element"):
            types += 1
        elif kind == "pseudo-class" and name in ("not", "is", "has"):
            best = max(specificity(part) for part in split_selector_list(argument))
            ids, classes, types = ids + best[0], classes + best[1], types + best[2]
        elif kind == "pseudo-class" and name != "where":
            classes += 1
    return ids, classes, types


def _attribute_matches(spec: str, element: Element) -> bool:
    match = _ATTRIBUTE.match(spec)
    if match is None:
        raise ValueError(f"attribute selector not modelled: [{spec}]")
    name, *values = match.groups()
    if name not in element.attrs:
        return False
    expected = next((value for value in values if value is not None), None)
    return expected is None or element.attrs[name] == expected


def _simple_matches(token: Token, element: Element) -> bool:
    kind, name, argument = token
    if kind == "type":
        return name == element.tag
    if kind == "universal":
        return True
    if kind == "id":
        return element.attrs.get("id") == name
    if kind == "class":
        return name in element.attrs.get("class", "").split()
    if kind == "attribute":
        return _attribute_matches(name, element)
    if kind == "pseudo-element":
        # It styles a generated box, not the element itself.
        return False
    if name == "not":
        return not any(matches(part, element) for part in split_selector_list(argument))
    if name in ("is", "where"):
        return any(matches(part, element) for part in split_selector_list(argument))
    if name in _STATES:
        return False
    raise ValueError(f"pseudo-class not modelled: :{name}")


def matches(selector: str, element: Element) -> bool:
    """Whether ``selector`` can match ``element`` at rest.

    Only the subject (rightmost) compound is checked and ancestors are assumed to fit, so a
    descendant rule that could win somewhere on the page is never hidden from the cascade.
    """
    tokens = _tokens(selector)
    combinators = [index for index, token in enumerate(tokens) if token[0] == "combinator"]
    subject = tokens[combinators[-1] + 1 :] if combinators else tokens
    return all(_simple_matches(token, element) for token in subject)


# --- Cascade ------------------------------------------------------------------------------


def _shorthand_colour(value: str) -> str:
    """The colour of a ``border``-style shorthand; an omitted colour means currentcolor."""
    colours = [part for part in value.split() if not _WIDTH.match(part) and part not in _STYLES]
    if len(colours) > 1:
        raise ValueError(f"border shorthand not modelled: {value!r}")
    return colours[0] if colours else UNSET


def border_sides(name: str, value: str) -> dict[str, str]:
    """The border sides one declaration colours, mapped to the colour it gives them."""
    if not name.startswith("border"):
        return {}
    if "!important" in value:
        raise ValueError(f"!important is not modelled: {name}: {value}")
    if name.startswith(("border-block", "border-inline")):
        raise ValueError(f"logical border properties are not modelled: {name}")
    if name == "border":
        return dict.fromkeys(SIDES, _shorthand_colour(value))
    if name == "border-color":
        if len(value.split()) != 1:
            raise ValueError(f"multi-value border-color is not modelled: {value!r}")
        return dict.fromkeys(SIDES, value)
    for side in SIDES:
        if name == f"border-{side}":
            return {side: _shorthand_colour(value)}
        if name == f"border-{side}-color":
            return {side: value}
    return {}


def border_colours(rules: list[Rule], element: Element) -> dict[str, str]:
    """The colour each border side of ``element`` gets from ``rules``."""
    winners: dict[str, tuple[tuple[tuple[int, int, int], int, int], str]] = {}
    for rule in rules:
        for position, (name, value) in enumerate(rule.declarations):
            sides = border_sides(name, value)
            if not sides:
                continue
            for selector in rule.selectors:
                if not matches(selector, element):
                    continue
                rank = (specificity(selector), rule.order, position)
                for side, colour in sides.items():
                    if side not in winners or rank > winners[side][0]:
                        winners[side] = (rank, colour)
    return {side: winners[side][1] if side in winners else UNSET for side in SIDES}


def _colouring(rules: list[Rule], element: Element, colour: str) -> list[tuple[Rule, str]]:
    """Every (rule, selector) that matches ``element`` and gives a border side ``colour``."""
    return [
        (rule, selector)
        for rule in rules
        if any(colour in border_sides(name, value).values() for name, value in rule.declarations)
        for selector in rule.selectors
        if matches(selector, element)
    ]


# --- app.css ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_rules() -> list[Rule]:
    return parse_rules(CSS_PATH.read_text(encoding="utf-8"))


def _invalid(tag: str, **attrs: str) -> Element:
    return Element(tag, {**attrs, "aria-invalid": "true"})


# What Django 5.2 renders for a field with errors: the visible widget gets aria-invalid.
INVALID_CONTROLS = {
    "text": _invalid("input", type="text"),
    "number": _invalid("input", type="number"),
    "password": _invalid("input", type="password"),
    "url": _invalid("input", type="url"),
    "untyped": _invalid("input"),
    "select": _invalid("select"),
}
VALID_CONTROLS = {
    "text": Element("input", {"type": "text"}),
    "number": Element("input", {"type": "number"}),
    "aria-invalid-false": Element("input", {"type": "text", "aria-invalid": "false"}),
    "select": Element("select"),
}


@pytest.mark.parametrize("control", list(INVALID_CONTROLS.values()), ids=list(INVALID_CONTROLS))
def test_invalid_control_gets_the_danger_border(app_rules: list[Rule], control: Element) -> None:
    assert border_colours(app_rules, control) == dict.fromkeys(SIDES, DANGER)


@pytest.mark.parametrize("control", list(VALID_CONTROLS.values()), ids=list(VALID_CONTROLS))
def test_valid_control_keeps_the_control_border(app_rules: list[Rule], control: Element) -> None:
    assert border_colours(app_rules, control) == dict.fromkeys(SIDES, CONTROL)


@pytest.mark.parametrize("tag", ["input", "select"])
def test_invalid_rule_outranks_the_base_control_rule(app_rules: list[Rule], tag: str) -> None:
    # The danger rule must win on its own terms: it applies only to invalid controls, is at
    # least as specific as the rule that gives the control its grey border, and comes after.
    attrs = {"type": "text"} if tag == "input" else {}
    valid, invalid = Element(tag, attrs), _invalid(tag, **attrs)
    base = _colouring(app_rules, valid, CONTROL)
    danger = _colouring(app_rules, invalid, DANGER)
    assert base, f"no rule gives a valid {tag} the control border"
    assert danger, f"no rule gives an invalid {tag} the danger border"
    for rule, selector in danger:
        assert not matches(selector, valid), f"{selector!r} also colours a valid {tag}"
        for base_rule, base_selector in base:
            assert specificity(selector) >= specificity(base_selector), (
                f"{selector!r} {specificity(selector)} is less specific than "
                f"{base_selector!r} {specificity(base_selector)}"
            )
            assert rule.order > base_rule.order, f"{selector!r} comes before {base_selector!r}"


def test_stylesheet_keeps_the_ui_spec_file_rules() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    code = _COMMENT.sub("", css)
    outside_root = re.sub(r":root\s*\{[^}]*\}", "", code)
    assert len(css.splitlines()) <= 300
    assert not re.search(r"@import|@font-face|url\(", code)
    assert not re.findall(r"#[0-9A-Fa-f]{3,8}\b", outside_root)
    assert set(re.findall(r"\d+px", outside_root)) <= ALLOWED_PX


# --- The helpers themselves ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ('input:not([type="hidden"])', (0, 1, 1)),
        ('[aria-invalid="true"]', (0, 1, 0)),
        ('input:not([type="hidden"])[aria-invalid="true"]', (0, 2, 1)),
        (".site-nav a:hover", (0, 2, 1)),
        ("#id .c > p::before", (1, 1, 2)),
    ],
)
def test_specificity_counts_each_simple_selector(
    selector: str, expected: tuple[int, int, int]
) -> None:
    assert specificity(selector) == expected


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("*", (0, 0, 0)),
        ("*::before", (0, 0, 1)),
        ("a:before", (0, 0, 2)),
        (":where(#a, .b) p", (0, 0, 1)),
        (":is(.a, #b)", (1, 0, 0)),
    ],
)
def test_specificity_edge_selectors(selector: str, expected: tuple[int, int, int]) -> None:
    assert specificity(selector) == expected


@pytest.mark.parametrize(
    "selector", ["", "  ", 'input[type="hidden"', "input:not(.a", "a >", "a > + b", "a $ b"]
)
def test_specificity_rejects_malformed_selectors(selector: str) -> None:
    with pytest.raises(ValueError):
        specificity(selector)


def test_parse_rules_reads_rules_in_source_order() -> None:
    rules = parse_rules(
        "/* note { } */ a, b:not(.x, .y) { color: red; border: 0 }\n"
        "@media (width < 640px) { .c { margin: 0; } }"
    )
    assert [rule.selectors for rule in rules] == [("a", "b:not(.x, .y)"), (".c",)]
    assert rules[0].declarations == (("color", "red"), ("border", "0"))
    assert [rule.order for rule in rules] == [0, 1]


def test_parse_rules_edge_sheets() -> None:
    assert parse_rules("/* only a comment */\n") == []
    assert parse_rules("a { }") == [Rule(0, ("a",), ())]


@pytest.mark.parametrize(
    "css",
    [
        "a { color: red",
        "a { color: red } }",
        "@import url(x.css); a { color: red }",
        "@font-face { font-family: x }",
        "{ color: red }",
        "a { color }",
    ],
)
def test_parse_rules_rejects_what_it_does_not_model(css: str) -> None:
    with pytest.raises(ValueError):
        parse_rules(css)


INPUT = _invalid("input", type="text")


def test_cascade_later_rule_wins_a_specificity_tie() -> None:
    rules = parse_rules("input { border: 1px solid var(--a) } input { border-color: var(--b) }")
    assert border_colours(rules, INPUT) == dict.fromkeys(SIDES, "var(--b)")


def test_cascade_more_specific_earlier_rule_wins() -> None:
    # The audited defect in miniature: source order cannot beat higher specificity.
    rules = parse_rules(
        'input:not([type="hidden"]) { border: 1px solid var(--a) } '
        '[aria-invalid="true"] { border-color: var(--b) }'
    )
    assert border_colours(rules, INPUT) == dict.fromkeys(SIDES, "var(--a)")


def test_cascade_colours_only_the_sides_a_matching_rule_sets() -> None:
    rules = parse_rules("input { border-left: 4px solid var(--a) } .other { border: 0 }")
    assert border_colours(rules, INPUT) == {
        "top": UNSET,
        "right": UNSET,
        "bottom": UNSET,
        "left": "var(--a)",
    }


@pytest.mark.parametrize(
    "css",
    [
        "input { border-color: var(--a) !important }",
        "input:first-child { border: 0 }",
        '[type^="te"] { border: 0 }',
        "input { border-color: var(--a) var(--b) }",
        "input { border-inline-color: var(--a) }",
        "input { border: 1px var(--a) var(--b) }",
    ],
)
def test_cascade_rejects_what_it_does_not_model(css: str) -> None:
    with pytest.raises(ValueError):
        border_colours(parse_rules(css), INPUT)
