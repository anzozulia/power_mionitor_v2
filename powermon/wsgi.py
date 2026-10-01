"""WSGI entrypoint for gunicorn."""

import os

from django.core.wsgi import get_wsgi_application

if os.environ.get("APP_BUILD") == "1":
    # Build mode skips every secret and database check; it must never serve requests.
    raise RuntimeError("APP_BUILD=1 is build mode only; refusing to serve requests")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powermon.settings")

application = get_wsgi_application()
