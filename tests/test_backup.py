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


def _lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]


def _flag_value(call: list[str], flag: str) -> str:
    assert flag in call, f"{flag} missing from {call}"
    return call[call.index(flag) + 1]


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _epoch_of(at: datetime) -> int:
    return int(at.timestamp())


def _name(at: datetime) -> str:
    return f"powermon-{at:%Y%m%dT%H%M%S}Z.dump"


def _put_dump(stubs: Stubs, at: datetime) -> Path:
    """A dump taken at ``at``, as the script leaves one: 0600 in a 0700 directory."""
    stubs.backups.mkdir(mode=0o700, exist_ok=True)
    path = stubs.backups / _name(at)
    path.write_text(DUMP_BYTES)
    path.chmod(0o600)
    return path


def _names(stubs: Stubs) -> list[str]:
    """Every file in the backup directory, temp files included."""
    if not stubs.backups.exists():
        return []
    return sorted(path.name for path in stubs.backups.iterdir())


def _nights(first: int, last: int) -> list[datetime]:
    """03:00:10 UTC on October ``first`` ... ``last``, 2026: one scheduled check per night."""
    return [_utc(2026, 10, day, 3, 0, 10) for day in range(first, last + 1)]


def _errors(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in _lines(result) if "error:" in line]


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
    now = _epoch_of(_utc(2026, 10, 3, 3, 0, 10))
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
    now = _epoch_of(_utc(2026, 10, 3, 3, 0, 10))

    result = stubs.run("--once", "--now", str(now), POSTGRES_PASSWORD=None)

    assert result.returncode == 2
    lines = _lines(result)
    assert len(lines) == 1, lines
    assert "POSTGRES_PASSWORD" in lines[0]
    assert not stubs.backups.exists()
    assert stubs.calls() == []


# The nightly rules (INV-25 #1, D-09, D-10)


def test_INV25_1_sixteen_nightly_runs_keep_fourteen_dumps(stubs: Stubs) -> None:
    nights = _nights(1, 16)

    results = [stubs.run("--once", "--now", str(_epoch_of(night))) for night in nights]

    assert [result.returncode for result in results] == [0] * 16
    assert _names(stubs) == [_name(night) for night in nights[2:]]
    assert len(stubs.calls("pg_dump")) == 16


def test_INV25_1_start_with_a_dump_older_than_24h_dumps_at_once(stubs: Stubs) -> None:
    # 25 h old at 01:00, two hours before today's 03:00 slot: caught up at once (D-09).
    old = _put_dump(stubs, _utc(2026, 10, 2, 0, 0, 0))
    now = _utc(2026, 10, 3, 1, 0, 0)

    result = stubs.run("--once", "--now", str(_epoch_of(now)))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _names(stubs) == [old.name, _name(now)]
    assert len(stubs.calls("pg_dump")) == 1


def test_once_after_todays_slot_dump_does_not_dump_again(stubs: Stubs) -> None:
    taken = _put_dump(stubs, _utc(2026, 10, 3, 3, 0, 5))

    result = stubs.run("--once", "--now", str(_epoch_of(_utc(2026, 10, 3, 10, 0, 0))))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _names(stubs) == [taken.name]
    assert stubs.calls("pg_dump") == []


def test_once_at_the_slot_boundary(stubs: Stubs) -> None:
    yesterday = _put_dump(stubs, _utc(2026, 10, 2, 3, 0, 10))
    slot = _utc(2026, 10, 3, 3, 0, 0)

    before = stubs.run("--once", "--now", str(_epoch_of(slot) - 1))
    names_before = _names(stubs)
    at = stubs.run("--once", "--now", str(_epoch_of(slot)))

    assert before.returncode == 0, before.stdout + before.stderr
    assert names_before == [yesterday.name]
    assert at.returncode == 0, at.stdout + at.stderr
    assert _names(stubs) == [yesterday.name, _name(slot)]
    assert len(stubs.calls("pg_dump")) == 1


def test_failed_pg_dump_removes_its_temp_file_and_rotates_nothing(stubs: Stubs) -> None:
    # One dump more than BACKUP_KEEP: a rotation that ran on a failure would delete one.
    present = [_put_dump(stubs, night).name for night in _nights(1, 14)]
    now = str(_epoch_of(_utc(2026, 10, 15, 3, 0, 10)))

    result = stubs.run("--once", "--now", now, STUB_FAIL_DUMP="1", BACKUP_KEEP="13")

    assert result.returncode == 1
    assert _names(stubs) == present
    assert len(_lines(result)) == 1, _lines(result)
    assert len(_errors(result)) == 1
    assert "pg_dump" in _errors(result)[0]
    assert stubs.calls("pg_restore") == []


@pytest.mark.parametrize("failure", ["STUB_FAIL_LIST", "STUB_EMPTY_DUMP"])
def test_failed_verification_removes_its_temp_file_and_rotates_nothing(
    stubs: Stubs, failure: str
) -> None:
    # pg_dump "succeeds", but the file is empty or pg_restore --list rejects it (D-10).
    # One dump more than BACKUP_KEEP: a rotation that ran on a failure would delete one.
    present = [_put_dump(stubs, night).name for night in _nights(1, 14)]
    now = str(_epoch_of(_utc(2026, 10, 15, 3, 0, 10)))

    result = stubs.run("--once", "--now", now, BACKUP_KEEP="13", **{failure: "1"})

    assert result.returncode == 1
    assert _names(stubs) == present
    assert len(_lines(result)) == 1, _lines(result)
    assert len(_errors(result)) == 1
    assert "verification" in _errors(result)[0]
    assert len(stubs.calls("pg_dump")) == 1


def test_rotation_keeps_the_newest_by_name_whatever_the_mtime(stubs: Stubs) -> None:
    # mtimes run backwards: the newest name has the oldest mtime (a dump copied by hand).
    nights = _nights(1, 14)
    for index, night in enumerate(nights):
        path = _put_dump(stubs, night)
        stamp = _epoch_of(_utc(2026, 9, 1)) - index * 86400
        os.utime(path, (stamp, stamp))
    now = _utc(2026, 10, 15, 3, 0, 10)

    result = stubs.run("--once", "--now", str(_epoch_of(now)))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _names(stubs) == [_name(night) for night in nights[1:]] + [_name(now)]


def test_backup_keep_one_keeps_only_the_new_dump(stubs: Stubs) -> None:
    for night in _nights(1, 3):
        _put_dump(stubs, night)
    now = _utc(2026, 10, 4, 3, 0, 10)

    result = stubs.run("--once", "--now", str(_epoch_of(now)), BACKUP_KEEP="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _names(stubs) == [_name(now)]


# Health: the newest dump's age, by its name (D-11)


def test_health_by_the_newest_dump_name(stubs: Stubs) -> None:
    now = _utc(2026, 10, 4, 4, 0, 0)
    epoch = str(_epoch_of(now))

    no_directory = stubs.run("--health", "--now", epoch)
    directory_created = stubs.backups.exists()
    stubs.backups.mkdir(mode=0o700)
    (stubs.backups / f".{_name(_utc(2026, 10, 4, 3, 0, 0))}.partial").write_text("PGD")
    only_partial = stubs.run("--health", "--now", epoch)
    _put_dump(stubs, _utc(2026, 10, 3, 1, 0, 0))  # 27 h old
    stale = stubs.run("--health", "--now", epoch)
    _put_dump(stubs, _utc(2026, 10, 3, 2, 0, 0))  # exactly 26 h old
    boundary = stubs.run("--health", "--now", epoch)
    _put_dump(stubs, _utc(2026, 10, 3, 3, 0, 0))  # 25 h old
    # Health reads file names only: it needs no database setting and calls no tool.
    fresh = stubs.run("--health", "--now", epoch, POSTGRES_PASSWORD=None)

    assert no_directory.returncode == 1
    assert not directory_created
    assert "no dump yet" in no_directory.stdout
    assert only_partial.returncode == 1
    assert "no dump yet" in only_partial.stdout
    assert stale.returncode == 1
    assert "27 h" in stale.stdout
    assert boundary.returncode == 1
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert "25 h" in fresh.stdout
    for result in (no_directory, only_partial, stale, boundary, fresh):
        assert len(_lines(result)) == 1, _lines(result)
    assert stubs.calls() == []


# Settings and secrets (D-11, T-05-21)


@pytest.mark.parametrize(
    ("named", "args", "env"),
    [
        ("BACKUP_TIME_UTC", ("--once",), {"BACKUP_TIME_UTC": "3:00"}),
        ("BACKUP_TIME_UTC", ("--once",), {"BACKUP_TIME_UTC": "24:00"}),
        ("BACKUP_TIME_UTC", ("--once",), {"BACKUP_TIME_UTC": "03:60"}),
        ("BACKUP_TIME_UTC", ("--once",), {"BACKUP_TIME_UTC": "x"}),
        ("BACKUP_KEEP", ("--once",), {"BACKUP_KEEP": "0"}),
        ("BACKUP_KEEP", ("--once",), {"BACKUP_KEEP": "366"}),
        ("BACKUP_KEEP", ("--once",), {"BACKUP_KEEP": "x"}),
        ("--now", ("--once", "--now", "abc"), {}),
        ("--now", ("--health", "--now", "abc"), {}),
        ("--bogus", ("--bogus",), {}),
    ],
)
def test_invalid_settings_exit_2_and_write_nothing(
    stubs: Stubs, named: str, args: tuple[str, ...], env: dict[str, str]
) -> None:
    result = stubs.run(*args, **env)

    assert result.returncode == 2
    lines = _lines(result)
    assert len(lines) == 1, lines
    message = lines[0].split("backup: ", 1)[1]
    assert named in message
    # The line names the variable, never its value (a bad mode is itself what it names).
    for value in [*env.values(), *args[2:]]:
        assert value not in message
    assert not stubs.backups.exists()
    assert stubs.calls() == []


def test_password_never_in_output_or_argv(stubs: Stubs) -> None:
    night = _epoch_of(_utc(2026, 10, 3, 3, 0, 10))
    next_night = str(night + 86400)

    results = [
        stubs.run("--once", "--now", str(night)),
        stubs.run("--health", "--now", str(night)),
        stubs.run("--once", "--now", next_night, STUB_FAIL_DUMP="1"),
        stubs.run("--once", "--now", next_night, STUB_FAIL_LIST="1"),
        stubs.run("--once", "--now", next_night, BACKUP_KEEP="0"),
    ]

    assert [result.returncode for result in results] == [0, 0, 1, 1, 2]
    for result in results:
        assert PASSWORD not in result.stdout + result.stderr
    assert stubs.calls()
    assert PASSWORD not in stubs.log.read_text()
