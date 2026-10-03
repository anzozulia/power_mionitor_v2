"""``manage.py history_fingerprint``: per-table counts and checksums for the restore drill.

Read only. It prints one line per table, ``<table> <row count> <md5 checksum>``, for
``location``, ``power_interval`` and ``chart_message`` (D-16). The INV-25 #2 restore drill
runs it on the source database and on the restored one and compares the two outputs. Run
it before ``post_restore`` on the restored side, because ``post_restore`` changes the open
pieces and ``location_state``. Run it through ``manage.py`` on both sides: Django's
session time zone is UTC, so timestamps have the same text (RESEARCH Pitfall 14). See the
README section "Restore from a backup".

The logic lives in ``powermon.engine.restore.fingerprint``, which runs in its own read-only
transaction. The output holds counts and checksums only: the bot token and the device key
enter the location checksum as md5 inside the aggregate and are never printed.
"""

from typing import Any

from django.core.management.base import BaseCommand

from powermon.engine import restore


class Command(BaseCommand):
    help = (
        "Print the row count and checksum of location, power_interval and chart_message "
        "(read only), to compare a restored database with its source."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        for table, count, checksum in restore.fingerprint():
            self.stdout.write(f"{table} {count} {checksum}")
