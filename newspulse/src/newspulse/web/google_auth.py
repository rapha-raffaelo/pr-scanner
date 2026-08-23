"""Sign in with Google, for the two people who are allowed in.

Basic auth was the right call while the answer to "who may read this" was one
shared password in an env var. It stops being right the moment the answer is a
list of people: a shared secret cannot be revoked for one of them, it travels in
every request, and it says nothing about who was actually looking.

So the password is replaced by Google's own answer to the question. RauteOS
never sees a credential — Google authenticates the person and hands back a
signed assertion of their email address, and the only thing this module decides
is whether that address is on the list.

Three deliberate choices:

* **The allow-list is configuration, not a table.** Two addresses do not need a
  user model, an invitation flow and a role column. When the third person
  arrives, an env var changes and nothing else does.
* **The ID token is trusted because of where it came from, not because we
  re-derived Google's signature.** It arrives in the body of a direct HTTPS POST
  to Google's token endpoint, authenticated with the client secret. That is the
  one case Google's own documentation says needs no local signature check. The
  claims that describe *who it was issued for* are still verified here, because
  transport says nothing about those.
* **No new dependency.** ``urllib`` reaches Google the same way ``gemini`` and
  ``ingest`` already reach their APIs, and the signature comes from ``hmac``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# Google mints tokens with either issuer; both are legitimate and a check that
# accepts only one rejects real sign-ins at random.
_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

# Identity only. No mailbox, no Drive, no contacts: the consent screen should be
# able to say "sieht Ihre E-Mail-Adresse" and nothing more, because that is all
# this uses. Sending mail (OUT-04) asks for its own scopes separately.
SCOPES = ("openid", "email")

_SECRET_FILENAME = ".session-secret"
_SECRET_MODE = 0o600

#: How long a sign-in lasts before Google is asked again.
SESSION_DAYS = 30
#: How long the browser has to come back from Google with a code.
STATE_SECONDS = 600

SESSION_COOKIE = "rauteos_session"
STATE_COOKIE = "rauteos_oauth_state"


class SignInError(Exception):
    """Sign-in failed for a reason worth showing the person, in their language."""


@dataclass(frozen=True)
class Identity:
    """Who Google says this is."""

    email: str
    subject: str


# --- Configuration ------------------------------------------------------------


def client_id() -> str:
    """The sign-in OAuth client. Its own variable, deliberately.

    An earlier version read the mailbox credential as a fallback, on the theory
    that one Google client can serve both and configuring it twice is silly.
    It can, and it is — but the coupling is wrong in the direction that matters:
    connecting a mailbox for *sending* would have silently changed how everyone
    *signs in*. Two unrelated decisions, one switch, and the surprise lands on
    whoever is locked out.

    So sign-in is on only when it has been asked for by name. Sharing the
    credential is still fine and costs one variable reference in the deployment.
    """
    return config.google_client_id()


def client_secret() -> str:
    return config.google_client_secret()


def is_configured() -> bool:
    """True when Google sign-in can actually run.

    Both halves of the OAuth client *and* at least one allowed address: a
    configured client with an empty allow-list would authenticate people
    perfectly and then refuse every one of them, which reads as a broken login
    rather than as a missing setting."""
    return bool(client_id() and client_secret() and allowed_emails())


# Gmail ignores dots in the local part and everything after a plus, so
# lucas.neurauter@, lucasneurauter@ and lucas+rauteos@ are one mailbox. Google
# returns whichever spelling the account was registered with, which is not
# necessarily the one somebody typed into the allow-list — and an exact string
# comparison then locks out a person whose address is, to Google, identical.
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def canonical(email: str) -> str:
    """One spelling per mailbox, for comparison only.

    Never stored and never displayed: the address a person sees is the one
    Google returned. This exists so that two spellings of the same Gmail
    account do not read as two different people.

    Left alone outside Gmail, where dots and plus signs may genuinely separate
    mailboxes and folding them would let one address stand in for another.
    """
    local, _, domain = (email or "").strip().casefold().partition("@")
    if domain not in _GMAIL_DOMAINS:
        return f"{local}@{domain}" if domain else local
    local = local.partition("+")[0].replace(".", "")
    # googlemail.com is the same inbox as gmail.com — the domain Google issued
    # in Germany and the UK for years, so it is the likelier spelling here, not
    # an edge case.
    return f"{local}@gmail.com"


def allowed_emails() -> frozenset[str]:
    """The addresses that may sign in, as written, lowercased."""
    return frozenset(
        part.strip().casefold()
        for part in config.allowed_emails().split(",")
        if part.strip()
    )


def is_allowed(email: str) -> bool:
    """Whether Google's answer names somebody on the list.

    Compared on the canonical form, so the list may carry any spelling of a
    Gmail address and still recognise the person Google authenticated.
    """
    if not (email or "").strip():
        return False
    return canonical(email) in {canonical(a) for a in allowed_emails()}


def redirect_uri(base: str | None = None) -> str:
    """Where Google sends the browser back to.

    Must match a Redirect URI registered on the OAuth client exactly, down to
    the scheme and the trailing path — Google rejects the request outright
    otherwise, before the person ever sees a consent screen.
    """
    return f"{(base or config.base_url()).rstrip('/')}/auth/google/callback"


# --- The signing secret -------------------------------------------------------


def secret_path() -> Path:
    """Beside the database, which on Railway is the mounted volume.

    Next to the data rather than in it: a database copy pulled down for local
    work then cannot mint session cookies for production.
    """
    return config.DATABASE_PATH.parent / _SECRET_FILENAME


def session_secret() -> bytes:
    """The HMAC key for session cookies, generated once and then reused.

    Generated rather than configured, because a secret nobody has to invent is a
    secret nobody sets to "changeme". It has to survive a restart, though: a
    fresh key on every boot would sign everyone out on each deploy, which looks
    exactly like a broken login.

    An explicit ``NEWSPULSE_SESSION_SECRET`` wins when set, for a deployment
    that would rather keep it in a secret manager than on a disk.
    """
    configured = config.session_secret()
    if configured:
        return configured.encode("utf-8")

    path = secret_path()
    try:
        existing = path.read_bytes().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as exc:  # unreadable is not the same as absent
        raise SignInError(f"Session-Schlüssel nicht lesbar: {exc}") from exc

    generated = secrets.token_urlsafe(48).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened with the mode rather than written and then chmod-ed: the second
    # shape leaves the key world-readable for the moment that matters.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _SECRET_MODE)
    except FileExistsError:  # a parallel worker won the race; theirs is fine
        existing = path.read_bytes().strip()
        if existing:
            return existing
        # Present and empty: nothing to adopt and nothing that will ever fill
        # it, so this worker takes the file over rather than looping.
        path.write_bytes(generated)
        path.chmod(_SECRET_MODE)
        return generated
    with os.fdopen(fd, "wb") as handle:
        handle.write(generated)
    return generated


# --- Signed values ------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: bytes) -> str:
    digest = hmac.new(session_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(digest)}"


def _unsign(token: str) -> bytes | None:
    """The payload if the signature holds and has not expired, else None."""
    body, _, signature = (token or "").partition(".")
    if not body or not signature:
        return None
    try:
        payload, given = _unb64(body), _unb64(signature)
    except (ValueError, base64.binascii.Error):
        return None
    expected = hmac.new(session_secret(), payload, hashlib.sha256).digest()
    # compare_digest: a byte-by-byte comparison leaks how much of a forged
    # signature was right, one request at a time.
    if not hmac.compare_digest(expected, given):
        return None
    return payload


def issue_session(identity: Identity, *, now: float | None = None) -> str:
    """A signed cookie value naming who signed in and when it lapses."""
    expires = (now if now is not None else time.time()) + SESSION_DAYS * 86400
    body = json.dumps(
        {"email": identity.email, "sub": identity.subject, "exp": int(expires)},
        separators=(",", ":"),
    ).encode("utf-8")
    return _sign(body)


def read_session(token: str | None, *, now: float | None = None) -> str | None:
    """The signed-in address, or None if the cookie is absent, forged, expired,
    or names somebody who is no longer allowed in.

    The last of those is the point of re-checking rather than trusting the
    cookie: taking an address off the list has to end that person's access at
    their next request, not in thirty days.
    """
    if not token:
        return None
    payload = _unsign(token)
    if payload is None:
        return None
    try:
        claims = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if float(claims.get("exp", 0)) < (now if now is not None else time.time()):
        return None
    email = str(claims.get("email", ""))
    return email if is_allowed(email) else None


def issue_state(*, now: float | None = None) -> str:
    """A short-lived signed nonce, so the callback can prove it answers a sign-in
    this browser actually started. Without it, anyone able to make the browser
    fetch a URL can complete a sign-in with a code of their choosing."""
    stamp = int(now if now is not None else time.time())
    body = json.dumps(
        {"n": secrets.token_urlsafe(16), "exp": stamp + STATE_SECONDS},
        separators=(",", ":"),
    ).encode("utf-8")
    return _sign(body)


def state_is_valid(cookie: str | None, returned: str | None, *, now: float | None = None) -> bool:
    if not cookie or not returned:
        return False
    if not hmac.compare_digest(cookie, returned):
        return False
    payload = _unsign(cookie)
    if payload is None:
        return False
    try:
        claims = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return float(claims.get("exp", 0)) >= (now if now is not None else time.time())


# --- The flow -----------------------------------------------------------------


def authorization_url(state: str, *, base: str | None = None) -> str:
    """Where to send the browser to sign in."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri(base),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            # The account chooser every time. This is a shared machine in an
            # agency; silently resuming whichever Google account the browser
            # last used is how the wrong person's session gets adopted.
            "prompt": "select_account",
        }
    )
    return f"{_AUTH_ENDPOINT}?{query}"


def _post_form(url: str, fields: dict[str, str], *, timeout: float) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _claims_from_id_token(id_token: str) -> dict:
    """The payload of a JWT, without verifying its signature.

    Safe *only* because of how this token was obtained: the body of a direct
    HTTPS POST to Google's token endpoint, authenticated with the client secret.
    TLS establishes that Google sent it. A token that arrived any other way —
    from the browser, from a query string — would need its signature checked
    against Google's keys, and this function must not be used on one.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise SignInError("Google hat kein lesbares ID-Token geschickt.")
    try:
        return json.loads(_unb64(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise SignInError("Google hat kein lesbares ID-Token geschickt.") from exc


def _verify_claims(claims: dict, *, now: float | None = None) -> Identity:
    """Check the claims transport cannot vouch for, then read the address.

    ``aud`` is the one that matters: without it, an ID token Google issued for
    some *other* application would be accepted here, and any developer with a
    Google client could mint one.
    """
    if claims.get("iss") not in _ISSUERS:
        raise SignInError("Das Token stammt nicht von Google.")
    if claims.get("aud") != client_id():
        raise SignInError("Das Token wurde für eine andere Anwendung ausgestellt.")
    if float(claims.get("exp", 0)) < (now if now is not None else time.time()):
        raise SignInError("Das Token von Google ist bereits abgelaufen.")
    email = str(claims.get("email", "")).strip()
    if not email:
        raise SignInError("Google hat keine E-Mail-Adresse mitgeschickt.")
    # An unverified address on a Google account is one the person typed, not one
    # they proved they own — accepting it would make the allow-list meaningless.
    if claims.get("email_verified") not in (True, "true"):
        raise SignInError("Diese Google-Adresse ist nicht bestätigt.")
    return Identity(email=email, subject=str(claims.get("sub", "")))


def exchange_code(code: str, *, base: str | None = None, now: float | None = None,
                  timeout: float = 15.0) -> Identity:
    """Turn the callback's one-time code into a verified identity."""
    try:
        payload = _post_form(
            _TOKEN_ENDPOINT,
            {
                "code": code,
                "client_id": client_id(),
                "client_secret": client_secret(),
                "redirect_uri": redirect_uri(base),
                "grant_type": "authorization_code",
            },
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        # The response body names the actual fault (redirect_uri_mismatch,
        # invalid_client) and the status alone does not, so it is logged. It is
        # not shown: it is Google's English, aimed at whoever configured the
        # client, and it can carry the client id.
        _log.warning("Google token exchange failed: %s %s", exc.code,
                     exc.read()[:400].decode("utf-8", "replace"))
        raise SignInError("Google hat die Anmeldung abgelehnt.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        _log.warning("Google token exchange unreachable: %s", exc)
        raise SignInError("Google war nicht erreichbar.") from exc

    id_token = payload.get("id_token")
    if not id_token:
        raise SignInError("Google hat keine Identität zurückgegeben.")
    return _verify_claims(_claims_from_id_token(id_token), now=now)
