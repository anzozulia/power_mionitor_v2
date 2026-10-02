"""Failed admin sign-ins, counted per client IP for the login throttle (SEC-03, D-16).

Five failed sign-ins within 60 s from one client IP make every sign-in POST from that IP
answer HTTP 429 for 5 minutes (``powermon.throttle.rules``). The table is small: only the
client IP and the time of each failure, never a username or a password, and rows older
than an hour are pruned on each insert (``powermon.throttle.store``). A database table,
not process memory, because gunicorn runs several processes and threads that must all see
the same count.

No timestamp column has a database default: every time comes from the caller's Clock
(Pitfall 3).
"""

from django.db import models


class LoginFailure(models.Model):
    """One failed sign-in from one client IP."""

    # The client IP from throttle.rules.client_ip (the address Caddy saw).
    client_ip = models.GenericIPAddressField()
    failed_at = models.DateTimeField()

    class Meta:
        db_table = "login_failure"
        indexes = [
            # The throttle's lookup: one IP's recent failures, in time order.
            models.Index(fields=["client_ip", "failed_at"], name="login_failure_ip_at")
        ]

    def __str__(self) -> str:
        return f"login failure {self.pk}: {self.client_ip} at {self.failed_at.isoformat()}"
