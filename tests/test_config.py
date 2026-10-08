"""Fail-closed configuration (SEC-01, INV-21 #1; OPS-01, ALRT-03, D-07, D-09, D-16).

Production refuses to start when a secret, DOMAIN or ACME_EMAIL is missing, empty or still
the .env.example value, when DEBUG is on, or when DISPLAY_TZ does not resolve; the error
names the variable, and the real process exits non-zero.

The admin ops chat (OPS_BOT_TOKEN + OPS_CHAT_ID) is optional: both unset means "not
configured"; one of them alone, or a malformed value, is refused in every mode, and the
error never holds the token. ALERT_MAX_AGE_HOURS (1-48, default 6) and LOG_LEVEL (default
INFO) are refused when malformed. Build mode reads none of them.

The Config's repr leaves out every secret (SECRET_KEY, ADMIN_PASSWORD, POSTGRES_PASSWORD,
OPS_BOT_TOKEN), so neither a log line nor the DEBUG technical 500 page, which lists the
``CFG`` setting by its repr, can show one (wave 1 audit A3).

Every production case starts from one complete valid production env and changes only the
variable under test, so each error names exactly that variable whatever order ``load()``
checks in. Envs are plain dicts: no os.environ mutation, no monkeypatching.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import RequestFactory
from django.views.debug import technical_500_response

from powermon.config import BUILD_SECRET_KEY, EXAMPLE_VALUES, ConfigError, load

ENV_EXAMPLE = Path(settings.BASE_DIR) / ".env.example"
SECRETS = ("SECRET_KEY", "ADMIN_PASSWORD", "POSTGRES_PASSWORD")
DOMAIN = "power.example.org"
OPS_TOKEN = "555555555:" + "C" * 35
OPS_CHAT_ID = "-1005555555555"
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
    "OPS_BOT_TOKEN": OPS_TOKEN,
    "OPS_CHAT_ID": OPS_CHAT_ID,
    "ALERT_MAX_AGE_HOURS": "6",
    "LOG_LEVEL": "INFO",
    # Read only by docker/backup/backup.sh; the app ignores them (OPS-06).
    "BACKUP_TIME_UTC": "03:00",
    "BACKUP_KEEP": "14",
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
    assert cfg.ops_configured
    assert cfg.ops_bot_token == OPS_TOKEN
    assert cfg.ops_chat_id == -1005555555555
    assert cfg.alert_max_age_hours == 6
    assert cfg.log_level == "INFO"
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


def test_INV21_production_refuses_a_short_admin_password() -> None:
    # Imported here, not at module level: on the old code only this test is red (F-14).
    from powermon import config

    short = r"^ADMIN_PASSWORD must be at least 12 characters in production$"
    # Failure: 11 characters, and 11 padded with spaces (surrounding spaces do not count).
    with pytest.raises(ConfigError, match=short):
        load(_env(ADMIN_PASSWORD="p" * 11))
    with pytest.raises(ConfigError, match=short):
        load(_env(ADMIN_PASSWORD="  " + "p" * 11 + "  "))

    # Edge: exactly 12 characters load.
    assert load(_env(ADMIN_PASSWORD="p" * 12)).admin_password == "p" * 12
    # Expected: local mode has no floor.
    assert load(_env(APP_ENV="local", ADMIN_PASSWORD="p" * 11)).admin_password == "p" * 11
    assert config.MIN_ADMIN_PASSWORD_LENGTH == 12


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


# The admin ops chat (D-09, OPS-01, INV-20)


@pytest.mark.parametrize("app_env", ["production", "local"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_ops_chat_unset_is_allowed(app_env: str, value: str | None) -> None:
    cfg = load(_env(APP_ENV=app_env, OPS_BOT_TOKEN=value, OPS_CHAT_ID=value))

    assert not cfg.ops_configured
    assert cfg.ops_bot_token == ""
    assert cfg.ops_chat_id is None


def test_ops_chat_values_are_trimmed() -> None:
    cfg = load(_env(OPS_BOT_TOKEN=f"  {OPS_TOKEN}\n", OPS_CHAT_ID=" 12345 "))

    assert cfg.ops_configured
    assert cfg.ops_bot_token == OPS_TOKEN
    assert cfg.ops_chat_id == 12345


@pytest.mark.parametrize("app_env", ["production", "local"])
@pytest.mark.parametrize("unset", [None, "", "  "])
def test_ops_chat_half_configured_refused(app_env: str, unset: str | None) -> None:
    with pytest.raises(ConfigError, match=r"^OPS_CHAT_ID") as only_token:
        load(_env(APP_ENV=app_env, OPS_CHAT_ID=unset))
    with pytest.raises(ConfigError, match=r"^OPS_BOT_TOKEN") as only_chat:
        load(_env(APP_ENV=app_env, OPS_BOT_TOKEN=unset))

    assert OPS_TOKEN not in str(only_token.value)
    assert OPS_CHAT_ID not in str(only_chat.value)


@pytest.mark.parametrize(
    "token",
    [
        "abc",
        "12345:short",
        "123456789:" + "A" * 29,
        "123456789:" + "A" * 20 + "/" + "A" * 20,
        "123456789:" + "A" * 20 + "?" + "A" * 20,
        "١٢٣٤٥٦٧٨٩:" + "A" * 35,
        "123456789:" + "A" * 246,
    ],
    ids=["abc", "short", "29-chars", "slash", "question-mark", "arabic-indic", "256-chars"],
)
@pytest.mark.parametrize("app_env", ["production", "local"])
def test_bad_ops_bot_token_refused_without_echo(app_env: str, token: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(_env(APP_ENV=app_env, OPS_BOT_TOKEN=token))

    message = str(excinfo.value)
    secret = token.partition(":")[2]
    assert message.startswith("OPS_BOT_TOKEN")
    assert token not in message
    assert not secret or secret not in message


def test_a_255_character_ops_bot_token_loads() -> None:
    token = "123456789:" + "A" * 245

    assert load(_env(OPS_BOT_TOKEN=token)).ops_bot_token == token


@pytest.mark.parametrize(
    "chat_id",
    [
        "@ops_channel",
        "abc",
        "1_000",
        "+100",
        "1" * 20,
        "-9223372036854775809",
        "１２３４５",
    ],
    ids=["username", "abc", "underscore", "plus", "20-digits", "below-int64", "full-width"],
)
def test_bad_ops_chat_id_refused(chat_id: str) -> None:
    with pytest.raises(ConfigError, match=r"^OPS_CHAT_ID") as excinfo:
        load(_env(OPS_CHAT_ID=chat_id))

    assert OPS_TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    "chat_id", ["-9223372036854775808", "9223372036854775807", "-1001234567890", "42"]
)
def test_int64_ops_chat_ids_load(chat_id: str) -> None:
    assert load(_env(OPS_CHAT_ID=chat_id)).ops_chat_id == int(chat_id)


# Alert maximum age (D-07, ALRT-03) and log level (D-16)


@pytest.mark.parametrize(
    ("value", "hours"),
    [(None, 6), ("", 6), ("  ", 6), ("1", 1), ("48", 48), (" 7 ", 7), ("06", 6)],
)
def test_alert_max_age_hours_default_and_range(value: str | None, hours: int) -> None:
    assert load(_env(ALERT_MAX_AGE_HOURS=value)).alert_max_age_hours == hours


@pytest.mark.parametrize("value", ["0", "49", "-1", "6.5", "1_0", "100", "６", "6h"])
def test_alert_max_age_hours_outside_1_to_48_refused(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^ALERT_MAX_AGE_HOURS .*1 to 48"):
        load(_env(ALERT_MAX_AGE_HOURS=value))


@pytest.mark.parametrize(
    ("value", "level"),
    [
        (None, "INFO"),
        ("", "INFO"),
        ("debug", "DEBUG"),
        (" Warning ", "WARNING"),
        ("ERROR", "ERROR"),
        ("info", "INFO"),
    ],
)
def test_log_level_default_and_values(value: str | None, level: str) -> None:
    assert load(_env(LOG_LEVEL=value)).log_level == level


@pytest.mark.parametrize("value", ["TRACE", "VERBOSE", "CRITICAL", "20", "ınfo"])
def test_bad_log_level_refused(value: str) -> None:
    with pytest.raises(ConfigError, match=r"^LOG_LEVEL"):
        load(_env(LOG_LEVEL=value))


def test_build_mode_skips_secrets() -> None:
    cfg = load(
        {
            "APP_BUILD": "1",
            "DEBUG": "1",
            "OPS_BOT_TOKEN": "not-a-token",
            "ALERT_MAX_AGE_HOURS": "0",
            "LOG_LEVEL": "TRACE",
        }
    )

    assert cfg.build
    assert not cfg.debug
    assert cfg.production
    # Build mode reads no secret and no runtime setting.
    assert not cfg.ops_configured
    assert cfg.ops_bot_token == ""
    assert cfg.ops_chat_id is None
    assert cfg.alert_max_age_hours == 6
    assert cfg.log_level == "INFO"


# The config's repr never shows a secret (wave 1 audit A3)

# Every secret Config holds, by the variable it comes from.
SECRET_VARIABLES = (*SECRETS, "OPS_BOT_TOKEN")


def _boom() -> None:
    raise RuntimeError("a view failed")


def test_config_repr_hides_every_secret() -> None:
    text = repr(load(_env()))

    for name in SECRET_VARIABLES:
        assert VALID_PRODUCTION[name] not in text, name
    for field in ("secret_key", "admin_password", "db_password", "ops_bot_token"):
        assert f"{field}=" not in text, field
    # Everything else is still shown.
    assert "admin_username='admin'" in text
    assert f"ops_chat_id={OPS_CHAT_ID}" in text
    assert f"domain='{DOMAIN}'" in text


def test_build_mode_config_repr_hides_its_placeholder_key() -> None:
    cfg = load({"APP_BUILD": "1"})

    text = repr(cfg)

    assert cfg.secret_key == BUILD_SECRET_KEY
    assert BUILD_SECRET_KEY not in text
    assert text.startswith("Config(app_env='production', production=True, build=True")


def test_debug_500_page_never_shows_config_secrets(settings: Any, rf: RequestFactory) -> None:
    # With DEBUG on (local only), Django's technical 500 page lists every setting. It masks
    # settings by name, and "CFG" matches none of its patterns, so the page shows CFG as its
    # repr: that repr must hold no secret.
    settings.CFG = load(_env())
    settings.DEBUG = True
    try:
        _boom()
    except RuntimeError:
        response = technical_500_response(rf.get("/"), *sys.exc_info())

    page = response.content.decode()
    assert "admin_username" in page
    for name in SECRET_VARIABLES:
        assert VALID_PRODUCTION[name] not in page, name


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


def test_bad_ops_bot_token_stops_the_process_without_echo() -> None:
    secret = "B" * 20 + "/" + "B" * 20

    result = _run_django_setup(_env(OPS_BOT_TOKEN=f"123456789:{secret}"))

    assert result.returncode != 0
    assert "ImproperlyConfigured: OPS_BOT_TOKEN" in result.stderr
    assert secret not in result.stderr + result.stdout
