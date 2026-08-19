"""The sign-in flow: a page, a trip to Google, and the cookie that comes back.

Deliberately four small routes and no session store. The only thing worth
remembering between requests is "Google says this is lucas.neurauter@gmail.com",
which fits in a signed cookie — and a cookie that carries its own signature
cannot be forged, cannot be enumerated, and survives a redeploy without a table.

Every route here is reachable unauthenticated (``auth._PUBLIC_PREFIXES``), which
is the point: requiring a session to reach the page that creates one is a locked
door with the key behind it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ... import config
from .. import google_auth
from ..app import templates

_log = logging.getLogger(__name__)

router = APIRouter()

#: Where a signed-in person lands when nothing better is known.
_HOME = "/"


def _safe_next(raw: str | None) -> str:
    """A path from the query string, or the dashboard.

    Only a same-site *path* is ever honoured. Reflecting an absolute URL here
    would turn the sign-in page into an open redirect: a link to our own domain
    that quietly lands somewhere else, which is worth a lot to whoever is
    phishing the two people who have access. "//host" is rejected for the same
    reason — the browser reads it as a scheme-relative absolute URL.
    """
    candidate = (raw or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return _HOME
    return candidate


def _is_secure(request: Request) -> bool:
    """Whether to mark cookies Secure.

    Behind Railway's proxy the app speaks plain HTTP, so ``request.url.scheme``
    says "http" while the browser is on HTTPS. The forwarded header is what the
    browser actually used, and marking the cookie Secure on a real HTTPS site is
    what keeps it off a plaintext connection.
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return (forwarded or request.url.scheme) == "https"


def _set_cookie(response: Response, request: Request, name: str, value: str,
                *, seconds: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        httponly=True,          # never readable from JavaScript
        secure=_is_secure(request),
        # Lax, not Strict: the browser arrives back from accounts.google.com on
        # a cross-site redirect, and Strict would withhold the state cookie on
        # exactly that request, failing every sign-in.
        samesite="lax",
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, error: str | None = None):
    """The sign-in page. Already signed in? Go straight through."""
    if google_auth.is_configured() and google_auth.read_session(
        request.cookies.get(google_auth.SESSION_COOKIE)
    ):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": _safe_next(next),
            "error": error,
            "configured": google_auth.is_configured(),
            "allowed": sorted(google_auth.allowed_emails()),
        },
    )


@router.get("/auth/google/start")
def google_start(request: Request, next: str | None = None):
    """Mint a state nonce and hand the browser to Google."""
    if not google_auth.is_configured():
        return RedirectResponse("/login?error=nicht_eingerichtet", status_code=303)
    state = google_auth.issue_state()
    target = google_auth.authorization_url(state)
    response = RedirectResponse(target, status_code=303)
    _set_cookie(response, request, google_auth.STATE_COOKIE, state,
                seconds=google_auth.STATE_SECONDS)
    # Carried in a cookie rather than through Google: `state` is the only field
    # that comes back, and stuffing a destination into it would mean trusting a
    # value that made a round trip through the address bar.
    _set_cookie(response, request, "rauteos_next", _safe_next(next),
                seconds=google_auth.STATE_SECONDS)
    return response


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None,
                    state: str | None = None, error: str | None = None):
    """Google's answer. Verify it, then either let the person in or say why not."""
    if error:
        # The person declined the consent screen, or Google refused. Neither is
        # an application fault and neither deserves a stack trace.
        _log.info("Google sign-in declined: %s", error)
        return RedirectResponse("/login?error=abgebrochen", status_code=303)

    if not google_auth.state_is_valid(request.cookies.get(google_auth.STATE_COOKIE), state):
        return RedirectResponse("/login?error=abgelaufen", status_code=303)
    if not code:
        return RedirectResponse("/login?error=abgebrochen", status_code=303)

    try:
        identity = google_auth.exchange_code(code)
    except google_auth.SignInError as exc:
        _log.warning("Google sign-in failed: %s", exc)
        return RedirectResponse("/login?error=fehlgeschlagen", status_code=303)

    if not google_auth.is_allowed(identity.email):
        # Deliberately its own message. "Wrong password" for an account that is
        # simply not on the list sends the person to reset a credential that was
        # never the problem.
        _log.warning("Rejected sign-in for %s: not on the allow-list", identity.email)
        return RedirectResponse("/login?error=nicht_freigegeben", status_code=303)

    destination = _safe_next(request.cookies.get("rauteos_next"))
    response = RedirectResponse(destination, status_code=303)
    _set_cookie(response, request, google_auth.SESSION_COOKIE,
                google_auth.issue_session(identity),
                seconds=google_auth.SESSION_DAYS * 86400)
    response.delete_cookie(google_auth.STATE_COOKIE, path="/")
    response.delete_cookie("rauteos_next", path="/")
    _log.info("Signed in: %s", identity.email)
    return response


@router.post("/logout")
@router.get("/logout")
def logout(request: Request):
    """Drop the session cookie and go back to the sign-in page."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(google_auth.SESSION_COOKIE, path="/")
    return response
