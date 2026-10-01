"""Device keys (D-07): 32 random characters from [A-Za-z0-9].

Pure module: it imports nothing from Django. The alphabet has no "%", "-" or "_", so a key
is safe in a crontab and never needs URL-encoding in ``?key=``. Keys are stored retrievably
(the setup page reveals them), and the full key is shown only after an explicit reveal.
"""

import secrets
import string

KEY_ALPHABET = string.ascii_letters + string.digits
KEY_LENGTH = 32
# The masked form shows twelve bullets (U+2022) and the last four characters.
MASK_BULLETS = 12
MASK_VISIBLE = 4


def generate_device_key() -> str:
    """A new key from the OS CSPRNG (about 190 bits)."""
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def mask_key(key: str) -> str:
    """``••••••••••••`` plus the key's last four characters.

    Raises ValueError for anything that is not a full-length key. The message never
    contains the key.
    """
    if len(key) != KEY_LENGTH:
        raise ValueError(f"a device key has {KEY_LENGTH} characters")
    return "•" * MASK_BULLETS + key[-MASK_VISIBLE:]
