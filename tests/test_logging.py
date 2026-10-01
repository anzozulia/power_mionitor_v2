"""The redacting log formatter, driven through a real handler (OPS-08, INV-23, T-01-19).

Each test logs through a StreamHandler that uses RedactingFormatter with the format string
from settings.LOGGING, the same shape the stdout handler has in production, and asserts the
literal secret values are absent from what the handler wrote.

IN-03 / D-16: every line starts with a UTC ISO 8601 timestamp with milliseconds and offset,
whatever DISPLAY_TZ is. Django sets TZ to the display zone (Europe/Kyiv in tests) and calls
time.tzset(), so the default %(asctime)s would print ambiguous local wall time in the
repeated autumn hour. LOG_LEVEL sets the root and django loggers; urllib3 and
django.db.backends stay pinned at WARNING at every level, because at DEBUG they print the
/bot<token>/ path and SQL parameters such as device keys.

Records for the timestamp tests are built with logging.LogRecord and an explicit
``created``; no test calls time.tzset or mutates os.environ.
"""

import io
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from django.conf import settings

from powermon import config, logging_setup
from powermon.logging_setup import RedactingFormatter

# Bot API token shape: digits, a colon, 35 characters from [A-Za-z0-9_-].
TOKEN = "123456789:AbCdEfGhIjKlMnOpQrStUvWxYz0123456_-"
BARE_TOKEN = "987654321:ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543-_"
KEY = "dK3vQ9mW2xR7tY5uI8oP1aS4dF6gH0jL"  # 32 characters, D-07 shape
OTHER_KEY = "Qw8eRt6yUi4oPa2sDf0gHj9kLz7xCv5b"
BEARER = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFi"


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(settings.LOGGING["formatters"]["redacting"]["format"]))
    logger = logging.getLogger("tests.redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield logger, stream
    finally:
        logger.removeHandler(handler)
        logger.propagate = True


def test_redacting_formatter_scrubs_tokens_keys_and_bearer(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture

    logger.warning(
        "POST https://api.telegram.org/bot%s/sendMessage failed; bare %s; GET /hb?key=%s; "
        "GET /hb?x=1&key=%s; Authorization: Bearer %s",
        TOKEN,
        BARE_TOKEN,
        KEY,
        OTHER_KEY,
        BEARER,
    )
    text = stream.getvalue()

    for secret in (TOKEN, BARE_TOKEN, KEY, OTHER_KEY, BEARER):
        assert secret not in text
    for secret_half in (TOKEN.split(":")[1], BARE_TOKEN.split(":")[1]):
        assert secret_half not in text
    assert "https://api.telegram.org/[REDACTED-TOKEN]/sendMessage" in text
    assert "/hb?x=1&key=[REDACTED]" in text


def test_redacting_formatter_scrubs_traceback_text(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture

    try:
        raise OSError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage?key={KEY}")
    except OSError:
        logger.exception("send failed")
    text = stream.getvalue()

    assert "Traceback (most recent call last)" in text
    assert "OSError" in text
    assert TOKEN not in text
    assert TOKEN.split(":")[1] not in text
    assert KEY not in text


def test_redacting_formatter_keeps_short_digit_pairs(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    # Near misses: too few digits, or too few characters after the colon, to be a token.
    line = (
        "ratio 12345:short; build 1234:AbCdEfGhIjKlMnOpQrStUvWxYz0123456789; "
        "id 123456:AbCdEfGhIjKlMnOpQrStUvWxYz012"
    )

    logger.info(line)

    assert stream.getvalue().rstrip("\n").endswith(f"INFO tests.redaction {line}")


# IN-03: UTC ISO 8601 timestamps with offset (D-16)


def _record(
    at: datetime, msg: str = "cycle done", name: str = "powermon.worker"
) -> logging.LogRecord:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, (), None)
    record.created = at.timestamp()
    return record


def _utc_formatter() -> RedactingFormatter:
    return RedactingFormatter(settings.LOGGING["formatters"]["redacting"]["format"])


def test_IN03_log_timestamps_are_utc_iso_with_offset() -> None:
    # The display zone is Kyiv (UTC+3 on this date); the log line must still be UTC.
    assert settings.TIME_ZONE == "Europe/Kyiv"
    record = _record(datetime(2026, 10, 1, 11, 31, 6, 452000, tzinfo=UTC))

    text = _utc_formatter().format(record)

    assert text.startswith("2026-10-01T11:31:06.452+00:00 INFO powermon.worker cycle done")


def test_IN03_log_timestamp_is_utc_in_the_repeated_dst_hour() -> None:
    # 2026-10-25: Kyiv falls back from 04:00 EEST to 03:00 EET, so 00:30Z and 01:30Z are
    # both 03:30 local. Local wall time cannot order them; UTC with offset can.
    first = _record(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    second = _record(datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
    local = logging.Formatter("%(asctime)s")
    assert local.format(first)[:19] == local.format(second)[:19] == "2026-10-25 03:30:00"

    texts = [_utc_formatter().format(r) for r in (first, second)]

    assert texts[0].startswith("2026-10-25T00:30:00.000+00:00 INFO ")
    assert texts[1].startswith("2026-10-25T01:30:00.000+00:00 INFO ")


def test_IN03_log_timestamp_ignores_a_datefmt() -> None:
    # A caller-supplied datefmt must not bring back local time.
    formatter = RedactingFormatter("%(asctime)s %(message)s", datefmt="%H:%M")

    text = formatter.format(_record(datetime(2026, 10, 1, 23, 59, 59, 999000, tzinfo=UTC)))

    assert text == "2026-10-01T23:59:59.999+00:00 cycle done"


# LOG_LEVEL (D-16, OPS-08)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_build_logging_applies_the_level(level: str) -> None:
    built = logging_setup.build_logging(level)

    assert built["root"] == {"handlers": ["stdout"], "level": level}
    assert built["loggers"]["django"]["level"] == level
    # Pinned whatever LOG_LEVEL is: at DEBUG they print token URLs and SQL parameters.
    assert built["loggers"]["urllib3"]["level"] == "WARNING"
    assert built["loggers"]["django.db.backends"]["level"] == "WARNING"
    assert built["formatters"]["redacting"] == {
        "()": "powermon.logging_setup.RedactingFormatter",
        "format": logging_setup.FORMAT,
    }
    assert built["handlers"]["stdout"]["formatter"] == "redacting"


def test_build_logging_returns_independent_dicts() -> None:
    # dictConfig and gunicorn_conf extend the result; one caller must not change another's.
    first = logging_setup.build_logging("DEBUG")
    first["loggers"]["gunicorn.error"] = {"level": "DEBUG"}

    assert "gunicorn.error" not in logging_setup.build_logging("DEBUG")["loggers"]
    assert logging_setup.LOGGING["root"]["level"] == "INFO"


@pytest.mark.parametrize("level", ["TRACE", "", "info", "CRITICAL"])
def test_build_logging_rejects_an_unknown_level(level: str) -> None:
    with pytest.raises(ValueError, match="log level"):
        logging_setup.build_logging(level)


def test_log_levels_match_the_config_values() -> None:
    assert logging_setup.LEVELS == config.LOG_LEVELS


def test_settings_logging_uses_the_configured_level() -> None:
    assert settings.LOGGING["root"]["level"] == settings.CFG.log_level
    redacting = settings.LOGGING["formatters"]["redacting"]
    assert redacting["()"] == "powermon.logging_setup.RedactingFormatter"
    assert redacting["format"] == logging_setup.FORMAT


def test_log_level_env_reaches_the_live_loggers() -> None:
    # A real process with LOG_LEVEL=DEBUG: root and django log at DEBUG, the pins hold.
    probe = (
        "import logging, django; django.setup(); "
        "print(logging.getLevelName(logging.getLogger().level), "
        "logging.getLevelName(logging.getLogger('django').getEffectiveLevel()), "
        "logging.getLevelName(logging.getLogger('urllib3').getEffectiveLevel()), "
        "logging.getLevelName(logging.getLogger('django.db.backends').getEffectiveLevel()))"
    )
    env = {**os.environ, "LOG_LEVEL": "DEBUG", "DJANGO_SETTINGS_MODULE": "powermon.settings"}

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["DEBUG", "DEBUG", "WARNING", "WARNING"]


# Redaction runs on the final text (OPS-08, ordering edge)


def test_redacting_formatter_scrubs_tokens_in_format_arguments(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    logger.error("send failed: %s", url)
    try:
        raise ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
    except ConnectionError:
        logger.error("relay failed", exc_info=True)
    text = stream.getvalue()

    assert TOKEN not in text
    assert TOKEN.split(":")[1] not in text
    # Once from the %-format argument, once from the exception line of the traceback.
    assert text.count("[REDACTED-TOKEN]") == 2
    assert "send failed: https://api.telegram.org/[REDACTED-TOKEN]/sendMessage" in text
    assert "url: https://api.telegram.org/[REDACTED-TOKEN]/sendMessage\n" in text


def test_redacting_formatter_scrubs_gunicorn_error_records() -> None:
    # The same formatter class serves gunicorn's own error logger (gunicorn_conf).
    record = logging.LogRecord(
        "gunicorn.error", logging.ERROR, __file__, 1, "Error handling /bot%s/x", (TOKEN,), None
    )
    record.created = datetime(2026, 10, 1, 8, 0, tzinfo=UTC).timestamp()

    text = _utc_formatter().format(record)

    assert text == (
        "2026-10-01T08:00:00.000+00:00 ERROR gunicorn.error Error handling /[REDACTED-TOKEN]/x"
    )


def test_logging_setup_imports_without_django() -> None:
    # gunicorn's master loads this module (through gunicorn_conf) before django.setup().
    probe = "import sys, powermon.logging_setup; sys.exit(1 if 'django' in sys.modules else 0)"

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=settings.BASE_DIR,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
