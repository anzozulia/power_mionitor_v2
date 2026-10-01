"""Django settings, built from the fail-closed config (SEC-01, SEC-02).

Every value that depends on the environment comes from ``config.load(os.environ)``.
A bad environment stops every entrypoint with ImproperlyConfigured, and the
message starts with the name of the offending variable.
"""

import os
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from powermon import config

try:
    CFG = config.load(os.environ)
except config.ConfigError as exc:
    raise ImproperlyConfigured(str(exc)) from None

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = CFG.secret_key
# False in production, always (config refuses production with a truthy DEBUG).
DEBUG = CFG.debug
# Production: DOMAIN and 127.0.0.1 (the container health check); local: localhost, 127.0.0.1.
ALLOWED_HOSTS = list(CFG.allowed_hosts)
# Production: https://DOMAIN. Never derived from the request Host header.
PUBLIC_BASE_URL = CFG.public_base_url
CSRF_TRUSTED_ORIGINS = [PUBLIC_BASE_URL] if PUBLIC_BASE_URL else []

TIME_ZONE = CFG.display_tz
USE_TZ = True
# The admin UI is English only.
USE_I18N = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "powermon",
    "powermon.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Default-deny: every view needs a login unless it is marked login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "powermon.urls"
WSGI_APPLICATION = "powermon.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Build mode (collectstatic in the Dockerfile) has no database at all.
DATABASES: dict[str, dict[str, Any]] = (
    {}
    if CFG.build
    else {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": CFG.db_name,
            "USER": CFG.db_user,
            "PASSWORD": CFG.db_password,
            "HOST": CFG.db_host,
            "PORT": CFG.db_port,
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "connect_timeout": 5,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "options": "-c idle_in_transaction_session_timeout=60000",
            },
        }
    }
)

# Security. Caddy terminates TLS and overwrites X-Forwarded-* from clients; the web
# container is reachable only through Caddy (production) or 127.0.0.1 (local).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Caddy redirects to HTTPS; the app never does, so /hb never answers with a redirect.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = CFG.production
CSRF_COOKIE_SECURE = CFG.production
# D-17: one year, no includeSubDomains, no preload.
SECURE_HSTS_SECONDS = 31_536_000 if CFG.production else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
X_FRAME_OPTIONS = "DENY"
SILENCED_SYSTEM_CHECKS = ["security.W008", "security.W005", "security.W021"]

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

LOGIN_URL = "login"
# A path, not a route name: the list route lands after the login route.
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
