"""URL routes."""

from django.urls import path

from powermon.web.views import HeartbeatView, healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    # The exact device URL, with no trailing slash and no slash redirect: device clients do
    # not follow redirects, and v1's slash redirect echoed the key in its Location header.
    path("hb", HeartbeatView.as_view(), name="heartbeat"),
]
