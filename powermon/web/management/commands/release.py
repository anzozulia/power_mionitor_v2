"""``manage.py release``: apply migrations, then sync the env admin account (D-02, LOC-01).

The one-shot ``migrate`` service runs this on every deploy, before web and the worker
start (they wait for it with ``service_completed_successfully``). So migrations run
exactly once per deploy, and a failed migration stops the deploy before the admin
account or the new app is touched.

Migrations run with no statement timeout. The shared DATABASES OPTIONS cap every web
statement at 5 s (D-16), and a migration on a large table can take longer, so the first
statement sets ``statement_timeout = 0`` on the same connection, in the same thread,
that ``migrate`` then uses (RESEARCH Pitfall 7).

Env changes take effect with the deploy command
(``docker compose -f docker-compose.prod.yml up -d --build --wait``), not with
``docker compose restart``, which neither re-reads env_file nor runs this command (P-14).
"""

from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

from powermon.web.admin_sync import sync_admin


class Command(BaseCommand):
    help = "Apply pending migrations, then sync the single admin account from the env."

    def handle(self, *args: Any, **options: Any) -> None:
        # Session-level (autocommit): it holds for every statement migrate runs below.
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")
        streams = {"stdout": self.stdout, "stderr": self.stderr}
        call_command("migrate", interactive=False, verbosity=options["verbosity"], **streams)
        sync_admin(settings.CFG.admin_username, settings.CFG.admin_password)
        # The username only: the password never reaches any output.
        self.stdout.write(f"admin account synced: {settings.CFG.admin_username}")
