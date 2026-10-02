"""The login throttle's failure store: the ``login_failure`` table (SEC-03, D-16).

Every function works on the caller's database connection, takes its time from the
caller's clock (``now``) and never uses SQL ``now()``. The decision itself is the pure
``powermon.throttle.rules.blocked``; this module only reads and writes the rows it needs.
"""

from datetime import datetime

from django.db import transaction

from powermon.throttle import rules
from powermon.throttle.models import LoginFailure


def failures(ip: str, now: datetime) -> list[datetime]:
    """The IP's failure times newer than ``now - LOOKBACK`` (strict), oldest first.

    Older failures can no longer take part in an active cool-down (rules.LOOKBACK).
    """
    return list(
        LoginFailure.objects.filter(client_ip=ip, failed_at__gt=now - rules.LOOKBACK)
        .order_by("failed_at")
        .values_list("failed_at", flat=True)
    )


def record_failure(ip: str, now: datetime) -> None:
    """Record one failed sign-in from ``ip`` and prune rows older than PRUNE_AFTER.

    One transaction: the insert and the prune of every IP's old rows commit together.
    """
    with transaction.atomic():
        LoginFailure.objects.create(client_ip=ip, failed_at=now)
        LoginFailure.objects.filter(failed_at__lt=now - rules.PRUNE_AFTER).delete()


def clear(ip: str) -> int:
    """Forget the IP's failures (a successful sign-in); the number of rows deleted."""
    deleted, _ = LoginFailure.objects.filter(client_ip=ip).delete()
    return deleted


def is_blocked(ip: str, now: datetime) -> bool:
    """True while the IP's cool-down runs at ``now``."""
    return rules.blocked(failures(ip, now), now)
