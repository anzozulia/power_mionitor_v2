"""The vendored frontend files are exactly the approved bytes (UI-13, INV-26, D6-02, D6-07).

``powermon/web/assets/vendor-manifest.json`` is the first-party record of every third-party
frontend file: name, exact version, source URL, sha256, byte size, SPDX licence and repo
path. The maintainer approved it at the 06-01 INV-26 legitimacy checkpoint on 2026-10-05
("Approved, lucide 1.48.0"); 06-06 copied the files in byte-identical after re-checking each
download against it. A swapped, edited, missing or unlisted file fails here.

Entries whose path starts with ``Dockerfile#`` are the Tailwind CLI binaries. They never
enter the repository: ``ADD --checksum`` verifies their bytes at build time, and
tests/web/test_assets.py checks that the Dockerfile's checksums equal these entries.
"""

import hashlib
import json
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage

ROOT = Path(settings.BASE_DIR)
MANIFEST = ROOT / "powermon" / "web" / "assets" / "vendor-manifest.json"
LICENCE_DIR = "powermon/web/static/web/vendor/LICENSES"
# The directories that may hold only manifest files. LICENSES/ is left out of the "no
# unlisted file" scan (TEST-STRATEGY 7.2); its five texts are manifest entries, so their
# presence and hashes are still checked.
VENDORED_DIRS = (
    "powermon/web/static/web/vendor",
    "powermon/web/static/web/fonts",
    "powermon/web/templates/icons",
)
FIELDS = {"name", "version", "kind", "source_url", "sha256", "bytes", "licence", "path"}
KINDS = {"binary", "script", "font", "icon", "licence"}
EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# 06-UI-SPEC "Registry Safety": the licence texts that ship, TailAdmin's MIT notice included.
LICENCE_FILES = {
    "alpinejs-MIT.txt",
    "inter-OFL-1.1.txt",
    "lucide-ISC.txt",
    "tailadmin-MIT.txt",
    "tailwindcss-MIT.txt",
}
# 06-UI-SPEC "Iconography": exactly these 44 Lucide icons, and no others.
ICON_NAMES = set(
    """
    bell-off calendar-days check chevron-down chevron-right circle-alert circle-check clock
    copy cpu ellipsis-vertical eye eye-off file-question-mark hourglass info key-round
    layout-grid loader-circle lock log-out map-pin maximize-2 menu message-square-warning
    monitor moon panel-left-close panel-left-open pencil plus refresh-cw rotate-ccw router
    send sun trash-2 triangle-alert user wifi-off wrench x zap zap-off
    """.split()
)
# The vendored static names later templates load through {% static %}.
STATIC_NAMES = (
    "web/vendor/alpine-csp-3.17.4.min.js",
    "web/fonts/inter-latin-wght-normal.woff2",
    "web/fonts/inter-cyrillic-wght-normal.woff2",
    "web/fonts/inter-latin-ext-wght-normal.woff2",
    "web/fonts/inter-cyrillic-ext-wght-normal.woff2",
)


def _entries() -> list[dict[str, Any]]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    entries: list[dict[str, Any]] = data["entries"]
    return entries


def _repo_entries() -> list[dict[str, Any]]:
    return [entry for entry in _entries() if not entry["path"].startswith("Dockerfile#")]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listed_paths(entries: Iterable[dict[str, Any]]) -> set[str]:
    """The repository paths the manifest lists: a set, so entry order never matters."""
    return {entry["path"] for entry in entries if not entry["path"].startswith("Dockerfile#")}


def _files_on_disk(root: Path) -> set[str]:
    """Every file under the vendored directories of ``root``, LICENSES/ left out."""
    found = set()
    for directory in VENDORED_DIRS:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and not relative.startswith(f"{LICENCE_DIR}/"):
                found.add(relative)
    return found


def _difference(root: Path, entries: Iterable[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """(missing, unlisted): listed paths with no file, and files the manifest does not list."""
    listed = _listed_paths(entries)
    missing = {path for path in listed if not (root / path).is_file()}
    return missing, _files_on_disk(root) - listed


def _hashed(name: str) -> str:
    """The pattern of the manifest-hashed static name: ``stem.<12 hex>.suffix``."""
    stem, _, suffix = name.rpartition(".")
    return rf"{re.escape(stem)}\.[0-9a-f]{{12}}\.{re.escape(suffix)}"


def test_vendored_files_match_the_manifest(tmp_path: Path) -> None:
    entries = _repo_entries()
    assert len(entries) == 54

    # Edge: the vendored directories hold exactly the listed files, in both directions.
    assert _difference(ROOT, entries) == (set(), set())

    # Expected: every listed repository file has exactly the approved bytes.
    actual = {
        entry["path"]: (_sha256(ROOT / entry["path"]), (ROOT / entry["path"]).stat().st_size)
        for entry in entries
    }
    approved = {entry["path"]: (entry["sha256"], entry["bytes"]) for entry in entries}
    assert actual == approved

    # Failure, on copies only (never the repository files): one flipped byte changes the
    # hash, and a missing or an extra file shows up in the comparison.
    zap = next(entry for entry in entries if entry["path"].endswith("/icons/zap.svg"))
    copy = tmp_path / "zap.svg"
    data = bytearray((ROOT / zap["path"]).read_bytes())
    data[-2] ^= 0x01
    copy.write_bytes(bytes(data))
    assert _sha256(copy) != zap["sha256"]

    checkout = tmp_path / "checkout"
    for directory in VENDORED_DIRS:
        shutil.copytree(ROOT / directory, checkout / directory)
    (checkout / zap["path"]).unlink()
    licence = f"{LICENCE_DIR}/lucide-ISC.txt"
    (checkout / licence).unlink()
    extra = "powermon/web/static/web/fonts/unlisted.woff2"
    (checkout / extra).write_bytes(b"not approved")
    assert _difference(checkout, entries) == ({zap["path"], licence}, {extra})


def test_manifest_licences_and_versions() -> None:
    entries = _entries()

    # Expected: complete, well-formed entries with exact versions (no ranges, no tags).
    for entry in entries:
        assert set(entry) == FIELDS, entry["path"]
        assert entry["kind"] in KINDS, entry["path"]
        assert EXACT_VERSION.fullmatch(entry["version"]), entry["path"]
        assert SHA256.fullmatch(entry["sha256"]), entry["path"]
        assert isinstance(entry["bytes"], int) and entry["bytes"] > 0, entry["path"]
        assert entry["source_url"].startswith("https://"), entry["path"]

    # Every licence text ships, and every other file's licence has its text: an entry of
    # kind "licence" with the same name, version and SPDX id, named *-<SPDX>.txt.
    texts = {
        (entry["name"], entry["version"], entry["licence"]): entry["path"]
        for entry in entries
        if entry["kind"] == "licence"
    }
    assert {path.rsplit("/", 1)[1] for path in texts.values()} == LICENCE_FILES
    for path in texts.values():
        assert path.startswith(f"{LICENCE_DIR}/")
        assert (ROOT / path).is_file(), path
    for entry in entries:
        key = (entry["name"], entry["version"], entry["licence"])
        assert key in texts, entry["path"]
        assert texts[key].endswith(f"-{entry['licence']}.txt"), entry["path"]

    # Exactly the 44 UI-SPEC icons, all at the one approved Lucide version: the version of
    # the shipped Lucide licence entry, which is also the version in each file's first line.
    icons = [entry for entry in entries if entry["kind"] == "icon"]
    assert len(icons) == 44
    filenames = {entry["path"].rsplit("/", 1)[1] for entry in icons}
    assert filenames == {f"{name}.svg" for name in ICON_NAMES}
    lucide = {entry["version"] for entry in entries if entry["name"] == "lucide-static"}
    assert len(lucide) == 1
    (version,) = lucide
    for entry in icons:
        filename = entry["path"].rsplit("/", 1)[1]
        assert entry["version"] == version
        assert entry["source_url"].endswith(f"/lucide-static@{version}/icons/{filename}")
        first_line = (ROOT / entry["path"]).read_text(encoding="utf-8").splitlines()[0]
        assert first_line == f"<!-- @license lucide-static v{version} - ISC -->"

    # Edge: entries are sorted by unique path, and the path comparison ignores order.
    paths = [entry["path"] for entry in entries]
    assert paths == sorted(paths)
    assert len(set(paths)) == len(paths)
    assert _listed_paths(reversed(entries)) == _listed_paths(entries)


def test_manifest_versions_reject_ranges() -> None:
    # Failure: the version check refuses everything that is not an exact x.y.z.
    for loose in ("^3.17.4", "~1.48.0", "1.48", "1.x", "latest", ">=4.3.3", "1.48.0-rc.1"):
        assert not EXACT_VERSION.fullmatch(loose), loose


@pytest.mark.parametrize("name", STATIC_NAMES)
def test_vendored_static_names_are_hashed(name: str) -> None:
    # With DEBUG off the image's staticfiles manifest maps each name to a hashed copy.
    assert settings.DEBUG is False
    assert re.fullmatch(_hashed(name), staticfiles_storage.stored_name(name))


def test_unknown_vendor_static_name_raises() -> None:
    with pytest.raises(ValueError, match="Missing staticfiles manifest entry"):
        staticfiles_storage.stored_name("web/vendor/alpine-csp-3.17.3.min.js")
