"""URL routes."""

from django.urls import path

from powermon.web.location_views import LocationDetailView, MaintenanceSwitchView
from powermon.web.views import (
    HeartbeatView,
    LocationCreateView,
    LocationListView,
    LocationSetupView,
    SignInView,
    SignOutView,
    healthz,
)

urlpatterns = [
    path("", LocationListView.as_view(), name="location-list"),
    path("locations/new/", LocationCreateView.as_view(), name="location-create"),
    path("locations/<int:pk>/", LocationDetailView.as_view(), name="location-detail"),
    path(
        "locations/<int:pk>/maintenance/",
        MaintenanceSwitchView.as_view(),
        name="location-maintenance",
    ),
    path("locations/<int:pk>/setup/", LocationSetupView.as_view(), name="location-setup"),
    path("login/", SignInView.as_view(), name="login"),
    path("logout/", SignOutView.as_view(), name="logout"),
    path("healthz", healthz, name="healthz"),
    # The exact device URL, with no trailing slash and no slash redirect: device clients do
    # not follow redirects, and v1's slash redirect echoed the key in its Location header.
    path("hb", HeartbeatView.as_view(), name="heartbeat"),
]
