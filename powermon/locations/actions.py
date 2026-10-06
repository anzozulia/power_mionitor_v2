"""The admin's writes on a location (D-05, D-07, D-09, D-14; LOC-04, LOC-06, LOC-09,
LOC-10, DATA-04).

Each configuration write is one explicit, column-limited conditional UPDATE, decided by its
row count, and never ``Model.save()`` (INV-02 #3): a save writes every column from an
instance read earlier, so a stale page could switch a toggle back or bring a replaced
device key back. None of them touches ``location_state``: they are configuration, not
engine transitions. The maintenance switch is an engine transition and lives in
``powermon.engine.maintenance`` (D-02). The delete is the one exception: it takes the
``location_state`` row lock first, like every timeline writer, and bumps ``state_version``
(D-09). Nothing here does network I/O (KD2).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from django.db import connection, transaction

from powermon.alerts import outbox
from powermon.alerts.models import OpsIncident, OutboxMessage
from powermon.engine.maintenance import BUMP_SQL
from powermon.engine.transitions import LOCK_SQL
from powermon.locations import keys
from powermon.locations.models import Location

# The configuration-only switches (D-05). Maintenance is not one of them: it writes the
# timeline under the state row lock (``engine.maintenance.set_maintenance``).
FlagField = Literal["alerts_enabled", "router_grace"]
FLAG_FIELDS: tuple[str, ...] = get_args(FlagField)

# The columns the edit form writes on every save (D-07). The bot token is written only
# when a new one was typed. The switches, the device key and the live state are never
# among them, so a form loaded earlier can never revert what changed since (INV-02 #3).
CONFIG_FIELDS = ("name", "period_s", "grace_s", "chat_id", "language", "chart_refresh_min")


@dataclass(frozen=True)
class ConfigSaved:
    """What ``update_config`` did: ``found`` False when the location is gone (nothing written);
    ``channel_changed`` when the chat ID or the bot token changed (D-08)."""

    found: bool
    channel_changed: bool


def update_config(location_id: int, data: Mapping[str, Any], now: datetime) -> ConfigSaved:
    """Save the edit form's configuration of the location at ``now`` (D-07, D-08).

    One transaction. The stored chat ID and token are read first with ``SELECT ... FOR NO
    KEY UPDATE`` on the location row (it does not block the KEY SHARE that a concurrent
    outbox insert takes through its foreign key), then one UPDATE writes exactly
    ``CONFIG_FIELDS`` from ``data``, plus ``bot_token`` only when ``data["bot_token"]`` is
    not empty (an empty token keeps the current one). Never ``Model.save()``: the
    switches, ``device_key`` and ``location_state`` are not written, so with two stale
    forms the last save wins for these fields only, and the status, the outage start and
    the switches can never be reverted (INV-02 #3).

    Thresholds apply from the next detection cycle, which reads them afresh, and the
    stored timeline is never recomputed: raising grace changes no past row or total, and
    lowering it below the current silence makes the next cycle record OFF from the last
    heartbeat (DATA-04, INV-06).

    A new chat ID or a new token (one that differs from the stored one) is a channel
    change (D-08): in the same transaction the location's pending subscriber alerts become
    due at ``now`` (``outbox.make_due``), so a 15-minute backoff earned in the old channel
    does not hold them; the relay renders them at send time with the current bot and chat,
    so they go to the new channel. The chart moves on its own: the worker's chart planner
    sees records whose chat or bot no longer match and releases them (04-06). A change of
    only the name or the language needs neither: the next chart refresh shows it. A new
    chart update period needs nothing either: the worker's chart planner reads it on every
    pass (quick task 261006-of9).

    ``found`` is False, with nothing written, for an unknown or deleted location (a
    delete that commits first is seen: the locking read re-checks ``deleted_at``). No
    network I/O (KD2): nothing is sent to Telegram on save.
    """
    if now.utcoffset() is None:
        raise ValueError("update_config needs an aware now, not a naive datetime")
    with transaction.atomic():
        stored = (
            Location.objects.select_for_update(no_key=True)
            .filter(pk=location_id, deleted_at__isnull=True)
            .values("chat_id", "bot_token")
            .first()
        )
        if stored is None:
            return ConfigSaved(found=False, channel_changed=False)
        fields = {name: data[name] for name in CONFIG_FIELDS}
        new_token = str(data.get("bot_token") or "")
        if new_token:
            fields["bot_token"] = new_token
        Location.objects.filter(pk=location_id, deleted_at__isnull=True).update(**fields)
        channel_changed = fields["chat_id"] != stored["chat_id"] or (
            bool(new_token) and new_token != stored["bot_token"]
        )
        if channel_changed:
            outbox.make_due(location_id, now)
    return ConfigSaved(found=True, channel_changed=channel_changed)


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


def regenerate_key(location_id: int, current_key: str) -> bool:
    """Replace the device key ``current_key`` with a new one: True if this call replaced it.

    One UPDATE that stores a new 32-character key (``keys.generate_device_key``), only
    while the location is not deleted and still has ``current_key`` (D-14). So a second
    call with the same old key (a resubmitted form, a second tab) or a call that lost a
    race with another regeneration finds no row and replaces nothing (UI-D7). The
    heartbeat looks its location up by key, so the old key gets 401 from the moment this
    commits, and the history is not touched (INV-24 #1).

    ``device_key`` is UNIQUE, so the UPDATE takes the row's FOR UPDATE lock and waits
    briefly for an in-flight heartbeat whose outbox insert holds KEY SHARE on it; it holds
    no other lock, so it cannot deadlock (RESEARCH Pattern 7).
    """
    replaced = Location.objects.filter(
        pk=location_id, deleted_at__isnull=True, device_key=current_key
    ).update(device_key=keys.generate_device_key())
    return replaced == 1


def delete_location(location_id: int, now: datetime) -> bool:
    """Delete the location at ``now``: True if this call deleted it (D-09, LOC-04, INV-19 #2).

    One transaction, in this order:

    1. the location's ``location_state`` row lock (``transitions.LOCK_SQL``, imported, never
       copied), so the delete serializes with heartbeats, the OFF transition and the
       maintenance toggle; lock order is state row, then location row, as every writer's;
    2. the ``deleted_at`` tombstone, only while the location is not deleted yet: False,
       with nothing else written, for an unknown or already deleted location, so a second
       delete (a double click, a second tab) changes nothing (UI-D8);
    3. the ``state_version`` bump (``maintenance.BUMP_SQL``, the D-02 toggle's), so a
       detector snapshot read before the delete loses its OFF CAS on ``state_version`` as
       well as on ``deleted_at``;
    4. the location's pending subscriber alerts dropped with last_error="location_deleted"
       (``outbox.LOCATION_DELETED``): never sent;
    5. its open ops incidents closed at ``now`` without a recovery notice.

    The races the transaction alone does not close, and where each is handled:

    - a heartbeat that looked up its key before the delete and waits on the row lock sees
      the tombstone under the lock and writes nothing (``record_heartbeat``, 04-03);
    - a detector snapshot read before the delete: its ``mark_off`` waits on the same row
      lock and its OFF CAS then matches no row (``state_version`` bumped here, ``deleted_at``
      set), so no OFF, no off interval and no OFF alert;
    - a send in flight ("sending") is not pending, so it is not dropped here; if its outcome
      puts it back to pending, the relay never sends it (``subscriber_heads`` skips deleted
      locations) and drops it on its next pass (``drop_deleted_pending``, 04-05);
    - a concurrent permanent refusal: ``delivery.open_failing`` re-reads the location
      ``FOR SHARE`` and writes nothing for a tombstone, so it either sees the delete and
      opens nothing, or commits first and has its incident closed here without a notice.

    Pending ops notices about the location are left alone: they render from the tombstone's
    name, and dropping by location would also drop a global notice that names it. The
    tombstone keeps its bot token, because the worker's chart release still needs it to
    unpin the location's charts in their stored chats (04-06). History rows stay; there is
    no undelete. No network I/O (KD2).
    """
    if now.utcoffset() is None:
        raise ValueError("delete_location needs an aware now, not a naive datetime")
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(LOCK_SQL, [location_id])
        cur.fetchone()
        tombstoned = Location.objects.filter(pk=location_id, deleted_at__isnull=True).update(
            deleted_at=now
        )
        if tombstoned != 1:
            return False
        cur.execute(BUMP_SQL, {"id": location_id})
        OutboxMessage.objects.filter(
            channel=outbox.CHANNEL_SUBSCRIBER, location_id=location_id, status="pending"
        ).update(status="dropped", last_error=outbox.LOCATION_DELETED)
        OpsIncident.objects.filter(location_id=location_id, ended_at__isnull=True).update(
            ended_at=now
        )
    return True
