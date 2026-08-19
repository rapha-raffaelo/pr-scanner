"""The connection to one Gmail account: consent, token, profile, disconnect.

Nothing here reads mail. This module owns the OAuth round trip (DEC-5: the Gmail
API through an *Internal* app in RAUTE's own Google Workspace), the refresh token
that has to survive a restart, and the honest answer to "is a mailbox connected,
and as whom".

Three things are deliberate and load-bearing:

* **The scopes are exactly what DEC-4 licenses and no more.** They are a tuple
  here, spelled once, so the consent screen Google shows a person is the same
  sentence this codebase can act on. Adding a scope is a decision, not an import.
* **The refresh token never touches the database.** ``config.py`` sources every
  secret from ``NEWSPULSE_*``; a refresh token cannot follow that rule because it
  is obtained at runtime. So it goes to a file beside the SQLite database on the
  volume, mode 0600, and neither a database copy nor the Excel export can carry
  it out of the machine. No token value is ever logged either — the failure
  messages built here quote Google's ``error`` field and nothing else.
* **Every network call goes through an injected ``fetch``.** The default reaches
  Google; a test passes its own and the suite never leaves the machine.

The word "connected" is used narrowly: it means a refresh token is on disk *and*
Google answered a profile request with an address. The address shown in Settings
is that answer, never what somebody typed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

_log = logging.getLogger(__name__)

# --- Google's endpoints --------------------------------------------------------

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
_PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# --- What the connection is allowed to do (DEC-4, option C: lesen und senden) ---
#
# Two scopes, and the narrowest pair that covers it. Not `gmail.modify`, which
# would also let the tool delete and relabel; not `mail.google.com`, which is
# everything. What is asked for here is what the person sees on Google's own
# consent screen, so this tuple is the promise made to them.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)

# The same permissions in words, because "gmail.readonly" is not a sentence a
# person can consent to twice. Shown in Settings beside the connected address.
_SCOPE_WORDS: dict[str, str] = {
    "https://www.googleapis.com/auth/gmail.readonly": "Nachrichten lesen",
    "https://www.googleapis.com/auth/gmail.send": "Nachrichten senden",
}

# --- The token file ------------------------------------------------------------

# Beside the database, not in it. Named for what it holds so an operator looking
# at the volume knows immediately that this file is a credential.
_TOKEN_FILENAME = "gmail_token.json"

# Owner read/write only. The whole point of keeping the token out of the database
# is that it stays on the machine it was granted to; a world-readable file would
# give that back on a shared host.
_TOKEN_MODE = 0o600

# Refresh this many seconds before Google's stated expiry. A token that expires
# mid-request is indistinguishable from a revoked one at the call site, and the
# clocks involved are not the same clock.
_EXPIRY_SKEW_SECONDS = 60

# Google states the lifetime in seconds; this is only the floor used when a token
# response omits `expires_in` entirely, so a missing field costs one extra
# refresh rather than an access token that is treated as eternal.
_DEFAULT_EXPIRES_IN = 0

# Request budget. These are small JSON calls to Google; a mailbox that does not
# answer inside this window is a failure to report, not something to wait out.
_TIMEOUT = 20.0

# Google's word for "this refresh token no longer works" — revoked in the
# account, consent withdrawn, or the app removed. It is the one token error that
# means the connection is over rather than that Google had a bad minute.
_REVOKED_ERROR = "invalid_grant"

# Bytes read from an error body before giving up on parsing it. Google's OAuth
# errors are a few hundred bytes of JSON; this bounds a proxy's HTML error page.
_MAX_ERROR_BYTES = 4096


class GmailError(RuntimeError):
    """A Gmail call failed in a way the caller has to hear about.

    Deliberately *not* raised when Google says the access was revoked: that is a
    fact about the connection, so it disconnects and reports rather than raising
    into a page render (see :func:`token`).
    """


#: What one network call looks like from in here: a URL, an optional form body
#: (its presence makes it a POST), an optional bearer token. Returns Google's
#: parsed JSON — including an error payload, which callers read rather than
#: catch, so a stub in a test is a dict in and a dict out.
Fetch = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Link:
    """What Settings needs to know about the mailbox connection.

    ``email`` is empty exactly when nothing is connected. ``lost`` carries the
    reason a connection ended at Google's end, so the panel can say *why* it is
    asking for consent again instead of silently reverting to an empty state.
    """

    email: str = ""
    scopes: tuple[str, ...] = ()
    connected_at: dt.datetime | None = None
    lost: str = ""

    @property
    def is_connected(self) -> bool:
        return bool(self.email)

    @property
    def scope_words(self) -> list[str]:
        """The granted permissions in German, in the order Google granted them."""
        return [_SCOPE_WORDS.get(scope, scope) for scope in self.scopes]


@dataclass(frozen=True, slots=True)
class Profile:
    """The Gmail profile of the connected account, as Google reports it."""

    email: str
    messages_total: int = 0
    history_id: str = ""


# --- The 0600 token file -------------------------------------------------------


def token_path() -> Path:
    """Where the refresh token lives: beside the SQLite database.

    Read from :data:`config.DATABASE_PATH` on every call rather than resolved at
    import, so the deployment's volume path and a test's tmp directory are both
    simply "wherever the database is".
    """
    return config.DATABASE_PATH.parent / _TOKEN_FILENAME


def _read() -> dict[str, Any]:
    """The stored connection, or ``{}`` when there is none.

    A corrupt file reads as "not connected" and says so in the log. Raising would
    make an unparsable byte on the volume take the whole Settings page with it.
    """
    try:
        raw = token_path().read_text("utf-8")
    except OSError:
        return {}
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("Gmail token file at %s is not readable JSON", token_path())
        return {}
    return stored if isinstance(stored, dict) else {}


def _write(data: dict[str, Any]) -> None:
    """Write the connection file, owner-only, creating it that way.

    ``os.open`` with the mode rather than write-then-chmod: the second shape
    leaves the token world-readable for the moment in between, which is the only
    moment that matters. The explicit chmod covers a file that already existed,
    since O_CREAT does not touch the mode of one it did not create.
    """
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_MODE)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    path.chmod(_TOKEN_MODE)


def _forget(reason: str) -> None:
    """Drop the credential but keep the reason, so the panel can explain itself.

    Used when Google refuses the refresh token. The file stays, holding no secret
    — only the sentence Settings shows next to the connect button.
    """
    _write({"lost": reason})
    _log.warning("Gmail connection ended at Google: %s", reason)


# --- Network -------------------------------------------------------------------


def _decode(body: bytes) -> dict[str, Any]:
    """Parse a Google JSON body; an empty body is an empty payload.

    The revoke endpoint answers 200 with no content at all, which is a success
    and not a parse failure.
    """
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GmailError(f"Google antwortete nicht in JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _fetch(
    url: str, *, form: dict[str, str] | None = None, token: str = ""
) -> dict[str, Any]:
    """One call to Google. The only place in this package that reaches it.

    A 4xx comes back as the parsed payload rather than an exception, because
    Google puts the thing the caller has to branch on — ``error:
    "invalid_grant"`` — in the body of exactly those responses. Everything else
    (unreachable, timeout, 5xx without a body) raises :class:`GmailError`.

    The message of that error never carries the response body: bodies from the
    token endpoint are where a token would be if one leaked into a log.
    """
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoints
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return _decode(response.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = _decode(exc.read(_MAX_ERROR_BYTES))
        except GmailError:
            payload = {}
        if payload.get("error"):
            return payload
        raise GmailError(f"Google antwortete mit HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GmailError(f"Google ist nicht erreichbar: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GmailError(f"Google antwortete nicht innerhalb von {_TIMEOUT}s") from exc


def _error_text(payload: dict[str, Any]) -> str:
    """Google's error as one line: the code, plus its description when there is one.

    Only these two fields, never the whole payload — see :func:`_fetch`.
    """
    code = str(payload.get("error") or "")
    detail = str(payload.get("error_description") or "")
    return f"{code}: {detail}" if detail else code


# --- The round trip ------------------------------------------------------------


def new_state() -> str:
    """A fresh, unguessable ``state`` for one authorisation attempt."""
    return secrets.token_urlsafe(32)


def authorize_url(state: str) -> str:
    """Where to send the operator for consent.

    ``access_type=offline`` is what makes Google issue a refresh token at all,
    and ``prompt=consent`` is what makes it issue one *again* after a disconnect
    — without it the second connection comes back with an access token alone and
    dies an hour later.
    """
    if not config.gmail_configured():
        raise GmailError("Gmail ist nicht eingerichtet (NEWSPULSE_GMAIL_CLIENT_ID)")
    params = {
        "client_id": config.gmail_client_id(),
        "redirect_uri": config.gmail_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _granted_scopes(payload: dict[str, Any]) -> tuple[str, ...]:
    """What Google actually granted, falling back to what was asked for.

    Read from the response rather than assumed: a person can uncheck a permission
    on the consent screen, and a panel that then names both would be describing a
    connection that does not exist.
    """
    raw = str(payload.get("scope") or "").split()
    return tuple(raw) if raw else SCOPES


def _expiry(payload: dict[str, Any], now: dt.datetime) -> dt.datetime:
    """When the access token in ``payload`` stops working."""
    try:
        seconds = int(payload.get("expires_in", _DEFAULT_EXPIRES_IN))
    except (TypeError, ValueError):
        seconds = _DEFAULT_EXPIRES_IN
    return now + dt.timedelta(seconds=seconds)


def _now(now: dt.datetime | None = None) -> dt.datetime:
    return now or dt.datetime.now(dt.UTC)


def _parse_time(raw: object) -> dt.datetime | None:
    """A stored ISO timestamp, or ``None`` when it is missing or unreadable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def exchange(code: str, *, fetch: Fetch = _fetch) -> Link:
    """Turn the callback's one-time code into a stored connection.

    The profile is fetched *before* anything is written, so a mailbox that
    answers the token exchange but not a read is not recorded as connected. The
    address that lands in the file is Google's answer, which is the whole reason
    Settings can show one at all.
    """
    payload = fetch(
        _TOKEN_ENDPOINT,
        form={
            "code": code,
            "client_id": config.gmail_client_id(),
            "client_secret": config.gmail_client_secret(),
            "redirect_uri": config.gmail_redirect_uri(),
            "grant_type": "authorization_code",
        },
    )
    if payload.get("error"):
        raise GmailError(f"Google lehnte den Code ab ({_error_text(payload)})")
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access or not refresh:
        # Without a refresh token the connection would last an hour. Google omits
        # one when consent was already granted and prompt=consent was not sent —
        # a bug in the request, so say that rather than storing a dead link.
        raise GmailError("Google lieferte keinen Refresh-Token zurück")

    now = _now()
    found = _profile(access, fetch=fetch)
    scopes = _granted_scopes(payload)
    _write(
        {
            "refresh_token": refresh,
            "access_token": access,
            "expires_at": _expiry(payload, now).isoformat(),
            "scopes": list(scopes),
            "email": found.email,
            "connected_at": now.isoformat(),
            "lost": "",
        }
    )
    # The address, never the token. This line ends up in the deployment's log.
    _log.info("Gmail connected as %s (%s)", found.email, " ".join(scopes))
    return Link(email=found.email, scopes=scopes, connected_at=now)


def token(*, fetch: Fetch = _fetch, now: dt.datetime | None = None) -> str | None:
    """A usable access token, refreshed if the stored one has expired.

    ``None`` means no mailbox is connected — including the case where Google has
    just told us the refresh token is dead, which disconnects and records why.
    That path deliberately does not raise: access being revoked at Google is a
    state this tool has to be able to display, not an exception for every caller
    to remember to catch.

    Any *other* token error does raise. "Google had a bad minute" must not delete
    a working credential.
    """
    stored = _read()
    refresh = str(stored.get("refresh_token") or "")
    if not refresh:
        return None
    moment = _now(now)
    access = str(stored.get("access_token") or "")
    expires_at = _parse_time(stored.get("expires_at"))
    if access and expires_at is not None:
        if moment < expires_at - dt.timedelta(seconds=_EXPIRY_SKEW_SECONDS):
            return access

    payload = fetch(
        _TOKEN_ENDPOINT,
        form={
            "client_id": config.gmail_client_id(),
            "client_secret": config.gmail_client_secret(),
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    if payload.get("error"):
        if payload.get("error") == _REVOKED_ERROR:
            _forget(_error_text(payload))
            return None
        raise GmailError(f"Gmail-Token nicht erneuerbar ({_error_text(payload)})")

    fresh = str(payload.get("access_token") or "")
    if not fresh:
        raise GmailError("Google erneuerte den Zugriff ohne Token")
    stored["access_token"] = fresh
    stored["expires_at"] = _expiry(payload, moment).isoformat()
    # A refresh response may re-state the scopes; keep the file's answer current.
    if payload.get("scope"):
        stored["scopes"] = list(_granted_scopes(payload))
    _write(stored)
    return fresh


def _profile(access: str, *, fetch: Fetch = _fetch) -> Profile:
    """The Gmail profile behind one access token."""
    payload = fetch(_PROFILE_ENDPOINT, token=access)
    if payload.get("error"):
        detail = payload.get("error")
        message = detail.get("message") if isinstance(detail, dict) else _error_text(payload)
        raise GmailError(f"Gmail-Profil nicht lesbar ({message})")
    email = str(payload.get("emailAddress") or "")
    if not email:
        raise GmailError("Gmail nannte keine Adresse für dieses Konto")
    try:
        total = int(payload.get("messagesTotal", 0))
    except (TypeError, ValueError):
        total = 0
    return Profile(
        email=email, messages_total=total, history_id=str(payload.get("historyId") or "")
    )


def profile(*, fetch: Fetch = _fetch) -> Profile:
    """Ask Gmail who this is — the proof behind "verbunden als …".

    Raises when nothing is connected: a caller asking for the profile of no
    mailbox has a bug, unlike a caller asking for a token.
    """
    access = token(fetch=fetch)
    if access is None:
        raise GmailError("Kein Postfach verbunden")
    return _profile(access, fetch=fetch)


def connected() -> Link | None:
    """The stored connection, or ``None`` when this machine has never had one.

    Reads the file and nothing else — no network — because Settings renders on
    every page load and a panel that costs a round trip to Google is a panel that
    makes the whole page as slow as Google's worst minute. The address it returns
    came from :func:`exchange`, i.e. from Gmail itself.
    """
    stored = _read()
    if not stored:
        return None
    return Link(
        email=str(stored.get("email") or ""),
        scopes=tuple(str(s) for s in stored.get("scopes") or ()),
        connected_at=_parse_time(stored.get("connected_at")),
        lost=str(stored.get("lost") or ""),
    )


def disconnect(*, fetch: Fetch = _fetch) -> bool:
    """Revoke the token at Google and delete the local file.

    Returns whether Google confirmed the revocation. The file goes either way:
    "disconnect" that leaves a working credential on the volume because a request
    failed is the failure mode this function exists to avoid. Touches no letter,
    no reply and no other row — this module holds no database session at all.
    """
    stored = _read()
    refresh = str(stored.get("refresh_token") or "")
    revoked = False
    if refresh:
        try:
            payload = fetch(_REVOKE_ENDPOINT, form={"token": refresh})
            revoked = not payload.get("error")
            if not revoked:
                _log.warning("Google refused the revocation: %s", _error_text(payload))
        except GmailError as exc:
            _log.warning("Gmail revocation could not be delivered: %s", exc)
    token_path().unlink(missing_ok=True)
    _log.info("Gmail disconnected (revoked at Google: %s)", revoked)
    return revoked


def scope_words(scopes: tuple[str, ...] = SCOPES) -> list[str]:
    """The permissions in German — what the consent screen is about to say."""
    return [_SCOPE_WORDS.get(scope, scope) for scope in scopes]


__all__ = [
    "GmailError",
    "Link",
    "Profile",
    "SCOPES",
    "authorize_url",
    "connected",
    "disconnect",
    "exchange",
    "new_state",
    "profile",
    "scope_words",
    "token",
    "token_path",
]
