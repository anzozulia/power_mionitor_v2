"""The alert outbox table (KD2, D-14): the only way an alert leaves the system.

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
