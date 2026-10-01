"""Device setup examples (HB-01, LOC-05; INV-24 #3, D-06, D-07).

Every example the setup page prints comes from ``powermon.locations.examples``. The
INV-24 test runs those exact strings with ``sh -c`` against a live server, with real curl,
GNU wget and BusyBox wget, and checks that each one records a heartbeat. The only change
to a cron line is dropping its five schedule fields, and a no-op ``sleep`` first on PATH
keeps the sub-minute offsets fast. A redirect would record nothing (curl ``-f`` treats a
3xx as success), so "state_version went up" is also the no-redirect check.

The live server URL uses 127.0.0.1: BusyBox wget resolves ``localhost`` to ``::1`` and
gets "Connection refused" (RESEARCH P-6).
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from powermon.engine.models import LocationState
from powermon.locations.examples import (
    cron_lines,
    curl_cmd,
    heartbeat_url,
    wget_busybox,
    wget_gnu,
)

KEY = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"
URL = "https://power.example.org/hb"
CURL = f'curl -fsS -m 10 -o /dev/null -H "Authorization: Bearer {KEY}" {URL}'
BEARER = f'-H "Authorization: Bearer {KEY}"'
# The periods the PITFALLS checklist names: sub-minute, one minute and multi-minute.
PERIODS = (10, 30, 60, 120)
COMMAND_TIMEOUT_S = 30


def _version(location: Any) -> int:
    version: int = LocationState.objects.get(pk=location.pk).state_version
    return version


def _sh(cmd: str, *path_dirs: Path) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` as cron and a device shell would: ``sh -c``, only PATH in the env."""
    path = os.pathsep.join([*(str(d) for d in path_dirs), os.environ["PATH"]])
    return subprocess.run(
        ["sh", "-c", cmd],
        env={"PATH": path},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def _sleep_shim(tmp_path: Path) -> Path:
    """A directory whose ``sleep`` returns at once, so ``sleep 50; curl ...`` runs fast."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    sleep = shim_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    return shim_dir


def _busybox_wget(tmp_path: Path) -> Path:
    """A directory where ``wget`` is BusyBox (the applet is picked by argv[0])."""
    busybox = shutil.which("busybox")
    assert busybox is not None, "the dev image must ship busybox for INV-24 #3"
    bb_dir = tmp_path / "busybox"
    bb_dir.mkdir()
    (bb_dir / "wget").symlink_to(busybox)
    return bb_dir


def _base_url(live_server: Any) -> str:
    url: str = live_server.url
    return url.replace("localhost", "127.0.0.1")


# INV-24 #3: every generated example, run verbatim, records a heartbeat


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("period_s", PERIODS)
def test_INV24_examples_run_verbatim(
    period_s: int, live_server: Any, location_factory: Any, tmp_path: Path
) -> None:
    location = location_factory(period_s=period_s)
    url = heartbeat_url(_base_url(live_server))
    key = location.device_key
    shim_dir = _sleep_shim(tmp_path)
    bb_dir = _busybox_wget(tmp_path)

    runs: list[tuple[str, str, tuple[Path, ...]]] = [
        ("curl multi-line", curl_cmd(url, key, multiline=True), ())
    ]
    for index, line in enumerate(cron_lines(url, key, period_s)):
        fields = line.split(None, 5)
        assert len(fields) == 6, f"cron line {index} has no five schedule fields: {line!r}"
        runs.append((f"cron line {index}", fields[5], (shim_dir,)))
    runs.append(("GNU wget", wget_gnu(url, key), ()))
    runs.append(("BusyBox wget", wget_busybox(url, key), (bb_dir,)))

    for label, cmd, path_dirs in runs:
        before = _version(location)
        result = _sh(cmd, *path_dirs)
        assert result.returncode == 0, f"{label} exited {result.returncode}: {result.stderr}"
        assert _version(location) > before, f"{label} recorded no heartbeat"


@pytest.mark.django_db(transaction=True)
def test_busybox_rejects_max_redirect(
    live_server: Any, location_factory: Any, tmp_path: Path
) -> None:
    # Why example d exists: the GNU line, run on BusyBox, fails before sending anything.
    location = location_factory()
    url = heartbeat_url(_base_url(live_server))
    bb_dir = _busybox_wget(tmp_path)
    # The wget on this PATH really is BusyBox.
    assert "BusyBox" in _sh("wget --help 2>&1", bb_dir).stdout

    before = _version(location)
    result = _sh(wget_gnu(url, location.device_key), bb_dir)

    assert result.returncode != 0
    assert "max-redirect" in result.stderr
    assert _version(location) == before


# The generators (pure)


def test_heartbeat_url_from_base_url() -> None:
    assert heartbeat_url("https://power.example.org") == URL
    assert heartbeat_url("https://power.example.org/") == URL
    assert heartbeat_url("http://localhost:8000") == "http://localhost:8000/hb"


@pytest.mark.parametrize(
    "base_url",
    ["", "power.example.org", "/hb", "ftp://power.example.org"],
    ids=["empty", "no-scheme", "path-only", "ftp"],
)
def test_heartbeat_url_rejects_a_base_url_that_is_not_absolute_http(base_url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        heartbeat_url(base_url)


def test_curl_cmd_shapes() -> None:
    assert curl_cmd(URL, KEY) == CURL
    assert curl_cmd(URL, KEY, multiline=True).split("\n") == [
        "curl -fsS -m 10 -o /dev/null \\",
        f'  -H "Authorization: Bearer {KEY}" \\',
        f"  {URL}",
    ]
    # The multi-line form is the same command, split with backslash-newline.
    assert curl_cmd(URL, KEY, multiline=True).replace(" \\\n  ", " ") == CURL


def test_wget_shapes() -> None:
    assert wget_gnu(URL, KEY) == f'wget -q -O /dev/null --max-redirect=0 "{URL}?key={KEY}"'
    assert wget_busybox(URL, KEY) == f'wget -q -O /dev/null "{URL}?key={KEY}"'


@pytest.mark.parametrize(
    ("period_s", "expected"),
    [
        (
            10,
            [f"* * * * * {CURL}"] + [f"* * * * * sleep {s}; {CURL}" for s in (10, 20, 30, 40, 50)],
        ),
        (30, [f"* * * * * {CURL}", f"* * * * * sleep 30; {CURL}"]),
        (59, [f"* * * * * {CURL}", f"* * * * * sleep 59; {CURL}"]),
        (60, [f"* * * * * {CURL}"]),
        (90, [f"* * * * * {CURL}"]),
        (120, [f"*/2 * * * * {CURL}"]),
        (3599, [f"*/59 * * * * {CURL}"]),
        (3600, [f"0 * * * * {CURL}"]),
    ],
)
def test_cron_lines_rules(period_s: int, expected: list[str]) -> None:
    assert cron_lines(URL, KEY, period_s) == expected


@pytest.mark.parametrize("period_s", [9, 3601, 0, -60])
def test_cron_lines_rejects_period_outside_10_3600(period_s: int) -> None:
    with pytest.raises(ValueError, match="10-3600"):
        cron_lines(URL, KEY, period_s)


@pytest.mark.parametrize("period_s", PERIODS)
def test_key_transport_per_example(period_s: int) -> None:
    header_examples = [
        curl_cmd(URL, KEY),
        curl_cmd(URL, KEY, multiline=True),
        *cron_lines(URL, KEY, period_s),
    ]
    for example in header_examples:
        assert BEARER in example
        assert "?key=" not in example
        assert "--max-redirect" not in example
        # A % in a crontab line starts stdin; keys and the URL never contain one (D-07).
        assert "%" not in example

    for example in (wget_gnu(URL, KEY), wget_busybox(URL, KEY)):
        assert f"?key={KEY}" in example
        assert "Authorization" not in example
    assert "--max-redirect=0" in wget_gnu(URL, KEY)
    assert "--max-redirect" not in wget_busybox(URL, KEY)
