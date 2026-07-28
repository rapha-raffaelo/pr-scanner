"""HTTP Basic authentication, and the guard that makes it non-optional.

The dashboard holds a PR agency's client portfolio: who is monitored, what was
written about them, and the strategic advice drawn from it. Local-only, the
loopback bind was the whole security model. The moment it is reachable from
anywhere else, that model is gone and something has to replace it.

Basic auth over HTTPS is deliberately the choice. It is a two-person internal
tool; a login form, sessions and a user table would be more code to get wrong for
no gain the terminating proxy does not already provide. What matters is that it
cannot be *forgotten* — see :func:`require_auth_for_public_bind`.

Credentials come from the environment and are never logged, the same posture as
the SMTP password (notify.py). The comparison is constant-time, so a wrong
password cannot be narrowed down by timing it.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .. import config

_log = logging.getLogger(__name__)

# Paths served before a credential is available. Static assets carry no client
# data, and excluding them keeps the browser from re-challenging on every file.
_PUBLIC_PREFIXES = ("/static/",)

_REALM = 'Basic realm="NewsPulse", charset="UTF-8"'

# Loopback-only addresses. Binding to one of these means the tool is reachable
# from this machine alone, which is the setup auth exists to replace.
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def is_configured() -> bool:
    """True when both a username and a password are set."""
    return bool(config.AUTH_USER and config.AUTH_PASSWORD)


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
    if is_loopback(host) or is_configured():
        return
    raise SystemExit(
        f"Refusing to bind {host}: NEWSPULSE_AUTH_USER and NEWSPULSE_AUTH_PASSWORD "
        "must be set before the dashboard is reachable off this machine. "
        "It serves client coverage and strategy notes with no other protection."
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
    user_ok = hmac.compare_digest(user, config.AUTH_USER)
    password_ok = hmac.compare_digest(password, config.AUTH_PASSWORD)
    return user_ok and password_ok


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Challenge every request when credentials are configured.

    A no-op when they are not, so the local single-user setup is unchanged. That
    is safe only because :func:`require_auth_for_public_bind` refuses to start a
    public server in that state.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not is_configured():
            return await call_next(request)
        if request.url.path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)
        if _credentials_match(request.headers.get("Authorization")):
            return await call_next(request)
        return PlainTextResponse(
            "Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": _REALM},
        )


__all__ = [
    "BasicAuthMiddleware",
    "is_configured",
    "is_loopback",
    "require_auth_for_public_bind",
]
