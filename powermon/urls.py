"""URL routes."""

from django.urls import path

from powermon.web import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
]
