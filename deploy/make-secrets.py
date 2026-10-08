#!/usr/bin/env python3
"""Create or check the production env file on the shared VPS (README section 17).

Run as root in /root/powermonitor:

    python3 deploy/make-secrets.py            create the file, once
    python3 deploy/make-secrets.py --check    check it; prints variable names, never values

``--repo DIR`` (default: the parent of this script's directory) targets another directory
that holds .env.example. CI uses it with a scratch directory.

Create copies .env.example line by line, sets APP_ENV, DEBUG, DOMAIN, ACME_EMAIL and
ADMIN_USERNAME to fixed values, sets SECRET_KEY, ADMIN_PASSWORD and POSTGRES_PASSWORD to
fresh random values, and writes the file with mode 0600. It refuses when the file already
exists: POSTGRES_PASSWORD is read only at the database's first init, so the file is never
regenerated. OPS_BOT_TOKEN and OPS_CHAT_ID stay empty (ops alerts off until the owner sets
both). The file name lives only in this script, so no command line has to name it.

Stdlib only, Python 3.12 or newer, and nothing from powermon: it runs on the host python3.
"""

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

ENV_NAME = ".env.docker_production"
EXAMPLE_NAME = ".env.example"
FIXED = {
    "APP_ENV": "production",
    "DEBUG": "0",
    "DOMAIN": "powermonitor.anzozulia.com",
    # Unused here (the host certbot does TLS), but production requires a non-example value.
    "ACME_EMAIL": "admin@powermonitor.anzozulia.com",
    "ADMIN_USERNAME": "admin",
}
RANDOM_KEYS = ("SECRET_KEY", "ADMIN_PASSWORD", "POSTGRES_PASSWORD")
# The sentinel values committed in .env.example (powermon/config.py EXAMPLE_VALUES).
EXAMPLE_VALUES = {
    "SECRET_KEY": "change-me-to-a-long-random-string",  # noqa: S105
    "ADMIN_PASSWORD": "change-me-admin-password",  # noqa: S105
    "POSTGRES_PASSWORD": "change-me-db-password",  # noqa: S105
    "DOMAIN": "power.example.com",
    "ACME_EMAIL": "you@example.com",
}
# powermon/config.py _REQUIRED, plus the two that production also requires.
REQUIRED = (
    "SECRET_KEY",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DOMAIN",
    "ACME_EMAIL",
)
MIN_SECRET_KEY_LENGTH = 50
# powermon/config.py MIN_ADMIN_PASSWORD_LENGTH.
MIN_ADMIN_PASSWORD_LENGTH = 12


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def _key(line: str) -> str | None:
    """The variable name of a ``KEY=value`` line, or None for a comment or blank line."""
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    key, sep, _ = line.partition("=")
    return key.strip() if sep else None


def parse(text: str) -> dict[str, str]:
    """``KEY=value`` per non-comment line, the value verbatim after the first ``=``."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key = _key(line)
        if key is not None:
            values[key] = line.partition("=")[2]
    return values


def create(repo: Path) -> int:
    target = repo / ENV_NAME
    if os.path.lexists(target):
        _error(
            f"refused: {target} already exists. POSTGRES_PASSWORD is read only at the "
            "database's first init, so this file is never regenerated; edit it by hand."
        )
        return 1
    example = repo / EXAMPLE_NAME
    if not example.is_file():
        _error(f"refused: {example} is missing")
        return 1

    wanted = dict(FIXED)
    for name in RANDOM_KEYS:
        wanted[name] = secrets.token_urlsafe(50)
    lines: list[str] = []
    seen: set[str] = set()
    for line in example.read_text().splitlines():
        key = _key(line)
        if key is not None and key in wanted:
            lines.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            lines.append(line)
    missing = sorted(set(wanted) - seen)
    if missing:
        _error(f"refused: {EXAMPLE_NAME} has no line for: {', '.join(missing)}")
        return 1

    data = ("\n".join(lines) + "\n").encode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except BaseException:
        os.close(fd)
        target.unlink()
        raise
    os.close(fd)
    print(f"wrote {target} (0600); set: {' '.join(sorted(wanted))}")
    return 0


def check(repo: Path) -> int:
    target = repo / ENV_NAME
    if not target.is_file():
        _error(f"{ENV_NAME}: missing in {repo}")
        return 1

    problems: list[str] = []
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode != 0o600:
        problems.append(f"{ENV_NAME}: mode is {mode:o}, expected 600")
    if os.path.lexists(repo / ".env"):
        problems.append(".env: exists in the repo directory; Compose would read it, remove it")
    values = parse(target.read_text())
    example = repo / EXAMPLE_NAME
    if example.is_file():
        for name in sorted(set(parse(example.read_text())) - set(values)):
            problems.append(f"{name}: missing (it is in {EXAMPLE_NAME})")
    else:
        problems.append(f"{EXAMPLE_NAME}: missing in {repo}")
    for name in REQUIRED:
        if not values.get(name, "").strip():
            problems.append(f"{name}: missing or empty")
    for name, example_value in EXAMPLE_VALUES.items():
        if values.get(name, "").strip() == example_value:
            problems.append(f"{name}: still the example value from {EXAMPLE_NAME}")
    if len(values.get("SECRET_KEY", "").strip()) < MIN_SECRET_KEY_LENGTH:
        problems.append(f"SECRET_KEY: shorter than {MIN_SECRET_KEY_LENGTH} characters")
    if len(values.get("ADMIN_PASSWORD", "").strip()) < MIN_ADMIN_PASSWORD_LENGTH:
        problems.append(f"ADMIN_PASSWORD: shorter than {MIN_ADMIN_PASSWORD_LENGTH} characters")
    for name in ("APP_ENV", "DEBUG", "DOMAIN"):
        if values.get(name, "").strip() != FIXED[name]:
            problems.append(f"{name}: must be {FIXED[name]}")
    if bool(values.get("OPS_BOT_TOKEN", "").strip()) != bool(values.get("OPS_CHAT_ID", "").strip()):
        problems.append("OPS_BOT_TOKEN, OPS_CHAT_ID: set both or neither")

    for problem in problems:
        _error(problem)
    if problems:
        return 1
    print(f"ok: {' '.join(sorted(values))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or check the production env file (names only, never values)."
    )
    parser.add_argument(
        "--check", action="store_true", help="check the existing file instead of creating it"
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the directory that holds .env.example (default: the repository root)",
    )
    args = parser.parse_args()
    repo: Path = args.repo
    return check(repo) if args.check else create(repo)


if __name__ == "__main__":
    sys.exit(main())
