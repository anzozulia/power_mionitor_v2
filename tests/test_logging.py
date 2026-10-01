"""The redacting log formatter, driven through a real handler (OPS-08, INV-23, T-01-19).

Each test logs through a StreamHandler that uses RedactingFormatter with the format string
from settings.LOGGING, the same shape the stdout handler has in production, and asserts the
literal secret values are absent from what the handler wrote.
"""

import io
import logging
from collections.abc import Iterator

import pytest
from django.conf import settings

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
