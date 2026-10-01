"""Bot token and chat ID input checks (LOC-02, D-11, D-12).

Pure module: it imports nothing from Django and makes no network call. Telegram is
never asked on save; the live check is the LOC-07 test message (Phase 4).

Every failure raises ValueError whose text is exactly one of the UI-SPEC messages below,
so a form can show it as a field error as it is. The text never holds the input, an
exception name or Python's own error wording.

The patterns use explicit ASCII classes with ``fullmatch`` (P-10): ``\\d`` and ``int()``
also accept non-ASCII digits and ``1_000``, and ``$`` also matches before a trailing
newline.
"""

import re

# The same shape the log redaction scrubs (powermon.logging_setup.TOKEN_RE). Its strict
# character set keeps "/", "?" and "#" out of the Bot API URL path.
TOKEN_RE = re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{30,}")
# The bot_token column's max_length: a longer token is refused here, not by the database.
MAX_BOT_TOKEN_LENGTH = 255
CHAT_ID_RE = re.compile(r"-?[0-9]+")
# No signed 64-bit integer has more digits. Checked before int(), so a long paste never
# reaches int()'s digit limit or its error text.
MAX_CHAT_ID_DIGITS = 19
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
MASK = "•" * 8

# UI-SPEC copywriting contract, verbatim. The TOKEN_* names are UI copy, not secrets (S105).
TOKEN_EMPTY = "Paste the bot token from @BotFather."  # noqa: S105
TOKEN_FORMAT = (
    "This does not look like a bot token. It should be digits, a colon, "  # noqa: S105
    "then at least 30 letters, digits, - or _ (like 123456789:AAH…)."
)
CHAT_ID_EMPTY = "Enter the channel's numeric chat ID."
CHAT_ID_USERNAME = "Use the numeric chat ID, not the @username. Private channels have no username."
CHAT_ID_NOT_INTEGER = "Enter a whole number, like -1001234567890."
CHAT_ID_TOO_LONG = (
    "This number is too long for a Telegram chat ID. Copy the ID again, like -1001234567890."
)


def clean_bot_token(value: str) -> str:
    """The trimmed token, or ValueError with the UI copy."""
    token = value.strip()
    if not token:
        raise ValueError(TOKEN_EMPTY)
    if len(token) > MAX_BOT_TOKEN_LENGTH or not TOKEN_RE.fullmatch(token):
        raise ValueError(TOKEN_FORMAT)
    return token


def parse_chat_id(value: str) -> int:
    """The chat ID as a signed 64-bit integer, or ValueError with the UI copy."""
    text = value.strip()
    if not text:
        raise ValueError(CHAT_ID_EMPTY)
    if text.startswith("@"):
        raise ValueError(CHAT_ID_USERNAME)
    if not CHAT_ID_RE.fullmatch(text):
        raise ValueError(CHAT_ID_NOT_INTEGER)
    if len(text.removeprefix("-")) > MAX_CHAT_ID_DIGITS:
        raise ValueError(CHAT_ID_TOO_LONG)
    chat_id = int(text)
    if not INT64_MIN <= chat_id <= INT64_MAX:
        raise ValueError(CHAT_ID_TOO_LONG)
    return chat_id


def mask_token(token: str) -> str:
    """``{bot_id}:••••••••``: the public bot ID only, never the secret part (D-11)."""
    bot_id, colon, _secret = token.partition(":")
    return f"{bot_id}:{MASK}" if colon else MASK
