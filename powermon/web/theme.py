"""``POST /theme/``: the no-JS theme switch (UI-02, D6-03, R8).

With JavaScript the theme control changes the theme in place and writes the cookie
itself; this form POST is the fallback. It sets the ``theme`` cookie (``light``, ``dark``
or ``system``) with the attributes the JS writes too, and always redirects to the fixed
``/``: it reads neither a ``next`` value nor the Referer, so it can never redirect off the
site (R8). The allowlist is the context processor's (``context_processors.THEMES``).
A GET sets no cookie and redirects to ``/`` (F-24: the sign-in redirect after an ended
session).
"""

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from powermon.web.context_processors import THEME_COOKIE, THEMES

# About one year, in seconds.
THEME_MAX_AGE = 31_536_000
# The fixed target of every successful POST (R8).
THEME_REDIRECT = "/"


@method_decorator(never_cache, name="dispatch")
class ThemeView(View):
    """POST ``theme=light|dark|system``: set the theme cookie, then 302 to ``/``.

    A missing or unknown value answers 400 with an empty body and sets no cookie (only a
    hand-made request can send one). A GET sets no cookie and redirects to ``/`` (F-24: the
    sign-in redirect after an ended session). Every other method answers 405. CSRF-protected
    and login-required like every admin POST. Writes nothing to the database and adds no
    flash. Every response is never cached.
    """

    http_method_names = ["get", "post"]

    def get(self, request: HttpRequest) -> HttpResponse:
        # The sign-in redirect after an ended session GETs /theme/ (F-24): no cookie, no
        # flash, only the fixed target.
        return HttpResponseRedirect(THEME_REDIRECT)

    def post(self, request: HttpRequest) -> HttpResponse:
        value = request.POST.get("theme")
        if value is None or value not in THEMES:
            return HttpResponseBadRequest()
        response = HttpResponseRedirect(THEME_REDIRECT)
        response.set_cookie(
            THEME_COOKIE,
            value,
            max_age=THEME_MAX_AGE,
            path="/",
            samesite="Lax",
            secure=settings.SESSION_COOKIE_SECURE,
            # The JS theme control updates the same cookie.
            httponly=False,
        )
        return response
