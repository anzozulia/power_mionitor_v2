"""Web views."""

import logging

from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)


@login_not_required
@require_GET
@never_cache
def healthz(request: HttpRequest) -> HttpResponse:
    """Container health check: 200 ``ok`` after one real query, 503 if the database fails."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        # No exception text: driver errors can carry connection details.
        log.warning("healthz: database query failed")
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
