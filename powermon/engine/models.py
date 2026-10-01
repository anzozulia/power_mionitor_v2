"""The live state of each location (MON-01, KD2).

Only the engine's gate SQL writes this table (``powermon.engine.transitions``), each time
as one conditional UPDATE. No configuration form ever touches it (INV-02 config/state
split). ``state_version`` is the compare-and-swap token: every write bumps it.
"""

from django.db import models
from django.db.models import Q

from powermon.locations.models import Location

STATUSES = ("waiting", "on", "off")

# CHECK conditions, enforced by PostgreSQL.
STATUS_KNOWN = Q(status__in=STATUSES)
OFF_HAS_OUTAGE_START = ~Q(status="off") | Q(outage_started_at__isnull=False)


class LocationState(models.Model):
    """One row per location, created together with the location in status "waiting"."""

    location = models.OneToOneField(
        Location,
        primary_key=True,
        on_delete=models.CASCADE,
        db_column="location_id",
        related_name="state",
    )
    status = models.CharField(max_length=8, default="waiting")
    state_version = models.BigIntegerField(default=0)
    # Advanced with GREATEST(), so an older timestamp never moves it back (D-08).
    last_heartbeat_at = models.DateTimeField(null=True)
    on_since = models.DateTimeField(null=True)
    outage_started_at = models.DateTimeField(null=True)
    # Maintenance-exit detection window (Phase 4).
    window_start_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "location_state"
        constraints = [
            models.CheckConstraint(condition=STATUS_KNOWN, name="location_state_status_valid"),
            models.CheckConstraint(
                condition=OFF_HAS_OUTAGE_START, name="location_state_off_needs_outage_start"
            ),
        ]

    def __str__(self) -> str:
        return f"location {self.pk}: {self.status}"
