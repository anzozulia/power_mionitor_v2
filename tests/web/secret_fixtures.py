"""Shared bot-token fixtures for every secret scan (INV-23 #2, SEC-04; TEST-STRATEGY §5.3).

Imported as ``from secret_fixtures import ...``: a helper module, not a conftest (test
directories have no ``__init__.py``). Not named ``secrets``, which would shadow the stdlib
module that device keys and Django's crypto use.

Every scan looks for the same strings: the full token, its secret part and its mask
``{bot_id}:••••••••``. ``SECRETS`` holds the values no response may ever contain.
"""

SECRET = "Sx_9-Qw7Lm" * 4
TOKEN = f"987654321:{SECRET}"
MASKED = "987654321:••••••••"
# Typed into an edit form that is invalid for another reason: never saved, never shown.
SECRET_2 = "Zq-8_Lp4Rt" * 4
TOKEN_2 = f"123123123:{SECRET_2}"
MASKED_2 = "123123123:••••••••"
# Saved by the last valid edit: from then on only its mask shows.
SECRET_3 = "Hy7_-Kd2Wv" * 4
TOKEN_3 = f"456456456:{SECRET_3}"
MASKED_3 = "456456456:••••••••"
SECRETS = (TOKEN, SECRET, TOKEN_2, SECRET_2, TOKEN_3, SECRET_3)
