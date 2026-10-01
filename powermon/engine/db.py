"""Database expressions shared by the engine models and their migrations.

``TsTzRange`` must stay at exactly this import path: migration 0002 references
``powermon.engine.db.TsTzRange`` in the power_interval exclusion constraint (P-9), so
moving or renaming it breaks ``migrate`` on every existing database.
"""

from django.contrib.postgres.fields import DateTimeRangeField
from django.db.models import Func


class TsTzRange(Func):
    """``TSTZRANGE(start, end, bounds)``: the span an interval covers (end NULL = open)."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()
