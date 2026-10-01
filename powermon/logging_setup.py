"""Logging that never prints bot tokens or device keys (OPS-08, T-01-09, D-16).

Every record goes to stdout through ``RedactingFormatter``. It scrubs the final
formatted text, so %-format arguments and exception tracebacks are covered too.

- Timestamps are UTC ISO 8601 with milliseconds and offset, for example
  ``2026-10-01T11:31:06.452+00:00``, whatever DISPLAY_TZ is (IN-03). Django sets TZ to
  the display zone and calls time.tzset(), so the default ``%(asctime)s`` would print
  local wall time, which is ambiguous in the repeated autumn hour.
- ``build_logging(level)`` applies LOG_LEVEL to the root and django loggers. urllib3
  and django.db.backends stay pinned at WARNING at every level: at DEBUG urllib3 logs
  request lines with the ``/bot<token>/`` path, and django.db.backends logs SQL
  parameters such as device keys, which the ``?key=`` pattern does not catch.

Pure module: it imports nothing from Django, because gunicorn's master loads it
(through ``powermon.web.gunicorn_conf``) before ``django.setup()``.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

# Explicit ASCII classes, never \d (it also matches non-ASCII digits).
# A Telegram bot token, with or without the "bot" prefix of the Bot API URL path.
TOKEN_RE = re.compile(r"(?:bot)?[0-9]{5,}:[A-Za-z0-9_-]{30,}")
# The device key in a "?key=" or "&key=" query parameter.
KEY_QS_RE = re.compile(r"([?&]key=)[^&\s\"']+")
# The value of an "Authorization: Bearer <value>" header.
BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)

# One line format for the app, the worker and gunicorn's own error logger.
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# The LOG_LEVEL values (the same set config.LOG_LEVELS accepts).
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Loggers that would print secrets below WARNING; never raised by LOG_LEVEL.
_PINNED = ("urllib3", "django.db.backends")


class RedactingFormatter(logging.Formatter):
    """Formats the record (UTC timestamp), then replaces tokens, query keys and bearer values."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # UTC ISO 8601 with milliseconds and offset (IN-03). datefmt is ignored on purpose:
        # a local-time format would bring back the ambiguous autumn hour.
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        # Format first, so the exc_info traceback text is scrubbed too.
        text = super().format(record)
        text = TOKEN_RE.sub("[REDACTED-TOKEN]", text)
        text = KEY_QS_RE.sub(r"\1[REDACTED]", text)
        return BEARER_RE.sub(r"\1[REDACTED]", text)


def build_logging(level: str) -> dict[str, Any]:
    """The dictConfig for ``level``: stdout only, redacted, secret-printing loggers pinned.

    Returns a new dict on every call, so callers (settings, gunicorn_conf) can extend it.
    Raises ValueError for anything but one of ``LEVELS``.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown log level; use one of {', '.join(LEVELS)}")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "redacting": {
                "()": "powermon.logging_setup.RedactingFormatter",
                "format": FORMAT,
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "redacting",
            },
        },
        "root": {"handlers": ["stdout"], "level": level},
        "loggers": {
            # Configuring "django" removes the console and mail_admins handlers that
            # Django's default logging attaches (they would print records unredacted); its
            # records propagate to the root stdout handler instead.
            "django": {"level": level},
            **{name: {"level": "WARNING"} for name in _PINNED},
        },
    }


# The INFO configuration, for importers that do not read LOG_LEVEL.
LOGGING: dict[str, Any] = build_logging("INFO")
