"""Switching the interface language.

A cookie rather than a URL prefix or a settings row: it is a per-reader
preference, it must survive a restart, and it must not turn every internal link
into a language-carrying URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from ... import i18n

router = APIRouter()

_SEE_OTHER = 303


@router.post("/language/{code}")
def set_language(code: str, request: Request) -> Response:
    """Set the interface language and return to the page it was set from."""
    redirect_to = request.query_params.get("next", "/")
    # Same-app paths only: the target comes from the query string, so an
    # absolute URL here would make this an open redirect.
    target = redirect_to if redirect_to.startswith("/") else "/"

    response = RedirectResponse(target, status_code=_SEE_OTHER)
    response.set_cookie(
        i18n.COOKIE_NAME,
        i18n.normalize(code),
        max_age=i18n.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response
