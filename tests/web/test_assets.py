"""The built frontend assets and the css build stage (UI-13, UI-02, D6-07, KD8).

TEST-STRATEGY §7.2 and 06-RESEARCH Pitfalls 7 and 13:

- The Docker css stage builds powermon/web/assets/css/app.css with the Tailwind CSS v4.3.3
  standalone binary, ADDed with --checksum=sha256 equal to the approved vendor manifest
  (INV-26). The built file is copied in after the source and before collectstatic, so the
  manifest always has it. No Tailwind binary and no Node reach the app images.
- With DEBUG off, {% static %} resolves the built stylesheet and admin.js to manifest-hashed
  names; an unknown name raises.
- The built CSS is fully processed: no Tailwind directive left, only relative or data: url()
  values, no source map. It has the dark and the system-dark branches, every light raw token
  and the num and bg-hatch utilities, whether or not a page uses them yet.
- In the entry, the [data-theme=dark] block and the system-in-dark block are identical, and
  every themed light token has a dark value (UI-02).
- admin.js applies the stored rail flag first; the button partial renders a link or a button
  with data-variant and never the disabled attribute.

The tests read the image they run in: the dev target is built from the same base stage as
production, after the css stage and collectstatic.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.template.loader import render_to_string
from django.test.html import Element, parse_html

BASE_DIR = Path(settings.BASE_DIR)
ENTRY = BASE_DIR / "powermon" / "web" / "assets" / "css" / "app.css"
ADMIN_JS = BASE_DIR / "powermon" / "web" / "static" / "web" / "admin.js"
DOCKERFILE = BASE_DIR / "Dockerfile"
VENDOR_MANIFEST = BASE_DIR / "powermon" / "web" / "assets" / "vendor-manifest.json"
BUILT_CSS_COPY = "COPY --from=css /out/app.css powermon/web/static/web/build/app.css"
CSS_BUILD = "RUN tailwindcss -i powermon/web/assets/css/app.css -o /out/app.css --minify"
# Tokens that follow the pointer, not the theme: they have no dark value.
DENSITY_TOKENS = frozenset(
    {"--tb-h", "--sb-w", "--sb-rail-w", "--control-h", "--control-h-sm", "--nav-item-h", "--row-h"}
)
# Directives only Tailwind understands; the built file must have none of them left.
TAILWIND_DIRECTIVE = re.compile(
    r"@(tailwind|import|source|theme|plugin|custom-variant|utility|variant|apply|reference|config)\b"
)
TAILWIND_ADD = re.compile(
    r"ADD --chmod=755 --checksum=sha256:([0-9a-f]{64}) (\S+) /usr/local/bin/tailwindcss"
)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_URL = re.compile(r"url\(\s*([^)]*)\)", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
BUTTON = "partials/_button.html"


# CSS helpers


def _built_css() -> str:
    """The stylesheet collectstatic stored, read through the manifest storage."""
    with staticfiles_storage.open(staticfiles_storage.stored_name("web/build/app.css")) as handle:
        data: bytes = handle.read()
    return data.decode("utf-8")


def _compact(css: str) -> str:
    """``css`` without comments, whitespace or quotes, lowercased (the minifier's forms)."""
    bare = re.sub(r"\s+", "", _CSS_COMMENT.sub("", css)).lower()
    return bare.replace('"', "").replace("'", "")


def _top_level_blocks(css: str) -> list[tuple[str, str]]:
    """(prelude, body) of every top-level block of ``css``; statements are skipped."""
    blocks: list[tuple[str, str]] = []
    depth = 0
    start = body_start = 0
    prelude = ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                prelude = " ".join(css[start:index].split())
                body_start = index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced '}}' at {index}")
            if depth == 0:
                blocks.append((prelude, css[body_start:index]))
                start = index + 1
        elif char == ";" and depth == 0:
            start = index + 1
    if depth:
        raise ValueError("unclosed block")
    return blocks


def _only(blocks: list[tuple[str, str]], prelude: str) -> str:
    """The body of the one block whose prelude is exactly ``prelude``."""
    bodies = [body for name, body in blocks if name == prelude]
    assert len(bodies) == 1, (prelude, len(bodies))
    return bodies[0]


def _declarations(body: str) -> dict[str, str]:
    """``property: value`` pairs of a flat block body; a repeated property is an error."""
    found: dict[str, str] = {}
    for raw in body.split(";"):
        if not raw.strip():
            continue
        name, colon, value = raw.partition(":")
        if not colon:
            raise ValueError(f"not a declaration: {raw!r}")
        name = name.strip()
        if name in found:
            raise ValueError(f"{name} is declared twice")
        found[name] = " ".join(value.split())
    return found


def _entry_blocks() -> list[tuple[str, str]]:
    return _top_level_blocks(_CSS_COMMENT.sub("", ENTRY.read_text(encoding="utf-8")))


def _url_values(css: str) -> list[str]:
    """Every url() argument, quotes and surrounding whitespace removed."""
    return [match.strip().strip("'\"") for match in _URL.findall(css)]


def _relative_or_data(url: str) -> bool:
    """True for a data: URL or a path relative to the stylesheet (no scheme, no leading /)."""
    if url.lower().startswith("data:"):
        return True
    return not (_SCHEME.match(url) or url.startswith("/"))


# Dockerfile helpers


def _instructions() -> list[str]:
    """The Dockerfile's instructions: continuation lines joined, whitespace collapsed, comments
    dropped."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    lines = (" ".join(line.split()) for line in joined.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _stages() -> dict[str, tuple[str, list[str]]]:
    """Stage name -> (the image or stage it is FROM, its instructions)."""
    stages: dict[str, tuple[str, list[str]]] = {}
    current: list[str] | None = None
    for instruction in _instructions():
        match = re.fullmatch(r"FROM (\S+) AS (\S+)", instruction)
        if match:
            current = []
            stages[match.group(2)] = (match.group(1), current)
        elif current is not None:
            current.append(instruction)
    return stages


def _ancestry(stages: dict[str, tuple[str, list[str]]], name: str) -> list[str]:
    chain = []
    while name in stages:
        chain.append(name)
        name = stages[name][0]
    return chain


# Hashed static names (UI-13, Pitfall 7)


@pytest.mark.parametrize(
    ("name", "pattern"),
    [
        ("web/build/app.css", r"web/build/app\.[0-9a-f]{12}\.css"),
        ("web/admin.js", r"web/admin\.[0-9a-f]{12}\.js"),
    ],
)
def test_UI13_built_css_and_admin_js_have_hashed_names(name: str, pattern: str) -> None:
    assert re.fullmatch(pattern, staticfiles_storage.stored_name(name))


def test_UI13_unknown_static_name_raises() -> None:
    with pytest.raises(ValueError, match="Missing staticfiles manifest entry"):
        staticfiles_storage.stored_name("web/build/missing.css")


# The built stylesheet (UI-13, UI-02, Pitfall 13)


def test_UI13_built_css_is_fully_processed() -> None:
    css = _built_css()
    bare = _CSS_COMMENT.sub("", css)

    assert TAILWIND_DIRECTIVE.findall(bare) == []
    assert [url for url in _url_values(bare) if not _relative_or_data(url)] == []
    assert "sourcemappingurl" not in css.lower()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("../fonts/inter-latin-wght-normal.0123456789ab.woff2", True),
        ("data:image/svg+xml,%3csvg%3e%3c/svg%3e", True),
        ("DATA:image/png;base64,AAAA", True),
        ("https://cdn.example/x.woff2", False),
        ("//cdn.example/x.woff2", False),
        ("/static/web/fonts/x.woff2", False),
        ("http:x.woff2", False),
    ],
)
def test_UI13_url_rule(url: str, allowed: bool) -> None:
    assert _relative_or_data(url) is allowed
    assert _url_values(f"a{{b:url( '{url}' )}}") == [url]


def test_UI02_built_css_has_the_dark_and_system_dark_branches() -> None:
    compact = _compact(_built_css())

    assert "[data-theme=dark]" in compact
    assert "[data-theme=system]" in compact
    assert "prefers-color-scheme:dark" in compact


def test_UI13_built_css_carries_the_design_before_any_page_uses_it() -> None:
    compact = _compact(_built_css())
    light = _declarations(_only(_entry_blocks(), ":root"))

    assert [name for name in light if f"{name.lower()}:" not in compact] == []
    # Safelisted in the entry, so they exist even before a template uses them.
    assert ".num{" in compact
    assert ".bg-hatch{" in compact
    # num reads the mono stack through the theme variable, which must be emitted with it.
    assert "--font-mono:" in compact


# The entry's tokens (UI-02)


def test_UI02_dark_and_system_dark_blocks_are_identical() -> None:
    blocks = _entry_blocks()
    light = _declarations(_only(blocks, ":root"))
    dark = _declarations(_only(blocks, "[data-theme=dark]"))
    media = _top_level_blocks(_only(blocks, "@media (prefers-color-scheme: dark)"))
    system_dark = _declarations(_only(media, "[data-theme=system]"))

    assert dark == system_dark
    assert dark["color-scheme"] == "dark"
    # Every themed light token has a dark value, and the dark block invents none.
    assert set(dark) == set(light) - DENSITY_TOKENS
    assert _declarations(_only(blocks, "[data-theme=system]")) == {"color-scheme": "light dark"}
    assert _declarations(_only(blocks, "[data-theme=light]")) == {"color-scheme": "light"}


def test_UI02_entry_starts_with_tailwind_and_scans_only_the_templates_and_admin_js() -> None:
    lines = ENTRY.read_text(encoding="utf-8").splitlines()
    sources = [line for line in lines if line.startswith("@source ") and "inline(" not in line]

    assert lines[0] == '@import "tailwindcss" source(none);'
    assert sources == ['@source "../../templates";', '@source "../../static/web/admin.js";']


def test_css_parsers_reject_broken_input() -> None:
    # Failure cases of the helpers the token tests rely on.
    with pytest.raises(ValueError, match="unclosed"):
        _top_level_blocks("a { b {")
    with pytest.raises(ValueError, match="unbalanced"):
        _top_level_blocks("a { } }")
    with pytest.raises(ValueError, match="declared twice"):
        _declarations("--fg: #000; --fg: #fff")
    with pytest.raises(AssertionError):
        _only(_top_level_blocks(":root { --a: 1 }"), "[data-theme=dark]")
    # Edge: a nested block is part of its parent's body, not a top-level block.
    assert _top_level_blocks("@media x { a { --b: 1 } } c { }") == [
        ("@media x", " a { --b: 1 } "),
        ("c", " "),
    ]


# The Dockerfile css stage (UI-13, T-06-17)


def test_UI13_css_stage_output_lands_before_collectstatic() -> None:
    stages = _stages()
    base_from, steps = stages["base"]

    copy_source = steps.index("COPY . /app")
    copy_css = steps.index(BUILT_CSS_COPY)
    collect = next(i for i, step in enumerate(steps) if "collectstatic" in step)

    assert base_from == "uvtool"
    assert steps[collect].startswith("RUN ")
    assert copy_source < copy_css < collect


def test_UI13_css_stage_builds_the_entry_from_its_sources() -> None:
    css_from, steps = _stages()["css"]

    assert css_from == "tailwind-${TARGETARCH}"
    assert steps[-1] == CSS_BUILD
    # The stage copies every path the entry scans, so the build sees every class.
    copied = [step.split()[1] for step in steps if step.startswith("COPY ")]
    for line in ENTRY.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r'@source "([^"]+)";', line)
        if match:
            resolved = (ENTRY.parent / match.group(1)).resolve()
            source = resolved.relative_to(BASE_DIR.resolve()).as_posix()
            assert any(source == path or source.startswith(path + "/") for path in copied), source


def test_UI13_no_tailwind_or_node_in_the_app_images() -> None:
    stages = _stages()
    build_only = {"css", "tailwind-amd64", "tailwind-arm64"}

    for image in ("base", "dev", "runtime"):
        assert build_only.isdisjoint(_ancestry(stages, image)), image
    copies = [step for _, steps in stages.values() for step in steps if "--from=css" in step]
    assert copies == [BUILT_CSS_COPY]
    # This test runs in the dev image itself: neither tool is in it.
    assert shutil.which("tailwindcss") is None
    assert shutil.which("node") is None
    assert not Path("/usr/local/bin/tailwindcss").exists()


def test_UI13_tailwind_checksums_equal_the_vendor_manifest() -> None:
    manifest = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    stages = _stages()

    for arch in ("amd64", "arm64"):
        entry = entries[f"Dockerfile#tailwind-{arch}"]
        base, steps = stages[f"tailwind-{arch}"]
        match = TAILWIND_ADD.fullmatch(steps[0]) if len(steps) == 1 else None
        assert base == "uvtool", arch
        assert match, steps
        assert match.groups() == (entry["sha256"], entry["source_url"]), arch
        assert (entry["name"], entry["version"]) == ("tailwindcss", "4.3.3")
    # No other checksum hides anywhere in the file.
    pinned = set(re.findall(r"--checksum=sha256:([0-9a-f]{64})", DOCKERFILE.read_text()))
    assert pinned == {
        entries[f"Dockerfile#tailwind-{arch}"]["sha256"] for arch in ("amd64", "arm64")
    }


# admin.js (UI-01)


def test_UI01_admin_js_applies_the_rail_flag_first() -> None:
    text = ADMIN_JS.read_text(encoding="utf-8")
    code = " ".join(re.sub(r"//[^\n]*", "", _CSS_COMMENT.sub("", text)).split())

    # One strict-mode IIFE whose first statement copies the stored flag to <html>.
    assert code.startswith(
        '(function () { "use strict"; try { if (window.localStorage.getItem('
        '"powermon.sidebar.rail") === "1") { document.documentElement.setAttribute('
        '"data-rail", "collapsed"); } } catch (error) {'
    )
    assert code.endswith("})();")


# The button partial (06-UI-SPEC Components > Button)


def _render_button(**context: Any) -> Element:
    element = parse_html(render_to_string(BUTTON, context))
    assert isinstance(element, Element)
    return element


def _attrs(element: Element) -> dict[str, str | None]:
    return dict(element.attributes)


@pytest.mark.parametrize("variant", ["primary", "secondary", "ghost", "danger", "outline-danger"])
def test_button_partial_link_and_button(variant: str) -> None:
    link = _render_button(variant=variant, label="Back to locations", href="/", testid="back")
    button = _render_button(variant=variant, label="Save changes")

    assert link.name == "a"
    assert {k: v for k, v in _attrs(link).items() if k != "class"} == {
        "href": "/",
        "data-variant": variant,
        "data-testid": "back",
    }
    assert link.children == ["Back to locations"]
    assert button.name == "button"
    assert {k: v for k, v in _attrs(button).items() if k != "class"} == {
        "type": "submit",
        "data-variant": variant,
    }
    assert button.children == ["Save changes"]


def test_button_partial_options() -> None:
    # Edge: every optional attribute, a "0" value and a screen-reader suffix.
    button = _render_button(
        variant="secondary",
        label="Turn off",
        type="button",
        name="value",
        value="0",
        pending_label="Saving…",
        sr_suffix="maintenance",
        testid="switch",
    )

    attrs = _attrs(button)
    assert (attrs["type"], attrs["name"], attrs["value"]) == ("button", "value", "0")
    assert attrs["data-pending-label"] == "Saving…"
    assert attrs["data-testid"] == "switch"
    assert "disabled" not in attrs
    suffix = button.children[1]
    assert isinstance(suffix, Element)
    assert button.children[0] == "Turn off"
    assert (suffix.name, suffix.children) == ("span", ["maintenance"])


def test_button_partial_escapes_its_values() -> None:
    # Failure input: markup in a label or an attribute stays text (R1).
    html = render_to_string(
        BUTTON, {"variant": "ghost", "label": "<b>x</b>", "href": '/"><i>', "testid": "t"}
    )

    assert "<b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert '"><i>' not in html
