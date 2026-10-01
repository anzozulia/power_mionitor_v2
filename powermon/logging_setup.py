"""Logging that never prints bot tokens or device keys (OPS-08, T-01-09).

Every record goes to stdout through ``RedactingFormatter``. It scrubs the final
formatted text, so exception tracebacks are covered too. The loggers that would print
secrets at DEBUG are pinned to WARNING: urllib3 logs request lines with the
``/bot<token>/`` path, and django.db.backends logs SQL parameters such as device keys.
"""

import logging
import re
from typing import Any

# Explicit ASCII classes, never \d (it also matches non-ASCII digits).
# A Telegram bot token, with or without the "bot" prefix of the Bot API URL path.
TOKEN_RE = re.compile(r"(?:bot)?[0-9]{5,}:[A-Za-z0-9_-]{30,}")
# The device key in a "?key=" or "&key=" query parameter.
KEY_QS_RE = re.compile(r"([?&]key=)[^&\s\"']+")
# The value of an "Authorization: Bearer <value>" header.
BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


class RedactingFormatter(logging.Formatter):
    """Formats the record, then replaces tokens, query keys and bearer values."""

    def format(self, record: logging.LogRecord) -> str:
        # Format first, so the exc_info traceback text is scrubbed too.
        text = super().format(record)
        text = TOKEN_RE.sub("[REDACTED-TOKEN]", text)
        text = KEY_QS_RE.sub(r"\1[REDACTED]", text)
        return BEARER_RE.sub(r"\1[REDACTED]", text)


LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "redacting": {
            "()": "powermon.logging_setup.RedactingFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "redacting",
        },
    },
    "root": {"handlers": ["stdout"], "level": "INFO"},
    "loggers": {
        # Configuring "django" removes the console and mail_admins handlers that Django's
        # default logging attaches (they would print records unredacted); its records
        # propagate to the root stdout handler instead.
        "django": {"level": "INFO"},
        "urllib3": {"level": "WARNING"},
        "django.db.backends": {"level": "WARNING"},
    },
}
