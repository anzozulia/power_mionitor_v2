"""The admin's configuration writes on a location (D-05, LOC-09, LOC-10).

Each write is one explicit, column-limited conditional UPDATE, decided by its row count,
and never ``Model.save()`` (INV-02 #3): a save writes every column from an instance read
earlier, so a stale page could switch a toggle back. None of them does network I/O (KD2),
and none touches ``location_state``: they are configuration, not engine transitions. The
maintenance switch is an engine transition and lives in ``powermon.engine.maintenance``
(D-02).
"""

from typing import Literal, get_args

from powermon.locations.models import Location

# The configuration-only switches (D-05). Maintenance is not one of them: it writes the
# timeline under the state row lock (``engine.maintenance.set_maintenance``).
FlagField = Literal["alerts_enabled", "router_grace"]
FLAG_FIELDS: tuple[str, ...] = get_args(FlagField)


def set_flag(location_id: int, field: FlagField, value: bool) -> bool:
    """Set one switch of the location: True if it changed, False if it already had ``value``.

    One UPDATE of that one column, only while it differs and the location is not deleted,
    so the same state again writes nothing (UI-D3) and a deleted location is never
    written. No ``location_state`` lock and no ``state_version`` bump: the detector reads
    the settings afresh every cycle, so router grace changes only decisions made after it
    (K-4), and the heartbeat gate and the OFF CAS read ``alerts_enabled`` inside their
    own transactions, so alerts off changes only transitions recorded after it; alerts
    already queued still go out (INV-05, D-06).

    ValueError for any other column and TypeError for a value that is not a bool, both
    before any write.
    """
    if field not in FLAG_FIELDS:
        raise ValueError(f"set_flag() sets only {', '.join(FLAG_FIELDS)}, not {field!r}")
    if not isinstance(value, bool):
        raise TypeError(f"set_flag() needs a bool value, not {type(value).__name__}")
    changed = (
        Location.objects.filter(pk=location_id, deleted_at__isnull=True)
        .exclude(**{field: value})
        .update(**{field: value})
    )
    return changed == 1
