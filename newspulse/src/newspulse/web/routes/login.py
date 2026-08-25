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
from .. import google_auth, redirects
from ..app import templates

_log = logging.getLogger(__name__)

router = APIRouter()

#: Where a signed-in person lands when nothing better is known.
_HOME = "/"


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
        return RedirectResponse(redirects.local_target(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": redirects.local_target(next),
            "error": error,
            "configured": google_auth.is_configured(),
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
    _set_cookie(response, request, "rauteos_next", redirects.local_target(next),
                seconds=google_auth.STATE_SECONDS)
    return response


def _back_to_login(error: str, request: Request) -> Response:
    """To the sign-in page, and the one-time state goes with us.

    Only the success path used to clear it, so a state stayed replayable for the
    rest of its ten-minute window after every refusal — including the refusals
    that mean somebody is trying something.
    """
    response = RedirectResponse(f"/login?error={error}", status_code=303)
    response.delete_cookie(google_auth.STATE_COOKIE, path="/")
    response.delete_cookie("rauteos_next", path="/")
    return response


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None,
                    state: str | None = None, error: str | None = None):
    """Google's answer. Verify it, then either let the person in or say why not."""
    if error:
        # The person declined the consent screen, or Google refused. Neither is
        # an application fault and neither deserves a stack trace.
        _log.info("Google sign-in declined: %s", error)
        return _back_to_login("abgebrochen", request)

    if not google_auth.state_is_valid(request.cookies.get(google_auth.STATE_COOKIE), state):
        return _back_to_login("abgelaufen", request)
    if not code:
        return _back_to_login("abgebrochen", request)

    try:
        identity = google_auth.exchange_code(code)
    except google_auth.SignInError as exc:
        _log.warning("Google sign-in failed: %s", exc)
        return _back_to_login("fehlgeschlagen", request)

    if not google_auth.is_allowed(identity.email):
        # Deliberately its own message. "Wrong password" for an account that is
        # simply not on the list sends the person to reset a credential that was
        # never the problem.
        _log.warning("Rejected sign-in for %s: not on the allow-list", identity.email)
        return _back_to_login("nicht_freigegeben", request)

    destination = redirects.local_target(request.cookies.get("rauteos_next"))
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
