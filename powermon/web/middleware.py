"""Content-Security-Policy on every response (UI-SPEC, Security-Bound UI Rules, rule 5).

Admin pages load one same-origin stylesheet and no scripts, so the policy turns
"no third-party runtime assets on pages that show secrets" (v1-lessons section 4) into
a checked guarantee. Only the exact heartbeat path is exempt: it answers devices, not
browsers.
"""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponseBase

CSP = "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"  # noqa: E501
HEARTBEAT_PATH = "/hb"


class ContentSecurityPolicyMiddleware:
    """Sets the CSP header on every response whose request path is not exactly /hb."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if request.path != HEARTBEAT_PATH:
            response["Content-Security-Policy"] = CSP
        return response
