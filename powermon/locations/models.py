"""The location record: identity, settings, bot and device key (LOC-02, D-07, D-10, D-12).

Configuration only. The live state lives in ``powermon.engine.models.LocationState`` and
is never written by a configuration form (INV-02 config/state split). PostgreSQL enforces
the setting ranges itself (K-6 defence in depth), so no code path can store a bad value.
"""

from django.db import models
from django.db.models import Q

# Setting bounds in seconds (D-10). The form enforces them too (01-10).
MIN_SECONDS = 10
MAX_SECONDS = 3600
# Subscriber-facing languages (D-15); the admin UI itself is English only.
LANGUAGE_CHOICES = [("uk", "Ukrainian"), ("en", "English"), ("ru", "Russian")]
LANGUAGES = tuple(code for code, _ in LANGUAGE_CHOICES)

# CHECK conditions, enforced by PostgreSQL.
PERIOD_IN_RANGE = Q(period_s__gte=MIN_SECONDS, period_s__lte=MAX_SECONDS)
GRACE_IN_RANGE = Q(grace_s__gte=MIN_SECONDS, grace_s__lte=MAX_SECONDS)
LANGUAGE_KNOWN = Q(language__in=LANGUAGES)


class Location(models.Model):
    """A monitored place with one device key, one bot and one channel."""

    # Not unique (D-09): a typo is fixed by creating a new location.
    name = models.CharField(max_length=100)
    period_s = models.IntegerField(default=60)
    grace_s = models.IntegerField(default=30)
    # Router-reconnect grace: engine rules use it now; its UI arrives with LOC-09 (D-10).
    router_grace = models.BooleanField(default=False)
    maintenance = models.BooleanField(default=False)
    alerts_enabled = models.BooleanField(default=True)
    language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES, default="uk")
    # Write-only in the UI, masked wherever it is shown (D-11).
    bot_token = models.CharField(max_length=255)
    # Channel IDs are -100... and need 64 bits (D-12).
    chat_id = models.BigIntegerField()
    device_key = models.CharField(max_length=32, unique=True)
    # Tombstone (Phase 4). The heartbeat lookup and the gates already ignore deleted rows.
    deleted_at = models.DateTimeField(null=True, blank=True)
    # Set by the caller from its Clock; never a database default (Pitfall 3).
    created_at = models.DateTimeField()

    class Meta:
        db_table = "location"
        constraints = [
            models.CheckConstraint(condition=PERIOD_IN_RANGE, name="location_period_10_3600"),
            models.CheckConstraint(condition=GRACE_IN_RANGE, name="location_grace_10_3600"),
            models.CheckConstraint(condition=LANGUAGE_KNOWN, name="location_language_valid"),
        ]

    def __str__(self) -> str:
        return self.name
