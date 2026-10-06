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
# Chart update period in minutes (CHRT-02 amended by quick task 261006-of9). Each value
# divides 60, so updates fall on clock minutes.
CHART_REFRESH_CHOICES = [
    (1, "1 min"),
    (5, "5 min"),
    (10, "10 min"),
    (15, "15 min"),
    (30, "30 min"),
    (60, "1 hour"),
]
CHART_REFRESH_MINUTES = tuple(minutes for minutes, _ in CHART_REFRESH_CHOICES)
DEFAULT_CHART_REFRESH_MIN = 15

# CHECK conditions, enforced by PostgreSQL.
PERIOD_IN_RANGE = Q(period_s__gte=MIN_SECONDS, period_s__lte=MAX_SECONDS)
GRACE_IN_RANGE = Q(grace_s__gte=MIN_SECONDS, grace_s__lte=MAX_SECONDS)
LANGUAGE_KNOWN = Q(language__in=LANGUAGES)
CHART_REFRESH_KNOWN = Q(chart_refresh_min__in=CHART_REFRESH_MINUTES)


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
    # db_default keeps DEFAULT 15 in the database, so old code on this schema can still
    # insert a location (a rollback, README section 8); default keeps an unsaved instance
    # at the int 15. IntegerField, not PositiveSmallIntegerField: the latter's own CHECK
    # would sort before location_chart_refresh_valid.
    chart_refresh_min = models.IntegerField(
        choices=CHART_REFRESH_CHOICES,
        default=DEFAULT_CHART_REFRESH_MIN,
        db_default=DEFAULT_CHART_REFRESH_MIN,
    )
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
            models.CheckConstraint(
                condition=CHART_REFRESH_KNOWN, name="location_chart_refresh_valid"
            ),
        ]

    def __str__(self) -> str:
        return self.name
