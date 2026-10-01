"""Fail-closed configuration (SEC-01, INV-21 #1).

Production refuses to start when a secret, DOMAIN or ACME_EMAIL is missing, empty or still
the .env.example value, when DEBUG is on, or when DISPLAY_TZ does not resolve; the error
names the variable, and the real process exits non-zero.

Every production case starts from one complete valid production env and changes only the
variable under test, so each error names exactly that variable whatever order ``load()``
checks in. Envs are plain dicts: no os.environ mutation, no monkeypatching.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from powermon.config import EXAMPLE_VALUES, ConfigError, load

ENV_EXAMPLE = Path(settings.BASE_DIR) / ".env.example"
SECRETS = ("SECRET_KEY", "ADMIN_PASSWORD", "POSTGRES_PASSWORD")
DOMAIN = "power.example.org"
# One complete production env: every .env.example key, none with its example value.
VALID_PRODUCTION: dict[str, str] = {
    "APP_ENV": "production",
    "DEBUG": "0",
    "SECRET_KEY": ("prod-test-secret-key-not-the-example-" * 2)[:64],
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "a-real-admin-password",
    "POSTGRES_DB": "powermon",
    "POSTGRES_USER": "powermon",
    "POSTGRES_PASSWORD": "a-real-db-password",
    "POSTGRES_HOST": "db",
    "POSTGRES_PORT": "5432",
    "DOMAIN": DOMAIN,
    "ACME_EMAIL": "ops@example.org",
    "DISPLAY_TZ": "Europe/Kyiv",
    "PUBLIC_BASE_URL": "http://localhost:8000",
}
# How a required value can be wrong; None means the variable is not set at all.
BAD_VALUES = ("missing", "empty", "blank", "example")


def _env_example() -> dict[str, str]:
    values = {}
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f".env.example line is not KEY=value: {line!r}"
        values[key] = value
    return values


def _env(**changes: str | None) -> dict[str, str]:
    """VALID_PRODUCTION with ``changes`` applied; a None value removes the variable."""
    env = dict(VALID_PRODUCTION)
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _bad(name: str, how: str) -> str | None:
    return {"missing": None, "empty": "", "blank": "   ", "example": _env_example()[name]}[how]


def _run_django_setup(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    child_env = {
        **env,
        "DJANGO_SETTINGS_MODULE": "powermon.settings",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=settings.BASE_DIR,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# Production


def test_valid_production_env_loads() -> None:
    cfg = load(VALID_PRODUCTION)

    assert cfg.production
    assert not cfg.debug
    assert not cfg.build
    assert cfg.allowed_hosts == (DOMAIN, "127.0.0.1")
    assert cfg.public_base_url == f"https://{DOMAIN}"
    assert cfg.secret_key == VALID_PRODUCTION["SECRET_KEY"]
    # The base env covers every key a deploy sets, so a later key cannot be forgotten here.
    assert set(VALID_PRODUCTION) == set(_env_example())


@pytest.mark.parametrize("how", BAD_VALUES)
@pytest.mark.parametrize("name", SECRETS)
def test_INV21_secret_missing_empty_or_example_refused(name: str, how: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(_env(**{name: _bad(name, how)}))

    assert str(excinfo.value).startswith(name)


@pytest.mark.parametrize("name", ["ADMIN_USERNAME", "POSTGRES_DB", "POSTGRES_USER"])
def test_other_required_settings_missing_refused(name: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(_env(**{name: None}))

    assert str(excinfo.value).startswith(name)


@pytest.mark.parametrize("how", BAD_VALUES)
@pytest.mark.parametrize("name", ["DOMAIN", "ACME_EMAIL"])
def test_domain_and_acme_email_required_and_example_values_refused_in_production(
    name: str, how: str
) -> None:
    env = _env(**{name: _bad(name, how)})

    with pytest.raises(ConfigError) as excinfo:
        load(env)

    assert str(excinfo.value).startswith(name)
    # Local mode needs neither: no Caddy, no certificate.
    assert not load({**env, "APP_ENV": "local"}).production


def test_short_secret_key_refused_in_production() -> None:
    with pytest.raises(ConfigError, match=r"^SECRET_KEY must be at least 50 characters"):
        load(_env(SECRET_KEY="s" * 49))

    assert load(_env(SECRET_KEY="s" * 50)).secret_key == "s" * 50


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_debug_refused_in_production(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^DEBUG must be off in production$"):
        load(_env(DEBUG=value))


@pytest.mark.parametrize("value", ["0", "", "false", "off", None])
def test_debug_off_values_load_in_production(value: str | None) -> None:
    assert load(_env(DEBUG=value)).debug is False


# Local mode and APP_ENV


def test_local_mode_honours_debug_and_accepts_example_values() -> None:
    env = {**_env_example(), "APP_ENV": "local", "DEBUG": "1"}

    cfg = load(env)

    assert not cfg.production
    assert cfg.debug
    assert cfg.secret_key == EXAMPLE_VALUES["SECRET_KEY"]
    assert cfg.allowed_hosts == ("localhost", "127.0.0.1")
    assert cfg.public_base_url == "http://localhost:8000"
    assert load({**env, "PUBLIC_BASE_URL": "http://localhost:8000/"}).public_base_url == (
        "http://localhost:8000"
    )


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_app_env_means_production(value: str | None) -> None:
    env = _env(APP_ENV=value)

    assert load(env).production
    with pytest.raises(ConfigError, match=r"^SECRET_KEY"):
        load({**env, "SECRET_KEY": _env_example()["SECRET_KEY"]})


@pytest.mark.parametrize("value", ["staging", "Production", "dev"])
def test_bad_app_env_refused(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^APP_ENV"):
        load(_env(APP_ENV=value))


# Other values


@pytest.mark.parametrize("value", ["Europe/Kiev", "Mars/Olympus_Mons", "../../etc/passwd"])
def test_display_tz_legacy_alias_refused(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^DISPLAY_TZ"):
        load(_env(DISPLAY_TZ=value))


@pytest.mark.parametrize("value", ["Europe/Kyiv", None, ""])
def test_display_tz_canonical_name_and_default_load(value: str | None) -> None:
    assert load(_env(DISPLAY_TZ=value)).display_tz == "Europe/Kyiv"


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-1", "1_000", "５４３２"])
def test_bad_postgres_port_refused(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^POSTGRES_PORT"):
        load(_env(POSTGRES_PORT=value))


@pytest.mark.parametrize(("value", "port"), [("5433", 5433), ("", 5432), (None, 5432)])
def test_postgres_port_and_default_load(value: str | None, port: int) -> None:
    assert load(_env(POSTGRES_PORT=value)).db_port == port


def test_build_mode_skips_secrets() -> None:
    cfg = load({"APP_BUILD": "1", "DEBUG": "1"})

    assert cfg.build
    assert not cfg.debug
    assert cfg.production


@pytest.mark.parametrize("value", ["0", "true", "yes"])
def test_only_app_build_1_enables_build_mode(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^SECRET_KEY"):
        load({"APP_BUILD": value})


def test_env_example_sentinels_match_and_are_refused() -> None:
    example = _env_example()

    for name, value in EXAMPLE_VALUES.items():
        assert example[name] == value, name
    assert set(SECRETS) | {"DOMAIN", "ACME_EMAIL"} <= set(EXAMPLE_VALUES)
    assert example["APP_ENV"] == "production"
    with pytest.raises(ConfigError):
        load(example)


# The real process


def test_INV21_production_process_exits_naming_the_variable() -> None:
    result = _run_django_setup(_env(SECRET_KEY=_env_example()["SECRET_KEY"]))

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "SECRET_KEY" in result.stderr


def test_production_process_starts_with_a_valid_env() -> None:
    result = _run_django_setup(VALID_PRODUCTION)

    assert result.returncode == 0, result.stderr
