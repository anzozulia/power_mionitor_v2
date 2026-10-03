"""Nightly backups: docker/backup/backup.sh (OPS-06, INV-25 #1; D-09, D-10, D-11, D-12).

The script runs here as the backup container runs it, with bash, as a subprocess. Stub
pg_dump, pg_restore and psql shims come first on PATH and append their argv to a log, so a
test sees every tool call; an injected ``--now EPOCH`` stands in for the clock. No container
is started and no database is touched (D-16: INV-25 #2, the restore drill, is a recorded
check at /gsd-verify-work 5).

Tests run as uid 10001 in the dev image, so the script's root branch (chown, then gosu
postgres) is never reached here; the container runs it.
"""

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_IMODE

import pytest
from django.conf import settings

BACKUP_SH = Path(settings.BASE_DIR) / "docker" / "backup" / "backup.sh"
PASSWORD = "s3cret-test-password-123"
DUMP_BYTES = "PGDMP stub dump\n"
COMMAND_TIMEOUT_S = 30

# Every stub first appends one line per call: its name, then its arguments, tab-separated.
_RECORD = r"""#!/bin/sh
{ printf '%s' "${0##*/}"; for a in "$@"; do printf '\t%s' "$a"; done; echo; } >> "$STUB_LOG"
"""
STUBS = {
    # Writes the stub dump to its -f argument. STUB_FAIL_DUMP=1 writes a few bytes and
    # fails; STUB_EMPTY_DUMP=1 writes an empty file and succeeds.
    "pg_dump": _RECORD
    + r"""out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-f" ]; then out=$2; shift; fi
  shift
done
if [ "${STUB_EMPTY_DUMP:-}" = 1 ]; then : > "$out"; exit 0; fi
if [ "${STUB_FAIL_DUMP:-}" = 1 ]; then printf 'PGD' > "$out"; exit 1; fi
printf 'PGDMP stub dump\n' > "$out"
""",
    # --list fails when STUB_FAIL_LIST=1, else prints one TOC line; a restore exits with
    # STUB_RESTORE_EXIT (default 0).
    "pg_restore": _RECORD
    + r"""if [ "$1" = "--list" ]; then
  if [ "${STUB_FAIL_LIST:-}" = 1 ]; then exit 1; fi
  echo "; Archive created by the stub"
  exit 0
fi
exit "${STUB_RESTORE_EXIT:-0}"
""",
    # Prints the table count the restore check asks for (STUB_TABLES, default 0).
    "psql": _RECORD + 'echo "${STUB_TABLES:-0}"\n',
}


@dataclass
class Stubs:
    bin_dir: Path
    log: Path
    backups: Path

    def env(self, **changes: str | None) -> dict[str, str]:
        """The script's env (only what the container passes); a None value unsets a key."""
        env = {
            "PATH": os.pathsep.join([str(self.bin_dir), os.environ["PATH"]]),
            "STUB_LOG": str(self.log),
            "BACKUP_DIR": str(self.backups),
            "BACKUP_TIME_UTC": "03:00",
            "BACKUP_KEEP": "14",
            "POSTGRES_DB": "powermon",
            "POSTGRES_USER": "powermon",
            "POSTGRES_PASSWORD": PASSWORD,
        }
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, *args: str, **env: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(BACKUP_SH), *args],
            env=self.env(**env),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )

    def calls(self, tool: str | None = None) -> list[list[str]]:
        """Every stub call so far as [tool, arg, ...], oldest first."""
        if not self.log.exists():
            return []
        calls = [line.split("\t") for line in self.log.read_text().splitlines()]
        return [call for call in calls if tool is None or call[0] == tool]


@pytest.fixture
def stubs(tmp_path: Path) -> Iterator[Stubs]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        shim = bin_dir / name
        shim.write_text(body)
        shim.chmod(0o755)
    yield Stubs(bin_dir=bin_dir, log=tmp_path / "calls.log", backups=tmp_path / "backups")


def _epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp())


def _lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]


def _flag_value(call: list[str], flag: str) -> str:
    assert flag in call, f"{flag} missing from {call}"
    return call[call.index(flag) + 1]


# The script and the image (RESEARCH A1)


def test_backup_script_runs_under_bash_in_the_test_image() -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(BACKUP_SH)],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    gnu_date = subprocess.run(
        ["date", "-u", "-d", "@0", "+%Y"],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )

    assert syntax.returncode == 0, syntax.stderr
    assert gnu_date.stdout.strip() == "1970", gnu_date.stderr


# One scheduled check (D-09, D-10, D-11)


def test_once_with_no_dump_writes_one_verified_dump(stubs: Stubs) -> None:
    now = _epoch(2026, 10, 3, 3, 0, 10)
    partial = str(stubs.backups / ".powermon-20261003T030010Z.dump.partial")

    result = stubs.run("--once", "--now", str(now))

    assert result.returncode == 0, result.stdout + result.stderr
    assert S_IMODE(stubs.backups.stat().st_mode) == 0o700
    assert sorted(path.name for path in stubs.backups.iterdir()) == [
        "powermon-20261003T030010Z.dump"
    ]
    dump = stubs.backups / "powermon-20261003T030010Z.dump"
    assert S_IMODE(dump.stat().st_mode) == 0o600
    assert dump.read_text() == DUMP_BYTES
    calls = stubs.calls()
    assert [call[0] for call in calls] == ["pg_dump", "pg_restore"]
    assert "-Fc" in calls[0]
    assert _flag_value(calls[0], "-f") == partial
    assert calls[1][1:] == ["--list", partial]
    assert "backup: dump powermon-20261003T030010Z.dump ok" in result.stdout


def test_once_refuses_a_missing_database_password(stubs: Stubs) -> None:
    now = _epoch(2026, 10, 3, 3, 0, 10)

    result = stubs.run("--once", "--now", str(now), POSTGRES_PASSWORD=None)

    assert result.returncode == 2
    lines = _lines(result)
    assert len(lines) == 1, lines
    assert "POSTGRES_PASSWORD" in lines[0]
    assert not stubs.backups.exists()
    assert stubs.calls() == []
