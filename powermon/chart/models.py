"""The chart messages posted to each location's chat (CHRT-05, D-01 to D-04, INV-17).

``ChartMessage`` is one row per posted chart message: the location, the local date (in
the display time zone) the chart belongs to, the chat it was actually sent to and the
message id Telegram gave it. The worker writes the row right after a successful send and
before any pin (INV-17), so a pinned chart always has a record and a pin never leaves an
orphan behind. ``pinned`` is a separate field, set only once the pin succeeded. Every
later edit, pin and unpin targets the chat stored here, never the location's current
chat (D-04).

The partial unique index ``chart_message_one_active_per_day`` allows at most one active
(not retired) row per location and local date, so a second post for a day can never be
recorded (INV-17 #2); a retired row (the message is gone) does not block its replacement.

The row holds ids, dates and times only, never a token or a text (OPS-08). No timestamp
column has a database default: every time comes from the worker's Clock (Pitfall 3).
Pillow-free: the web process imports this module through the app registry.
"""

from django.db import models
from django.db.models import Q

from powermon.locations.models import Location

# A record is active until its message is gone (retired).
ACTIVE = Q(retired_at__isnull=True)


class ChartMessage(models.Model):
    """One chart message in a location's chat, posted for one local date."""

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        db_column="location_id",
        related_name="chart_messages",
    )
    # The display-time-zone date the chart is for.
    local_date = models.DateField()
    # The chat the message was sent to; edits, pins and unpins use this one (D-04).
    chat_id = models.BigIntegerField()
    message_id = models.BigIntegerField()
    # Set once the pin succeeded; the record exists before any pin (INV-17).
    pinned = models.BooleanField(default=False)
    # The last permanent pin failure; the pin is tried again after the next render (D-07).
    pin_failed_at = models.DateTimeField(null=True)
    # When Telegram answered the last successful post or edit; the refresh cadence (D-05).
    last_rendered_at = models.DateTimeField()
    # The finished-day edit is done (03-09).
    finalized_at = models.DateTimeField(null=True)
    # The message is gone ("not found", 03-09); the day may get a new record.
    retired_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "chart_message"
        constraints = [
            models.UniqueConstraint(
                fields=["location", "local_date"],
                condition=ACTIVE,
                name="chart_message_one_active_per_day",
            )
        ]

    def __str__(self) -> str:
        return (
            f"chart message {self.pk}: location {self.location_id}, {self.local_date}, "
            f"message {self.message_id}"
        )
