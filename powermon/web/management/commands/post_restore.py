"""``manage.py post_restore``: restart every location silently after a restore (OPS-06).

When it runs: after a dump was restored into an empty database (``backup.sh --restore``)
and ``release`` has migrated it, before web and worker start. It sets every monitored
location back to "waiting for first heartbeat" with the lost hours not monitored, drops
every message the dump had queued and closes its open incidents, all without a message
(D-13, D-14). The worker's first start then sends one "monitoring gap" notice to the
admin, and no subscriber gets anything (SC4). See the README section "Restore from a
backup".

It refuses, writing nothing, while any session holds the worker lock: stop web and worker
first. Run a second time it changes nothing. The logic lives in
``powermon.engine.restore``; this command only reads the clock and prints the counts,
never a token or a device key.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from powermon.clock import Clock, SystemClock
from powermon.engine import restore


class Command(BaseCommand):
    help = (
        "After a restore: set every location waiting for its first heartbeat, drop the "
        "queued messages and close the open incidents, without sending anything."
    )
    # Tests replace it with a FakeClock.
    clock: Clock = SystemClock()

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.CFG.build:
            raise CommandError("post_restore cannot run in build mode (APP_BUILD=1)")
        try:
            counts = restore.restart_after_restore(self.clock.now())
        except restore.WorkerActive as exc:
            raise CommandError(
                "a worker holds the worker lock: stop web and worker before post_restore"
            ) from exc
        # Counts only: no name, token or key reaches the output.
        self.stdout.write(
            f"post_restore: {counts.locations} location(s) now wait for their first "
            f"heartbeat, {counts.dropped} queued message(s) dropped, "
            f"{counts.incidents} open incident(s) closed"
        )
