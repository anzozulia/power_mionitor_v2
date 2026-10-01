"""The engine's tables: live state, the stored power timeline, and the system singleton.

- ``LocationState`` (MON-01, KD2): only the engine's gate SQL writes it
  (``powermon.engine.transitions``), each time as one conditional UPDATE. No configuration
  form ever touches it (INV-02 config/state split). ``state_version`` is the
  compare-and-swap token: every write bumps it.
- ``PowerInterval`` (KD1): the single stored timeline that alerts, the chart and the
  totals read. Only ``powermon.engine.timeline.set_open_state`` writes it, in the same
  transaction as the state change. PostgreSQL itself rejects overlapping, zero-length and
  second open intervals (PITFALLS 15), so no code path can corrupt it.
- ``SystemState``: one row (id 1) with the process-level anchors of the detection window.

No timestamp column has a database default: every time comes from the caller's Clock
(Pitfall 3).
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Q

from powermon.engine.db import TsTzRange
from powermon.locations.models import Location

STATUSES = ("waiting", "on", "off")
# "no data" is never stored: it is the absence of an interval (before the first
# heartbeat, after a history reset, the future).
INTERVAL_STATES = ("on", "off", "not_monitored")

# CHECK conditions, enforced by PostgreSQL.
STATUS_KNOWN = Q(status__in=STATUSES)
OFF_HAS_OUTAGE_START = ~Q(status="off") | Q(outage_started_at__isnull=False)
INTERVAL_STATE_KNOWN = Q(state__in=INTERVAL_STATES)
END_AFTER_START = Q(end_at__isnull=True) | Q(end_at__gt=F("start_at"))
OUTAGE_START_IFF_OFF = Q(state="off", outage_start_at__isnull=False) | (
    ~Q(state="off") & Q(outage_start_at__isnull=True)
)
IS_OPEN = Q(end_at__isnull=True)
SINGLETON_ID = Q(id=1)


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


class PowerInterval(models.Model):
    """One span of the timeline: ``[start_at, end_at)``, open while ``end_at`` is NULL.

    An off interval carries ``outage_start_at``, the outage it belongs to. Two adjacent
    off intervals with different outage starts are two outages (INV-01 acceptance 2).
    """

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        db_column="location_id",
        related_name="intervals",
    )
    state = models.CharField(max_length=16)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField(null=True)
    outage_start_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "power_interval"
        constraints = [
            models.CheckConstraint(
                condition=INTERVAL_STATE_KNOWN, name="power_interval_state_valid"
            ),
            models.CheckConstraint(
                condition=END_AFTER_START, name="power_interval_end_after_start"
            ),
            models.CheckConstraint(
                condition=OUTAGE_START_IFF_OFF, name="power_interval_outage_start_iff_off"
            ),
            # One open interval per location. It is also the index that finds the open
            # interval; the exclusion constraint below rejects a second one as well.
            models.UniqueConstraint(
                fields=["location"], condition=IS_OPEN, name="power_interval_one_open"
            ),
            # btree_gist (migration 0002) lets the gist index compare location_id with =.
            ExclusionConstraint(
                name="power_interval_no_overlap",
                expressions=[
                    (TsTzRange("start_at", "end_at", RangeBoundary()), RangeOperators.OVERLAPS),
                    ("location", RangeOperators.EQUAL),
                ],
            ),
        ]
        indexes = [models.Index(fields=["location", "start_at"], name="power_interval_loc_start")]

    def __str__(self) -> str:
        return f"location {self.location_id}: {self.state} [{self.start_at}, {self.end_at})"


class SystemState(models.Model):
    """The single process-level row (id 1), created by migration 0002.

    ``detection_resumed_at`` is the worker start (D-14): every location that is on gets a
    fresh detection window from it. ``web_started_at`` and ``last_cycle_completed_at``
    arrive with the lapse carve in Phase 2.
    """

    id = models.SmallIntegerField(primary_key=True, default=1)
    web_started_at = models.DateTimeField(null=True)
    last_cycle_completed_at = models.DateTimeField(null=True)
    detection_resumed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "system_state"
        constraints = [
            models.CheckConstraint(condition=SINGLETON_ID, name="system_state_singleton")
        ]

    def __str__(self) -> str:
        return f"system state {self.pk}"
