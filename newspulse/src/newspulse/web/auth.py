"""Who may read the dashboard, and how they prove it.

Two mechanisms, and only ever one of them live at a time:

* **Sign in with Google** (``web.google_auth``) when an OAuth client and an
  allow-list are configured. This is the intended state.
* **HTTP Basic** otherwise, which is what the tool shipped with.

The order matters, and so does the fallback. Cutting basic auth out entirely
the moment the Google code landed would have bricked the deployment: the OAuth
client can only be created by a human in the Google Cloud console, and until
that exists there is no way to sign in at all. So Google *takes over* when it is
configured rather than being switched on by a flag somebody has to remember —
there is no state in which both are accepted, and no state in which neither is.

The dashboard holds a PR agency's client portfolio: who is monitored, what was
written about them, and the strategic advice drawn from it. Local-only, the
loopback bind was the whole security model. The moment it is reachable from
anywhere else, that model is gone and something has to replace it.

Basic auth's own posture is unchanged where it still applies: credentials come
from the environment, are never logged, and are compared in constant time so a
wrong password cannot be narrowed down by timing it. What matters most is that
authentication cannot be *forgotten* — see :func:`require_auth_for_public_bind`,
which now accepts either mechanism and refuses to boot on neither.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from .. import config
from . import google_auth

_log = logging.getLogger(__name__)

# Paths served before a credential is available. Static assets carry no client
# data, and excluding them keeps the browser from re-challenging on every file.
# The sign-in pages have to be reachable for the obvious reason: requiring a
# session to reach the page that creates one is a locked door with the key
# behind it.
_PUBLIC_PREFIXES = ("/static/", "/login", "/auth/google/", "/logout")

_REALM = 'Basic realm="NewsPulse", charset="UTF-8"'

# Loopback-only addresses. Binding to one of these means the tool is reachable
# from this machine alone, which is the setup auth exists to replace.
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def is_configured() -> bool:
    """True when basic auth has both halves of a credential."""
    return bool(config.AUTH_USER and config.AUTH_PASSWORD)


def any_auth_configured() -> bool:
    """True when *some* mechanism will challenge a request."""
    return google_auth.is_configured() or is_configured()


def is_loopback(host: str) -> bool:
    return (host or "").strip() in _LOOPBACK


def require_auth_for_public_bind(host: str) -> None:
    """Refuse to start a network-reachable server with no credentials set.

    This is the point of the module. Forgetting to configure auth is not an
    inconvenience here, it is publishing the client portfolio — so the failure
    is a refusal to boot rather than a warning nobody reads in a log. Loopback
    is unaffected: the local workflow that has always existed keeps working with
    no configuration at all.
    """
    if is_loopback(host) or any_auth_configured():
        return
    raise SystemExit(
        f"Refusing to bind {host}: configure sign-in before the dashboard is "
        "reachable off this machine. Either NEWSPULSE_GOOGLE_CLIENT_ID and "
        "NEWSPULSE_GOOGLE_CLIENT_SECRET (with NEWSPULSE_ALLOWED_EMAILS), or "
        "NEWSPULSE_AUTH_USER and NEWSPULSE_AUTH_PASSWORD. It serves client "
        "coverage and strategy notes with no other protection."
    )


def _credentials_match(header: str | None) -> bool:
    """Whether an Authorization header carries the configured credentials."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, _, password = decoded.partition(":")
    # compare_digest on both halves, and never short-circuit between them: an
    # early return on a wrong username would leak whether the name was right.
    user_ok = hmac.compare_digest(user.encode("utf-8"), config.AUTH_USER.encode("utf-8"))
    password_ok = hmac.compare_digest(
        password.encode("utf-8"), config.AUTH_PASSWORD.encode("utf-8")
    )
    return user_ok and password_ok


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Let a request through only if it is authenticated, by whichever mechanism
    is configured.

    Kept under its original name because it is wired into ``create_app`` and one
    rename buys nothing; what it guards is no longer only basic auth.

    A no-op when nothing is configured, so the local single-user setup is
    unchanged. That is safe only because :func:`require_auth_for_public_bind`
    refuses to start a public server in that state.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        # Google first and exclusively: once it is configured, the shared
        # password is no longer a way in. Leaving it accepted "just in case"
        # would mean the allow-list could be bypassed by whoever still has the
        # old password, which is most of the reason for moving off it.
        if google_auth.is_configured():
            email = google_auth.read_session(
                request.cookies.get(google_auth.SESSION_COOKIE)
            )
            if email:
                request.scope["user_email"] = email
                return await call_next(request)
            return _sign_in_required(request)

        if not is_configured():
            return await call_next(request)
        if _credentials_match(request.headers.get("Authorization")):
            return await call_next(request)
        return PlainTextResponse(
            "Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": _REALM},
        )


def _sign_in_required(request: Request) -> Response:
    """Send a browser to the sign-in page and anything else a bare 401.

    A redirect is right for a person and wrong for the HTMX poll in the header:
    swapping a login page into the run-status element would paint a sign-in form
    inside the dashboard chrome. Those get a status they can act on instead.
    """
    if request.headers.get("HX-Request"):
        return PlainTextResponse(
            "Sitzung abgelaufen.", status_code=401, headers={"HX-Redirect": "/login"}
        )
    if "text/html" not in (request.headers.get("Accept") or ""):
        return PlainTextResponse("Authentication required.", status_code=401)
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    # `next` so a bookmarked deep link survives the detour through Google.
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


__all__ = [
    "BasicAuthMiddleware",
    "any_auth_configured",
    "is_configured",
    "is_loopback",
    "require_auth_for_public_bind",
]
