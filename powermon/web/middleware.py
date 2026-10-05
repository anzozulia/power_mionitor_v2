"""Content-Security-Policy on every response (06-UI-SPEC security-bound rule R5, brief §8).

The Phase 6 admin loads only same-origin assets: its own scripts (admin.js and the vendored
CSP build of Alpine), the one built stylesheet, the self-hosted fonts and same-origin images
(plus data: images, which the forms plugin's select chevron uses). Live updates fetch only
same-origin URLs. No inline script or style, no eval, no third-party host: the policy turns
"no third-party runtime assets on pages that show secrets" (v1-lessons section 4) into a
checked guarantee. Only the exact heartbeat path is exempt: it answers devices, not browsers.
"""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponseBase

CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"  # noqa: E501
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
