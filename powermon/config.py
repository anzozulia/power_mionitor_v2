"""Fail-closed configuration read from the environment (SEC-01, INV-21).

Pure module: it imports nothing from Django (``powermon.locations.validators`` is
pure too). ``powermon.settings`` calls ``load(os.environ)`` once, so every
entrypoint (gunicorn, manage.py, pytest) refuses to start on a bad environment,
and the message always starts with the name of the offending variable.

The admin ops chat (``OPS_BOT_TOKEN`` + ``OPS_CHAT_ID``, D-09) is optional: both
unset means "not configured" and is allowed; one of them alone, or a malformed
value, is refused, and the error never holds the token. Consumers read
``settings.CFG`` at call time, never at import time.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from powermon.locations import validators


class ConfigError(Exception):
    """A configuration problem. The message starts with the variable name."""


# The sentinel values committed in .env.example. Production refuses to start while
# any of them is still set. They are placeholders, not secrets.
EXAMPLE_VALUES: Mapping[str, str] = {
    "SECRET_KEY": "change-me-to-a-long-random-string",  # noqa: S105
    "ADMIN_PASSWORD": "change-me-admin-password",  # noqa: S105
    "POSTGRES_PASSWORD": "change-me-db-password",  # noqa: S105
    "DOMAIN": "power.example.com",
    "ACME_EMAIL": "you@example.com",
}

DEFAULT_DISPLAY_TZ = "Europe/Kyiv"
DEFAULT_LOCAL_BASE_URL = "http://localhost:8000"
MIN_SECRET_KEY_LENGTH = 50
# Undelivered alerts expire this long after recorded_at (D-07, ALRT-03).
DEFAULT_ALERT_MAX_AGE_HOURS = 6
MAX_ALERT_MAX_AGE_HOURS = 48
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_LEVEL = "INFO"
# Build mode only (collectstatic in the Dockerfile); never used to serve requests.
BUILD_SECRET_KEY = "build-mode-only-not-a-secret"  # noqa: S105

_REQUIRED = (
    "SECRET_KEY",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# Explicit ASCII classes: \d and int() also accept non-ASCII digits and underscores.
_PORT_RE = re.compile(r"[0-9]{1,5}")
_HOURS_RE = re.compile(r"[0-9]{1,2}")
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


@dataclass(frozen=True, slots=True)
class Config:
    """The validated configuration every Django entrypoint is built from.

    The secrets are left out of the repr, so neither a log line nor Django's DEBUG 500
    page (which lists the ``CFG`` setting by its repr, its name matching none of
    Django's masking patterns) can show one (OPS-08).
    """

    app_env: str
    production: bool
    build: bool
    debug: bool
    secret_key: str = field(repr=False)
    admin_username: str
    admin_password: str = field(repr=False)
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    db_host: str
    db_port: int
    domain: str
    display_tz: str
    allowed_hosts: tuple[str, ...]
    public_base_url: str
    # "" and None when the ops chat is not configured (D-09).
    ops_bot_token: str = field(repr=False)
    ops_chat_id: int | None
    alert_max_age_hours: int
    log_level: str

    @property
    def ops_configured(self) -> bool:
        """True when both OPS_BOT_TOKEN and OPS_CHAT_ID are set (INV-20)."""
        return bool(self.ops_bot_token) and self.ops_chat_id is not None


def load(env: Mapping[str, str]) -> Config:
    """Parse ``env`` into a Config, or raise ConfigError naming the variable."""
    app_env = _app_env(env)
    production = app_env == "production"
    display_tz = _display_tz(env)

    if env.get("APP_BUILD") == "1":
        # Build mode: collectstatic runs without secrets and without a database.
        return Config(
            app_env=app_env,
            production=production,
            build=True,
            debug=False,
            secret_key=BUILD_SECRET_KEY,
            admin_username="",
            admin_password="",
            db_name="",
            db_user="",
            db_password="",
            db_host="",
            db_port=0,
            domain="",
            display_tz=display_tz,
            allowed_hosts=(),
            public_base_url="",
            ops_bot_token="",
            ops_chat_id=None,
            alert_max_age_hours=DEFAULT_ALERT_MAX_AGE_HOURS,
            log_level=DEFAULT_LOG_LEVEL,
        )

    debug = _flag(env.get("DEBUG", ""))
    if production and debug:
        raise ConfigError("DEBUG must be off in production")

    required = {name: _required(env, name) for name in _REQUIRED}
    db_host = env.get("POSTGRES_HOST", "").strip() or "db"
    db_port = _port(env)
    ops_bot_token, ops_chat_id = _ops_chat(env)
    alert_max_age_hours = _alert_max_age_hours(env)
    log_level = _log_level(env)

    if production:
        domain = _domain(env)
        _required(env, "ACME_EMAIL")
        for name, example in EXAMPLE_VALUES.items():
            if env.get(name, "").strip() == example:
                raise ConfigError(f"{name} still has the example value from .env.example")
        if len(required["SECRET_KEY"].strip()) < MIN_SECRET_KEY_LENGTH:
            raise ConfigError(
                f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters in production"
            )
        allowed_hosts: tuple[str, ...] = (domain, "127.0.0.1")
        public_base_url = f"https://{domain}"
    else:
        domain = env.get("DOMAIN", "").strip()
        allowed_hosts = ("localhost", "127.0.0.1")
        base_url = env.get("PUBLIC_BASE_URL", "").strip() or DEFAULT_LOCAL_BASE_URL
        public_base_url = base_url.rstrip("/")

    return Config(
        app_env=app_env,
        production=production,
        build=False,
        debug=debug,
        secret_key=required["SECRET_KEY"],
        admin_username=required["ADMIN_USERNAME"],
        admin_password=required["ADMIN_PASSWORD"],
        db_name=required["POSTGRES_DB"],
        db_user=required["POSTGRES_USER"],
        db_password=required["POSTGRES_PASSWORD"],
        db_host=db_host,
        db_port=db_port,
        domain=domain,
        display_tz=display_tz,
        allowed_hosts=allowed_hosts,
        public_base_url=public_base_url,
        ops_bot_token=ops_bot_token,
        ops_chat_id=ops_chat_id,
        alert_max_age_hours=alert_max_age_hours,
        log_level=log_level,
    )


def _app_env(env: Mapping[str, str]) -> str:
    # Missing (or empty) means production: fail closed.
    value = env.get("APP_ENV", "").strip() or "production"
    if value not in ("production", "local"):
        raise ConfigError("APP_ENV must be 'production' or 'local'")
    return value


def _display_tz(env: Mapping[str, str]) -> str:
    value = env.get("DISPLAY_TZ", "").strip() or DEFAULT_DISPLAY_TZ
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError, ValueError:
        raise ConfigError(
            f"DISPLAY_TZ {value!r} is not a known IANA time zone; use the canonical name, "
            f"e.g. {DEFAULT_DISPLAY_TZ!r} (the legacy alias 'Europe/Kiev' is not available)"
        ) from None
    return value


def _flag(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value.strip():
        raise ConfigError(f"{name} is missing or empty")
    return value


def _port(env: Mapping[str, str]) -> int:
    value = env.get("POSTGRES_PORT", "").strip() or "5432"
    if not _PORT_RE.fullmatch(value) or not 1 <= int(value) <= 65535:
        raise ConfigError("POSTGRES_PORT must be an integer from 1 to 65535")
    return int(value)


def _ops_chat(env: Mapping[str, str]) -> tuple[str, int | None]:
    """The ops bot token and chat ID, or ("", None) when neither is set (D-09).

    The token's strict shape keeps "/", "?" and "#" out of the Bot API URL path. No
    error text holds the token.
    """
    token = env.get("OPS_BOT_TOKEN", "").strip()
    chat = env.get("OPS_CHAT_ID", "").strip()
    if not token and not chat:
        return "", None
    if not chat:
        raise ConfigError("OPS_CHAT_ID must be set together with OPS_BOT_TOKEN")
    if not token:
        raise ConfigError("OPS_BOT_TOKEN must be set together with OPS_CHAT_ID")
    if len(token) > validators.MAX_BOT_TOKEN_LENGTH or not validators.TOKEN_RE.fullmatch(token):
        raise ConfigError(
            "OPS_BOT_TOKEN does not look like a Telegram bot token: digits, a colon, "
            "then at least 30 of A-Z a-z 0-9 _ -"
        )
    try:
        chat_id = validators.parse_chat_id(chat)
    except ValueError:
        raise ConfigError(
            "OPS_CHAT_ID must be a numeric Telegram chat ID such as -1001234567890"
        ) from None
    return token, chat_id


def _alert_max_age_hours(env: Mapping[str, str]) -> int:
    value = env.get("ALERT_MAX_AGE_HOURS", "").strip()
    if not value:
        return DEFAULT_ALERT_MAX_AGE_HOURS
    if not _HOURS_RE.fullmatch(value) or not 1 <= int(value) <= MAX_ALERT_MAX_AGE_HOURS:
        raise ConfigError(
            f"ALERT_MAX_AGE_HOURS must be a whole number of hours from 1 to "
            f"{MAX_ALERT_MAX_AGE_HOURS}"
        )
    return int(value)


def _log_level(env: Mapping[str, str]) -> str:
    value = env.get("LOG_LEVEL", "").strip()
    if not value:
        return DEFAULT_LOG_LEVEL
    # ASCII only: str.upper() maps the dotless "ı" to "I".
    level = value.upper()
    if not value.isascii() or level not in LOG_LEVELS:
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    return level


def _domain(env: Mapping[str, str]) -> str:
    value = _required(env, "DOMAIN").strip()
    if not _HOST_RE.fullmatch(value):
        raise ConfigError(
            "DOMAIN must be a bare host name such as power.example.org (no scheme, port or path)"
        )
    return value
