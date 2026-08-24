"""Refuse a state-changing request that arrived from another site's page.

Two routes carried this check and about fifty did not. The two were chosen well
— they mint the audit record the product's L5 claim rests on — but the reasoning
in their docstring applies to every write in the app: deactivate a mandate,
commit an import, delete a contact, overwrite a communications guide. Any open
web page could auto-submit a form at this app and the browser would attach
whatever it holds.

Under Google sign-in the session cookie is ``SameSite=Lax``, which already
refuses to travel with a cross-site POST. That is a real defence and it is a
single one: it is one cookie attribute away from being gone, it does nothing for
the basic-auth mode this app still supports and still ships with, and it is not
visible in any code a reader of these routes would think to check.

A request that names no origin passes. That is not a browser — curl, a test
client, the scheduler — and a non-browser carries no ambient credentials for a
foreign page to ride on. This is a floor, not a token: it stops the drive-by,
and it does not stop an attacker who can execute script on this origin. Nothing
here should be read as making that second case safe.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

_log = logging.getLogger(__name__)

#: Methods that change something. GET and HEAD are excluded because a browser
#: issues them for any link, and a same-origin rule on them would break every
#: bookmark and every link somebody pastes into a chat.
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

REFUSAL = "Anfrage von einer fremden Seite."


def is_foreign(request: Request) -> bool:
    """Whether a browser says this request was submitted from somewhere else."""
    named = request.headers.get("origin") or request.headers.get("referer") or ""
    if not named:
        return False
    # ``Origin: null`` — a sandboxed frame, a data: page — has an empty netloc
    # and fails the comparison, which is the right answer for it.
    return urlsplit(named).netloc != request.url.netloc


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Every write, not the four somebody remembered."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in UNSAFE and is_foreign(request):
            _log.warning(
                "refused a %s to %s from %r",
                request.method, request.url.path,
                request.headers.get("origin") or request.headers.get("referer"),
            )
            return PlainTextResponse(REFUSAL, status_code=403)
        return await call_next(request)
