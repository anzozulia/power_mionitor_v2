"""The confirmation fragments of the modal (UI-07, D6-05, R7; 06-UI-SPEC Confirmations).

The four destructive actions keep a server-side confirmation step (R7): S7 delete a
location, S9 regenerate its device key, S10 remove an outage and S11 reset its history are
each a GET confirmation page, then a POST with CSRF. With JavaScript, admin.js's
confirmDialog loads that same GET into a native dialog. It sends the request header
``X-PM-Fragment: 1`` and shows the answer only when it is a 200 that carries the header
back and holds exactly one ``[data-testid="confirm"]`` root; on any other outcome it loads
the page itself. So the page stays the no-JS and deep-link fallback, and both show one
shared partial: the same text, the same form, the same S9 marker.

- This module is the only place that reads the header, and only the four confirmation GET
  handlers call it. No middleware, base template or context processor does, so the header
  cannot change any other response (no cache poisoning, no unexpected bodies), and the POST
  handlers never read it, so it never changes a POST.
- Only the exact value "1" asks for the fragment; "0", "true", an empty value or no header
  get the full page.
- The header only picks the template, after every check of the view has run: anonymous
  requests, unknown or deleted locations and invalid instants answer the same 302 or 404 in
  both variants, and both get the same context (the S9 marker is an HMAC, never key
  characters, R4).
- Every response here, page, fragment or refusal redirect, is ``never_cache`` and varies on
  the header, so no cache can serve one variant for the other (TEST-STRATEGY §1 rule 1
  allows this strengthening for S7, S10 and S11).
- A refusal is the same 302 in both variants, but only the full-page request queues its
  flash. The dialog then loads the page itself, whose GET queues the flash once, so the
  admin sees exactly one toast (UI-09). A fragment never shows the toasts, so a pending
  flash survives a fragment load.
"""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.cache import add_never_cache_headers, patch_vary_headers

FRAGMENT_HEADER = "X-PM-Fragment"
# The one value that asks for the fragment, and the value the fragment response carries.
FRAGMENT_VALUE = "1"


def wants_fragment(request: HttpRequest) -> bool:
    """The request asks for the confirmation partial alone: ``X-PM-Fragment`` is exactly "1"."""
    return request.headers.get(FRAGMENT_HEADER) == FRAGMENT_VALUE


def _confirmation_headers(response: HttpResponse) -> HttpResponse:
    """``Vary: X-PM-Fragment`` and the ``never_cache`` headers, on every variant."""
    patch_vary_headers(response, (FRAGMENT_HEADER,))
    add_never_cache_headers(response)
    return response


def confirm_response(
    request: HttpRequest, page_template: str, partial_template: str, context: dict[str, Any]
) -> HttpResponse:
    """The confirmation: the partial alone for a fragment request, else the full page.

    The fragment is the partial rendered with ``fragment`` set (its modal sizes) and answers
    with the response header ``X-PM-Fragment: 1``, which the dialog requires before it shows
    anything. The page extends the app layout and includes the same partial.
    """
    if wants_fragment(request):
        response = render(request, partial_template, context | {"fragment": True})
        response[FRAGMENT_HEADER] = FRAGMENT_VALUE
    else:
        response = render(request, page_template, context)
    return _confirmation_headers(response)


def refusal_redirect(
    request: HttpRequest, level: int, message: str, to: str, **kwargs: Any
) -> HttpResponse:
    """A refused confirmation: the same redirect in both variants, the flash only for the page.

    ``to`` and ``kwargs`` name a fixed route (``redirect``), never a value from the request.
    The fragment request queues nothing; the dialog then loads the page, whose refusal
    queues the flash exactly once.
    """
    if not wants_fragment(request):
        messages.add_message(request, level, message)
    return _confirmation_headers(redirect(to, **kwargs))
