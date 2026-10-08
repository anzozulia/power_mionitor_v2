"""Nightly backups: docker/backup/backup.sh (OPS-06, INV-25 #1; D-09, D-10, D-11, D-12).

The script runs here as the backup container runs it, with bash, as a subprocess. Stub
pg_dump, pg_restore and psql shims come first on PATH and append their argv to a log, so a
test sees every tool call; an injected ``--now EPOCH`` stands in for the clock. The loop
(the container's command) runs as a background process with second-scale knobs and is
stopped with SIGTERM or SIGINT. No container is started and no database is touched (D-16:
INV-25 #2, the restore drill, is a recorded check at /gsd-verify-work 5).

Tests run as uid 10001 in the dev image, so the script's root branch (chown, then gosu
postgres) is never reached here; the container runs it.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_IMODE
from typing import IO

import pytest
from django.conf import settings

BACKUP_SH = Path(settings.BASE_DIR) / "docker" / "backup" / "backup.sh"
PASSWORD = "s3cret-test-password-123"
DUMP_BYTES = "PGDMP stub dump\n"
COMMAND_TIMEOUT_S = 30
# The question --restore asks before it restores anything (D-15).
TABLE_COUNT_SQL = "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"

# Every stub first appends one line per call: its name, then its arguments, tab-separated.
_RECORD = r"""#!/bin/sh
{ printf '%s' "${0##*/}"; for a in "$@"; do printf '\t%s' "$a"; done; echo; } >> "$STUB_LOG"
"""
STUBS = {
    # Writes the stub dump to its -f argument. STUB_FAIL_DUMP=1 writes a few bytes and
    # fails; STUB_EMPTY_DUMP=1 writes an empty file and succeeds. With STUB_TIMES set, it
    # appends the time it was called (as it saw it), whenever the test reads the file.
    "pg_dump": _RECORD
    + r"""if [ -n "${STUB_TIMES:-}" ]; then date +%s.%N >> "$STUB_TIMES"; fi
out=""
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


@dataclass
class Loop:
    """The container's command, ``bash backup.sh``, running in the background."""

    proc: subprocess.Popen[bytes]
    out: Path
    handle: IO[str]

    def output(self) -> str:
        return self.out.read_text()

    def stop(self, sig: int) -> int:
        """Send ``sig`` and return the exit code; raises if it has not exited within 5 s."""
        self.proc.send_signal(sig)
        return self.proc.wait(timeout=5)


@dataclass
class Loops:
    stubs: Stubs
    tmp_path: Path
    started: list[Loop] = field(default_factory=list)

    def start(self, **env: str) -> Loop:
        # Output goes to a file, not a pipe: nothing can block on a full pipe or on an
        # inherited write end.
        out = self.tmp_path / f"loop-{len(self.started)}.log"
        handle = out.open("w")
        proc = subprocess.Popen(
            ["bash", str(BACKUP_SH)],
            env=self.stubs.env(**env),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        loop = Loop(proc=proc, out=out, handle=handle)
        self.started.append(loop)
        return loop


@pytest.fixture
def loops(stubs: Stubs, tmp_path: Path) -> Iterator[Loops]:
    runner = Loops(stubs=stubs, tmp_path=tmp_path)
    yield runner
    for loop in runner.started:
        if loop.proc.poll() is None:
            loop.proc.kill()
            loop.proc.wait(timeout=5)
        loop.handle.close()


def _wait_for(check: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


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


def _dump_names(stubs: Stubs) -> list[str]:
    """The finished dumps only: temp files start with a dot."""
    return [name for name in _names(stubs) if not name.startswith(".")]


def _call_times(path: Path) -> list[float]:
    if not path.exists():
        return []
    return [float(stamp) for stamp in path.read_text().split()]


def _restores(stubs: Stubs) -> list[list[str]]:
    """pg_restore calls that restore (every call except --list)."""
    return [call for call in stubs.calls("pg_restore") if call[1:2] != ["--list"]]


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


FUTURE = _utc(2031, 1, 1, 3, 0, 0)


def test_INV25_1_a_future_dated_dump_does_not_stop_the_nightly_dumps(stubs: Stubs) -> None:
    # F-15: a dump from a clock that was ahead must not count as the newest.
    yesterday = _put_dump(stubs, _utc(2026, 10, 2, 3, 0, 10))
    future = _put_dump(stubs, FUTURE)
    now = _utc(2026, 10, 3, 3, 0, 10)

    result = stubs.run("--once", "--now", str(_epoch_of(now)))

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(stubs.calls("pg_dump")) == 1
    assert _names(stubs) == [yesterday.name, _name(now), future.name]


def test_INV25_1_dumps_on_demand_do_not_shorten_the_nightly_window(stubs: Stubs) -> None:
    # F-16: two midday dumps on demand (e.g. pre-migration) must not push a nightly one out.
    nightly = [_put_dump(stubs, night).name for night in _nights(2, 15)]
    midday = [_put_dump(stubs, _utc(2026, 10, day, 12, 0, 0)).name for day in (10, 12)]
    now = _utc(2026, 10, 16, 3, 0, 10)

    result = stubs.run("--once", "--now", str(_epoch_of(now)))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _names(stubs) == sorted([*nightly[1:], *midday, _name(now)])
    assert len(_names(stubs)) == 16
    assert f"removed {nightly[0]}" in result.stdout


def test_rotation_keeps_at_least_backup_keep_however_old(stubs: Stubs) -> None:
    # Edge: the count floor holds even when every kept dump is far older than the window.
    old = [_put_dump(stubs, _utc(2026, 8, day, 3, 0, 10)).name for day in range(1, 6)]
    now = _utc(2026, 10, 3, 3, 0, 10)

    result = stubs.run("--once", "--now", str(_epoch_of(now)), BACKUP_KEEP="3")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _names(stubs) == [*old[3:], _name(now)]


def test_rotation_deletes_an_undatable_name_beyond_the_keep(stubs: Stubs) -> None:
    # Failure: 31 February matches the name pattern but is no real time; it sorts first.
    undatable = stubs.backups / "powermon-20260231T030000Z.dump"
    nightly = [_put_dump(stubs, night).name for night in _nights(3, 15)]
    undatable.write_text(DUMP_BYTES)
    now = _utc(2026, 10, 16, 3, 0, 10)

    result = stubs.run("--once", "--now", str(_epoch_of(now)))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not undatable.exists()
    assert _names(stubs) == [*nightly, _name(now)]
    assert f"removed {undatable.name}" in result.stdout


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


@pytest.mark.parametrize("ahead", ["year 2031", "301 s ahead"])
def test_health_unhealthy_while_a_dump_is_dated_in_the_future(stubs: Stubs, ahead: str) -> None:
    now = _utc(2026, 10, 4, 4, 0, 0)
    _put_dump(stubs, now - timedelta(hours=1))
    future = _put_dump(stubs, FUTURE if ahead == "year 2031" else now + timedelta(seconds=301))

    result = stubs.run("--health", "--now", str(_epoch_of(now)), POSTGRES_PASSWORD=None)

    assert result.returncode == 1
    lines = _lines(result)
    assert len(lines) == 1, lines
    assert "future" in lines[0]
    assert future.name in lines[0]
    assert stubs.calls() == []


@pytest.mark.parametrize("ahead_s", [240, 300])
def test_a_dump_a_few_minutes_ahead_counts_as_newest(stubs: Stubs, ahead_s: int) -> None:
    # Edge: up to FUTURE_SLACK_S (300 s) ahead is clock jitter, not a clock that was ahead.
    now = _utc(2026, 10, 3, 10, 0, 0)
    ahead = _put_dump(stubs, now + timedelta(seconds=ahead_s))
    epoch = str(_epoch_of(now))

    once = stubs.run("--once", "--now", epoch)
    health = stubs.run("--health", "--now", epoch)

    assert once.returncode == 0, once.stdout + once.stderr
    assert stubs.calls("pg_dump") == []
    assert _names(stubs) == [ahead.name]
    assert health.returncode == 0, health.stdout + health.stderr
    assert "healthy: newest dump" in health.stdout


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


# The container loop (D-09, RESEARCH Pitfall 7)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_loop_dumps_at_start_cleans_leftovers_and_stops_on_term(
    stubs: Stubs, loops: Loops, sig: int
) -> None:
    # A dump cut off by a stop or a crash leaves its temp file behind. SIGINT is the
    # postgres image's STOPSIGNAL; SIGTERM is what `docker stop` sends by default.
    stubs.backups.mkdir(mode=0o700)
    leftover = stubs.backups / ".powermon-20261001T030000Z.dump.partial"
    leftover.write_text("PGD")

    loop = loops.start(BACKUP_CHECK_EVERY_S="1")
    dumped = _wait_for(lambda: len(_dump_names(stubs)) == 1, 10)
    leftover_gone = not leftover.exists()
    asked = time.monotonic()
    code = loop.stop(sig)
    took = time.monotonic() - asked

    assert dumped, loop.output()
    assert leftover_gone
    assert code == 0, loop.output()
    assert took < 5
    assert "stopping" in loop.output()
    assert len(_names(stubs)) == 1
    assert len(stubs.calls("pg_dump")) == 1


def test_loop_paces_failed_dumps(stubs: Stubs, loops: Loops, tmp_path: Path) -> None:
    # Retry after 2 s, then 4 s (the cap), instead of on every 1 s check (D-09). Only
    # lower bounds on the gaps between stub-recorded call times are asserted: load can
    # delay a call, never bring it forward.
    times = tmp_path / "pg_dump.times"
    loop = loops.start(
        STUB_FAIL_DUMP="1",
        STUB_TIMES=str(times),
        BACKUP_CHECK_EVERY_S="1",
        BACKUP_RETRY_FIRST_S="2",
        BACKUP_RETRY_MAX_S="4",
    )

    three_calls = _wait_for(lambda: len(_call_times(times)) >= 3, 30)
    time.sleep(2)
    code = loop.stop(signal.SIGTERM)
    calls = _call_times(times)
    gaps = [later - earlier for earlier, later in zip(calls, calls[1:], strict=False)]

    assert three_calls, loop.output()
    assert code == 0, loop.output()
    # The retry is counted on a whole-second clock from after the failed attempt.
    assert gaps[0] >= 0.9, gaps
    assert all(gap >= 2.9 for gap in gaps[1:]), gaps
    # Every failed attempt removed its own temp file; no dump was ever renamed.
    assert _names(stubs) == []


# A dump on demand (D-16)


def test_dump_now_dumps_even_when_not_due(stubs: Stubs) -> None:
    # The slot was 20 minutes ago and the newest dump is from 10 minutes ago: nothing is
    # due, so --once does nothing, but --dump-now dumps and rotates.
    now = datetime.now(UTC).replace(microsecond=0)
    slot = f"{now - timedelta(minutes=20):%H:%M}"
    older = [_put_dump(stubs, now - timedelta(days=day)).name for day in range(13, 0, -1)]
    recent = _put_dump(stubs, now - timedelta(minutes=10)).name

    once = stubs.run("--once", BACKUP_TIME_UTC=slot)
    forced = stubs.run("--dump-now", BACKUP_TIME_UTC=slot)

    assert once.returncode == 0, once.stdout + once.stderr
    assert forced.returncode == 0, forced.stdout + forced.stderr
    names = _names(stubs)
    new = [name for name in names if name not in [*older, recent]]
    assert len(new) == 1
    assert new[0] > recent
    # A dump on demand does not push out a nightly one (INV-25, F-16): all 15 dumps kept.
    assert names == [*older, recent, new[0]]
    assert len(stubs.calls("pg_dump")) == 1


# Restore only into an empty database (D-15, T-05-22, T-05-23)


def test_restore_into_an_empty_database(stubs: Stubs) -> None:
    dump = _put_dump(stubs, _utc(2026, 10, 3, 3, 0, 10))

    result = stubs.run("--restore", dump.name, STUB_TABLES="0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stubs.calls() == [
        ["pg_restore", "--list", str(dump)],
        ["psql", "-X", "-At", "-c", TABLE_COUNT_SQL],
        [
            "pg_restore",
            "--no-owner",
            "--single-transaction",
            "--exit-on-error",
            "-d",
            "powermon",
            str(dump),
        ],
    ]
    assert _errors(result) == []
    # A restore reads the dump and deletes nothing.
    assert _names(stubs) == [dump.name]


def test_restore_refuses_a_database_with_tables(stubs: Stubs) -> None:
    dump = _put_dump(stubs, _utc(2026, 10, 3, 3, 0, 10))

    result = stubs.run("--restore", dump.name, STUB_TABLES="3")

    assert result.returncode == 1
    assert len(_lines(result)) == 1, _lines(result)
    assert "restores only into a new, empty database" in _errors(result)[0]
    assert _restores(stubs) == []
    assert _names(stubs) == [dump.name]


@pytest.mark.parametrize(
    "case", ["parent dir", "sub dir", "empty name", "missing file", "fails --list"]
)
def test_restore_refuses_bad_names(stubs: Stubs, tmp_path: Path, case: str) -> None:
    # Every name points at a real dump-looking file, so only the refusal stops a restore.
    dump = _put_dump(stubs, _utc(2026, 10, 3, 3, 0, 10))
    (tmp_path / "x.dump").write_text(DUMP_BYTES)
    (stubs.backups / "sub").mkdir()
    (stubs.backups / "sub" / "x.dump").write_text(DUMP_BYTES)
    name, env = {
        "parent dir": ("../x.dump", {}),
        "sub dir": ("sub/x.dump", {}),
        "empty name": ("", {}),
        "missing file": ("powermon-20261004T030010Z.dump", {}),
        "fails --list": (dump.name, {"STUB_FAIL_LIST": "1"}),
    }[case]

    result = stubs.run("--restore", name, **env)

    assert result.returncode == 1
    assert len(_lines(result)) == 1, _lines(result)
    assert len(_errors(result)) == 1
    assert _restores(stubs) == []
    assert stubs.calls("psql") == []


def test_restore_reports_a_failed_pg_restore(stubs: Stubs) -> None:
    dump = _put_dump(stubs, _utc(2026, 10, 3, 3, 0, 10))

    result = stubs.run("--restore", dump.name, STUB_RESTORE_EXIT="1")

    assert result.returncode == 1
    assert len(_errors(result)) == 1, _lines(result)
    assert "pg_restore" in _errors(result)[0]
    assert len(_restores(stubs)) == 1


@pytest.mark.parametrize(
    "args",
    [("--restore",), ("--restore", "a.dump", "extra"), ("--dump-now", "extra")],
    ids=["restore without a name", "restore with two names", "dump-now with an argument"],
)
def test_restore_without_a_name_is_a_usage_error(stubs: Stubs, args: tuple[str, ...]) -> None:
    result = stubs.run(*args)

    assert result.returncode == 2
    lines = _lines(result)
    assert len(lines) == 1, lines
    assert args[0] in lines[0]
    assert not stubs.backups.exists()
    assert stubs.calls() == []
