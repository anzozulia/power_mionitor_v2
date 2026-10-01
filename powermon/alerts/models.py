"""The alert outbox (KD2, D-14) and the ops incidents (D-11).

``OutboxMessage`` is the only way an alert leaves the system.
A transition inserts its row in the same transaction as the state change
(``powermon.alerts.outbox.enqueue``). The worker relay drains the table one row at a
time (01-11). The relay sends with no transaction open and, per location, oldest row
first, so OFF always goes before ON.

Row statuses (RESEARCH Pattern 5):
- pending: waiting to be sent; ``next_attempt_at`` says when it is due.
- sending: claimed by the relay just before the HTTP call.
- sent: Telegram accepted it.
- uncertain: the send may have reached Telegram (read timeout, dropped connection); never
  resent (at-most-once, INV-16).
- expired, dropped: Phase 2 (``expires_at`` enforcement, ALRT-03) and ops handling.

A row never holds secrets or text (T-01-45): ``payload`` carries integer microsecond
durations only, the text is rendered at send time, the bot token and chat are read from
the location at send time, and ``last_error`` is a short code, never a URL or a token.
``OpsIncident`` (ARCHITECTURE Pattern 10, D-11) records an incident about the server's own
state that the admin is told about: a monitoring gap (opened and closed in one
transaction by the lapse carve, ``powermon.engine.lapse``), an all-silent spell (02-08)
and, in Phase 4, a failing delivery. "Exactly one notice per incident" is the database's
job, not a code convention: the partial unique index ``ops_incident_one_open`` allows at
most one open incident (``ended_at`` NULL) per kind and location, with a NULL location
counted as one value (NULLS NOT DISTINCT), so a second opener's
``INSERT ... ON CONFLICT DO NOTHING`` adds no row and sends no notice. ``details`` holds
integers only (OPS-08).

No timestamp column has a database default: every time comes from the caller's Clock
(Pitfall 3).
"""

from django.db import models
from django.db.models import Q

from powermon.locations.models import Location

CHANNELS = ("subscriber", "ops")
STATUSES = ("pending", "sending", "sent", "uncertain", "expired", "dropped")
# Rows the relay still has to deal with; the partial index covers only these.
OPEN_STATUSES = ("pending", "sending")

# CHECK conditions, enforced by PostgreSQL.
CHANNEL_KNOWN = Q(channel__in=CHANNELS)
STATUS_KNOWN = Q(status__in=STATUSES)
IS_OPEN = Q(status__in=OPEN_STATUSES)
# An incident is open until it has an end.
INCIDENT_IS_OPEN = Q(ended_at__isnull=True)


class OutboxMessage(models.Model):
    """One alert waiting for (or done with) delivery."""

    channel = models.CharField(max_length=16)
    # Null only for ops notices (Phase 2), which have no location.
    location = models.ForeignKey(
        Location,
        null=True,
        on_delete=models.CASCADE,
        db_column="location_id",
        related_name="outbox_messages",
    )
    kind = models.CharField(max_length=32)
    # When the event happened (outage start for OFF, restore time for ON).
    event_at = models.DateTimeField()
    # When the transition was recorded; expiry and the late-alert rule count from here.
    recorded_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="pending")
    attempts = models.IntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    # A short code such as "http_502" or "read_timeout"; never a URL or a token.
    last_error = models.CharField(max_length=64, default="", blank=True)
    sent_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "outbox_message"
        constraints = [
            models.CheckConstraint(condition=CHANNEL_KNOWN, name="outbox_channel_valid"),
            models.CheckConstraint(condition=STATUS_KNOWN, name="outbox_status_valid"),
        ]
        indexes = [
            # The relay's head-of-line query: the oldest open row of each location.
            models.Index(
                fields=["channel", "location", "id"], name="outbox_open_idx", condition=IS_OPEN
            )
        ]

    def __str__(self) -> str:
        return f"outbox {self.pk}: {self.kind} for location {self.location_id} ({self.status})"


class OpsIncident(models.Model):
    """One incident the admin hears about: ``[started_at, ended_at]``, open while no end."""

    # monitoring_gap | all_silent (02-08) | delivery_failing (Phase 4)
    kind = models.CharField(max_length=32)
    # Null for an incident about the whole system (a monitoring gap, all-silent).
    location = models.ForeignKey(
        Location,
        null=True,
        on_delete=models.CASCADE,
        db_column="location_id",
        related_name="ops_incidents",
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True)
    # Integers only, like an ops payload (OPS-08).
    details = models.JSONField(default=dict)

    class Meta:
        db_table = "ops_incident"
        constraints = [
            # At most one open incident per kind and location; NULL locations count as
            # one value (PostgreSQL 15+ NULLS NOT DISTINCT, RESEARCH spike 8).
            models.UniqueConstraint(
                fields=["kind", "location"],
                condition=INCIDENT_IS_OPEN,
                nulls_distinct=False,
                name="ops_incident_one_open",
            )
        ]

    def __str__(self) -> str:
        return (
            f"ops incident {self.pk}: {self.kind} for location {self.location_id} "
            f"[{self.started_at}, {self.ended_at}]"
        )
