"""The single admin account, synced from the env on every deploy (LOC-01, INV-21).

``manage.py release`` (the one-shot migrate service, D-02) calls ``sync_admin`` after
migrating, so after each deploy exactly one account exists and only the
ADMIN_USERNAME / ADMIN_PASSWORD from the env sign in.
"""

from django.contrib.auth import get_user_model
from django.db import transaction


def sync_admin(username: str, password: str) -> None:
    """Make ``username`` / ``password`` the one and only account.

    - Keeps the account already named ``username``, else the oldest account (renamed, so
      an ADMIN_USERNAME change never adds a second account), else creates one.
    - Deletes every other account.
    - Re-hashes only when the password changed: a new hash on every deploy would change
      the session hash and sign the admin out (P-13).
    - Never logs the password.
    """
    if not username.strip():
        raise ValueError("admin username must not be empty")
    if not password.strip():
        raise ValueError("admin password must not be empty")

    user_model = get_user_model()
    with transaction.atomic():
        users = list(user_model.objects.select_for_update().order_by("id"))
        keep = next((u for u in users if u.username == username), users[0] if users else None)
        extra = [u.pk for u in users if u is not keep]
        if extra:
            user_model.objects.filter(pk__in=extra).delete()
        if keep is None:
            keep = user_model(username=username)
        keep.username = username
        keep.is_active = True
        if not keep.has_usable_password() or not keep.check_password(password):
            keep.set_password(password)
        keep.save()
