"""The connection to one Gmail account: consent, token, profile, disconnect —
and, since DEC-4 locked option C, the letter itself.

This module owns the OAuth round trip (DEC-5: the Gmail API through an *Internal*
app in RAUTE's own Google Workspace), the refresh token that has to survive a
restart, the honest answer to "is a mailbox connected, and as whom", and the one
outward act this tool performs: composing a released letter as a draft and
sending it. It holds no database session and decides nothing about *whether* a
letter should go — that judgement is a person's, taken on the card.

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

import base64
import binascii
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
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import parseaddr
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import config

_log = logging.getLogger(__name__)

# --- Google's endpoints --------------------------------------------------------

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
_PROFILE_ENDPOINT = f"{_API_ROOT}/profile"
_DRAFTS_ENDPOINT = f"{_API_ROOT}/drafts"
_DRAFTS_SEND_ENDPOINT = f"{_API_ROOT}/drafts/send"
_THREADS_ENDPOINT = f"{_API_ROOT}/threads"

# Where a person opens the conversation this tool started. Gmail addresses a
# thread by its own id in the web client's fragment, so the link the card renders
# is the same conversation the sync reads, not a search that might find another.
_THREAD_WEB_URL = "https://mail.google.com/mail/u/0/#all/{thread_id}"

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
#
# The last three are never asked for here. Google adds them to a grant on its own
# when the consent screen is an OpenID one, and they arrive in the token
# response's `scope` — which is what the panel renders. Without a word for them
# the panel would print a URL where it promised words.
_SCOPE_WORDS: dict[str, str] = {
    "https://www.googleapis.com/auth/gmail.readonly": "Nachrichten lesen",
    "https://www.googleapis.com/auth/gmail.send": "Nachrichten senden",
    "openid": "Anmeldung bei Google",
    "https://www.googleapis.com/auth/userinfo.email": "E-Mail-Adresse des Kontos",
    "https://www.googleapis.com/auth/userinfo.profile": "Name des Kontos",
}

# --- The token file ------------------------------------------------------------

# Beside the database, not in it. Named for what it holds so an operator looking
# at the volume knows immediately that this file is a credential.
_TOKEN_FILENAME = "gmail_token.json"

# Owner read/write only. The whole point of keeping the token out of the database
# is that it stays on the machine it was granted to; a world-readable file would
# give that back on a shared host.
_TOKEN_MODE = 0o600

# The file is written to a sibling and renamed over the real name, so a reader
# only ever sees a whole record. A truncate-in-place leaves a window in which the
# file is half a JSON document, and a crash inside that window leaves it there.
_TOKEN_TEMP_SUFFIX = ".new"

# The file's own word for "this is the note a connection left behind, not a
# connection". Written by _forget, and it wins over every other key: a file that
# still carries an address from a hand-edit cannot read as connected while it is
# set.
_LOST_STATE = "verloren"

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

# Gmail's answer when it does not accept the access token itself. Google
# invalidates access tokens *before* their stated expiry when the account's
# password changes or a session is revoked, so this is not the same statement as
# the stored `expires_at`, and a caller that has been told it knows more than the
# file does.
_UNAUTHORIZED = 401

# What ``threads.get`` is asked for. "full" carries the headers and the decoded
# body parts; "metadata" would carry neither, and the round trip this module
# promises — the subject and text that were sent, read back byte for byte — needs
# the body.
_THREAD_FORMAT = "full"

# Gmail states a message's own moment as milliseconds since the epoch, in a
# string. It is the send date for a message this tool sent and the receipt date
# for a reply, and in both cases it is Google's clock rather than ours.
_MILLISECONDS = 1000

# How a letter is serialised: CRLF line endings, and a body encoded down to 7 bit.
# ``policy.SMTP`` alone leaves a German letter as raw 8-bit UTF-8 in the body,
# which is legal only on a path that negotiated 8BITMIME. Quoted-printable is
# what every mail transport on earth accepts, and the round trip is unaffected —
# Gmail hands the body back decoded either way.
_MAIL_POLICY = SMTP.clone(cte_type="7bit")


class GmailError(RuntimeError):
    """A Gmail call failed in a way the caller has to hear about.

    Deliberately *not* raised when Google says the access was revoked: that is a
    fact about the connection, so it disconnects and reports rather than raising
    into a page render (see :func:`token`).
    """


class GmailUnauthorized(GmailError):
    """Gmail refused the access token, whatever the stored expiry claimed.

    Its own type so one call site can retry with a renewed token instead of every
    caller pattern-matching on Google's message. Still a :class:`GmailError`, so
    a caller that does not care about the difference is unaffected.
    """


class Revocation(StrEnum):
    """What :func:`disconnect` managed to do about the token at Google.

    Three outcomes, not two. "There was nothing to revoke" is the ordinary result
    of a double-submitted form or a stale tab, and reporting that as a refused
    revocation sends the operator to hunt through their Google account for a
    permission that was already gone.
    """

    REVOKED = "revoked"
    NOTHING = "nothing"
    REFUSED = "refused"


#: What one network call looks like from in here: a URL, an optional form body
#: (its presence makes it a POST), an optional bearer token. Returns Google's
#: parsed JSON — including an error payload, which callers read rather than
#: catch, so a stub in a test is a dict in and a dict out.
Fetch = Callable[..., dict[str, Any]]


def _words(scope: str) -> str:
    """One scope as something a person can read.

    An unknown scope falls back to its last path segment rather than to the whole
    URL: the panel promises the granted permissions "in Worten", and a
    https://www.googleapis.com/auth/… string on that line is the panel breaking
    its own promise the first time Google widens a grant.
    """
    known = _SCOPE_WORDS.get(scope)
    if known:
        return known
    return scope.rstrip("/").rsplit("/", 1)[-1] or scope


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
        return [_words(scope) for scope in self.scopes]


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

    An unreadable file reads as "not connected" and says so in the log. Raising
    would make one bad byte on the volume take the whole Settings page with it —
    and "unreadable" includes bytes that are not UTF-8 at all, which is what a
    truncated volume restore or a torn write leaves behind. ``read_text`` answers
    that with ``UnicodeDecodeError``, which is a ``ValueError`` and not an
    ``OSError``, so it needs naming here.
    """
    try:
        raw = token_path().read_text("utf-8")
    except OSError:
        return {}
    except UnicodeDecodeError:
        _log.warning("Gmail token file at %s is not readable text", token_path())
        return {}
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("Gmail token file at %s is not readable JSON", token_path())
        return {}
    return stored if isinstance(stored, dict) else {}


def _write(data: dict[str, Any]) -> None:
    """Write the connection file, owner-only and in one step.

    ``os.open`` with the mode rather than write-then-chmod: the second shape
    leaves the token world-readable for the moment in between, which is the only
    moment that matters. The explicit chmod covers a temp file that already
    existed, since O_CREAT does not touch the mode of one it did not create.

    Written to a sibling and renamed over the real name: ``os.replace`` within one
    directory is atomic, so a reader sees either the old record or the new one and
    never half of either. Truncating in place instead means a crash mid-write
    leaves a file that is neither.
    """
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + _TOKEN_TEMP_SUFFIX)
    handle = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_MODE)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        staged.chmod(_TOKEN_MODE)
        os.replace(staged, path)
    finally:
        # A no-op after a successful rename; the cleanup that matters is the
        # failed one, which must not leave a readable fragment behind.
        staged.unlink(missing_ok=True)


def _forget(reason: str) -> None:
    """Drop the credential but keep the reason, so the panel can explain itself.

    Used when Google refuses the refresh token. What stays holds no secret and is
    marked as what it is — the note a connection left behind, not a connection —
    so no later reader can mistake it for one.
    """
    _write({"state": _LOST_STATE, "lost": reason})
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
    url: str,
    *,
    form: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    method: str = "",
    token: str = "",
) -> dict[str, Any]:
    """One call to Google. The only place in this package that reaches it.

    Two body shapes, because Google uses two: OAuth speaks form encoding and the
    Gmail API speaks JSON. Which one is present decides the ``Content-Type``, and
    the presence of either makes the request a POST unless ``method`` says
    otherwise — ``drafts.update`` is the one call here that is a PUT.

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
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoints
        url,
        data=data,
        headers=headers,
        method=method or ("POST" if data is not None else "GET"),
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


def _caller(fetch: Fetch | None) -> Fetch:
    """The injected call, or the real one.

    Resolved per call rather than as a default argument. ``fetch: Fetch = _fetch``
    binds the function object at import, which would leave this module's single
    network boundary as the one thing a caller could not replace without
    threading it through every call site — including the web routes, where the
    point is that a request handler knows nothing about how Google is reached.
    """
    return fetch or _fetch


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


def exchange(code: str, *, fetch: Fetch | None = None) -> Link:
    """Turn the callback's one-time code into a stored connection.

    The profile is fetched *before* anything is written, so a mailbox that
    answers the token exchange but not a read is not recorded as connected. The
    address that lands in the file is Google's answer, which is the whole reason
    Settings can show one at all.
    """
    call = _caller(fetch)
    payload = call(
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
    found = _profile(access, fetch=call)
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


def _unexpired(stored: dict[str, Any], moment: dt.datetime) -> str:
    """The stored access token while it is still good by its own expiry, else ``""``.

    A record with no readable expiry counts as expired: one extra refresh is the
    right price for not knowing.
    """
    access = str(stored.get("access_token") or "")
    expires_at = _parse_time(stored.get("expires_at"))
    if not access or expires_at is None:
        return ""
    usable_until = expires_at - dt.timedelta(seconds=_EXPIRY_SKEW_SECONDS)
    return access if moment < usable_until else ""


def _keep_renewed(
    refresh: str, access: str, expires_at: dt.datetime, payload: dict[str, Any]
) -> None:
    """Store a renewed access token — unless the credential it belongs to is gone.

    The refresh is a network call that may take up to :data:`_TIMEOUT`, and a
    disconnect landing inside that window has already revoked *this* refresh token
    at Google and deleted the file. Writing the record back wholesale would put
    the revoked credential on disk again, and the panel would then name a mailbox
    whose access ended. So the file is re-read, and only a record that is still
    the same one is updated; otherwise the renewed access token is dropped, which
    costs the next caller one refresh and nothing else.
    """
    stored = _read()
    if str(stored.get("refresh_token") or "") != refresh:
        _log.info("Gmail credential changed during a refresh; renewed access dropped")
        return
    stored["access_token"] = access
    stored["expires_at"] = expires_at.isoformat()
    # A refresh response may re-state the scopes; keep the file's answer current.
    if payload.get("scope"):
        stored["scopes"] = list(_granted_scopes(payload))
    _write(stored)


def token(
    *,
    fetch: Fetch | None = None,
    now: dt.datetime | None = None,
    renew: bool = False,
) -> str | None:
    """A usable access token, refreshed if the stored one has expired.

    ``None`` means no mailbox is connected — including the case where Google has
    just told us the refresh token is dead, which disconnects and records why.
    That path deliberately does not raise: access being revoked at Google is a
    state this tool has to be able to display, not an exception for every caller
    to remember to catch.

    Any *other* token error does raise. "Google had a bad minute" must not delete
    a working credential.

    ``renew=True`` ignores the stored access token and asks Google for a new one.
    The stored expiry is only Google's *statement* about a lifetime; it also
    invalidates access tokens early, on a password change or a revoked session. A
    caller that has just been handed :data:`_UNAUTHORIZED` therefore knows
    something the file does not (see :func:`profile`).
    """
    call = _caller(fetch)
    stored = _read()
    refresh = str(stored.get("refresh_token") or "")
    if not refresh:
        return None
    moment = _now(now)
    if not renew:
        access = _unexpired(stored, moment)
        if access:
            return access

    payload = call(
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
    _keep_renewed(refresh, fresh, _expiry(payload, moment), payload)
    return fresh


def _raise_for_api_error(payload: dict[str, Any], what: str) -> None:
    """Turn a Gmail API error payload into the right exception, or return.

    A 401 is told apart from the rest, because it is the one error the caller can
    do something about: the token was refused, so a renewed one may work.

    Shared by every Gmail call rather than repeated at each: Google nests the
    Gmail API's error under ``error.message`` and the OAuth endpoints' under a
    flat ``error``, and a second copy of that branch is a second place for the
    401 case to be forgotten.
    """
    detail = payload.get("error")
    if not detail:
        return
    message = detail.get("message") if isinstance(detail, dict) else _error_text(payload)
    if isinstance(detail, dict) and detail.get("code") == _UNAUTHORIZED:
        raise GmailUnauthorized(f"Gmail lehnte den Zugriff ab ({message})")
    raise GmailError(f"{what} ({message})")


def _profile(access: str, *, fetch: Fetch | None = None) -> Profile:
    """The Gmail profile behind one access token."""
    payload = _caller(fetch)(_PROFILE_ENDPOINT, token=access)
    _raise_for_api_error(payload, "Gmail-Profil nicht lesbar")
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


def profile(*, fetch: Fetch | None = None) -> Profile:
    """Ask Gmail who this is — the proof behind "verbunden als …".

    Raises when nothing is connected: a caller asking for the profile of no
    mailbox has a bug, unlike a caller asking for a token.

    A refused token is retried once with a renewed one, rather than reported. The
    stored expiry is Google's statement about a lifetime, and Google breaks it in
    the direction that matters — a password change invalidates every access token
    it has issued, minutes into an hour the file still considers valid. If the
    renewal shows the whole grant is gone, that has already disconnected and
    recorded why (see :func:`token`), so the panel explains itself on the next
    render and this raises rather than inventing a mailbox.
    """
    call = _caller(fetch)
    access = token(fetch=call)
    if access is None:
        raise GmailError("Kein Postfach verbunden")
    try:
        return _profile(access, fetch=call)
    except GmailUnauthorized as refused:
        _log.info("Gmail refused a stored access token before its expiry; renewing")
        renewed = token(fetch=call, renew=True)
        if renewed is None:
            raise GmailError(
                "Kein Postfach verbunden — der Zugriff wurde bei Google entzogen"
            ) from refused
        return _profile(renewed, fetch=call)


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
    if stored.get("state") == _LOST_STATE:
        # The note a lost connection left behind. Read as *only* that, so an
        # address that somehow ends up beside it cannot make the panel claim a
        # mailbox whose access Google has already withdrawn.
        return Link(lost=str(stored.get("lost") or ""))
    return Link(
        email=str(stored.get("email") or ""),
        scopes=tuple(str(s) for s in stored.get("scopes") or ()),
        connected_at=_parse_time(stored.get("connected_at")),
        lost=str(stored.get("lost") or ""),
    )


def disconnect(*, fetch: Fetch | None = None) -> Revocation:
    """Revoke the token at Google and delete the local file.

    Returns what became of the token at Google — revoked, refused, or nothing to
    revoke in the first place. The local file goes in every one of those cases: a
    "disconnect" that leaves a working credential on the volume because a request
    failed is the failure mode this function exists to avoid. Touches no letter,
    no reply and no other row — this module holds no database session at all.
    """
    stored = _read()
    refresh = str(stored.get("refresh_token") or "")
    outcome = Revocation.NOTHING
    if refresh:
        outcome = Revocation.REFUSED
        try:
            payload = _caller(fetch)(_REVOKE_ENDPOINT, form={"token": refresh})
            if payload.get("error"):
                _log.warning("Google refused the revocation: %s", _error_text(payload))
            else:
                outcome = Revocation.REVOKED
        except GmailError as exc:
            _log.warning("Gmail revocation could not be delivered: %s", exc)
    token_path().unlink(missing_ok=True)
    _log.info("Gmail disconnected (token at Google: %s)", outcome.value)
    return outcome


def scope_words(scopes: tuple[str, ...] = SCOPES) -> list[str]:
    """The permissions in German — what the consent screen is about to say."""
    return [_words(scope) for scope in scopes]


# --- The letter, in Gmail -------------------------------------------------------
#
# DEC-4 locked option C: RauteOS composes the letter in the connected mailbox and
# sends it. It does that in two steps rather than one call to ``messages.send``,
# and the reason is the one thing that cannot be undone in this whole tool. A
# bare send that times out leaves the caller unable to tell "Gmail never got it"
# from "Gmail got it and the answer was lost", and the only safe response to that
# ambiguity is to not retry — which means a letter silently never goes out. A
# draft is a name for the message *before* it exists as mail: a repeat push
# updates the draft that is already there, so a retry can be safe, and the send
# that follows is the single irreversible act, taken once.


@dataclass(frozen=True, slots=True)
class Draft:
    """A message composed in Gmail but not sent. Every id is Google's."""

    draft_id: str
    message_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class Sent:
    """A message that has left. ``sent_at`` is Gmail's own clock, not ours.

    ``None`` when Google's answer to the send carried no ``internalDate``, which
    it often does not: the caller then has to fall back to its own moment rather
    than invent one here.
    """

    message_id: str
    thread_id: str
    sent_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """One message in a conversation, decoded back into text.

    What the sync in OUT-05 reads, and what makes the round trip checkable here:
    the subject and body that come back through this are the ones that went in.
    """

    message_id: str
    thread_id: str
    from_name: str
    from_email: str
    subject: str
    body: str
    received_at: dt.datetime | None = None
    label_ids: tuple[str, ...] = ()


def thread_url(thread_id: str) -> str:
    """Where a person opens this conversation in Gmail."""
    return _THREAD_WEB_URL.format(thread_id=urllib.parse.quote(thread_id, safe=""))


def build_message(to: str, subject: str, body: str) -> str:
    """One letter as a base64url-encoded RFC 5322 message, the way Gmail wants it.

    Built with :mod:`email` rather than by pasting headers together, because the
    letters this tool writes are German: the subject carries umlauts and so does
    the text, and a hand-rolled ``f"Subject: {subject}"`` puts raw UTF-8 bytes in
    a header field, where the standard allows only ASCII. Gmail accepts that and
    the recipient reads ``AnschlussdauerÃ¼``. ``email.policy.SMTP`` encodes the
    header the RFC 2047 way and picks a transfer encoding for the body, so what
    comes back out of :func:`thread` is what went in.

    One normalisation, and it is the standard's rather than this module's: a mail
    body is a sequence of terminated lines, so a text that did not end in a
    newline is sent with one. Nothing inside the text is touched.
    """
    message = EmailMessage(policy=_MAIL_POLICY)
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _b64url(data: str) -> bytes:
    """Decode Gmail's padding-free base64url; an unreadable value is empty.

    Google omits the ``=`` padding, which :mod:`base64` requires. A body that
    still does not decode is a message this tool cannot read rather than an
    error worth failing a whole sync over.
    """
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        _log.warning("a Gmail message body was not decodable base64")
        return b""


def _api(
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    method: str = "",
    what: str,
    fetch: Fetch | None = None,
) -> dict[str, Any]:
    """One authenticated Gmail API call, with the retry that :func:`profile` has.

    The stored expiry is only Google's *statement* about a lifetime — it
    invalidates access tokens early on a password change or a revoked session —
    so a refusal is worth exactly one renewed attempt before it is reported.
    Raises when nothing is connected: the routes decide whether to offer a Gmail
    action at all, so reaching here with no mailbox is a bug, not a state.
    """
    call = _caller(fetch)
    access = token(fetch=call)
    if access is None:
        raise GmailError("Kein Postfach verbunden")
    try:
        answer = call(url, json_body=json_body, method=method, token=access)
        _raise_for_api_error(answer, what)
        return answer
    except GmailUnauthorized as refused:
        _log.info("Gmail refused a stored access token before its expiry; renewing")
        renewed = token(fetch=call, renew=True)
        if renewed is None:
            raise GmailError(
                "Kein Postfach verbunden — der Zugriff wurde bei Google entzogen"
            ) from refused
        answer = call(url, json_body=json_body, method=method, token=renewed)
        _raise_for_api_error(answer, what)
        return answer


def _ids(message: dict[str, Any]) -> tuple[str, str]:
    """The message and thread id out of a Gmail ``Message``, or a refusal.

    Read from Google's answer and never assembled here. An id this tool made up
    would point at a conversation that does not exist, and the whole reason the
    thread is stored is that OUT-05 can trust it to be the real one — so an
    answer that names neither is a failure rather than a row with a plausible
    blank in it.
    """
    message_id = str(message.get("id") or "")
    thread_id = str(message.get("threadId") or "")
    if not message_id or not thread_id:
        raise GmailError("Gmail nannte weder Nachricht noch Verlauf")
    return message_id, thread_id


def create_draft(
    to: str,
    subject: str,
    body: str,
    *,
    draft_id: str = "",
    thread_id: str = "",
    fetch: Fetch | None = None,
) -> Draft:
    """Compose this letter in Gmail, or rewrite the draft already composed.

    ``draft_id`` makes the call a ``drafts.update``: pushing the same letter
    twice updates the one draft rather than leaving a second, near-identical
    message sitting in the mailbox at the same journalist. ``thread_id`` keeps a
    re-composed draft inside the conversation it already belonged to.
    """
    payload: dict[str, Any] = {"message": {"raw": build_message(to, subject, body)}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    if draft_id:
        answer = _api(
            f"{_DRAFTS_ENDPOINT}/{urllib.parse.quote(draft_id, safe='')}",
            json_body=payload,
            method="PUT",
            what="Gmail-Entwurf nicht aktualisierbar",
            fetch=fetch,
        )
    else:
        answer = _api(
            _DRAFTS_ENDPOINT,
            json_body=payload,
            what="Gmail-Entwurf nicht anlegbar",
            fetch=fetch,
        )
    stored_id = str(answer.get("id") or "")
    message = answer.get("message")
    if not stored_id or not isinstance(message, dict):
        raise GmailError("Gmail nannte keinen Entwurf")
    message_id, opened_thread = _ids(message)
    return Draft(draft_id=stored_id, message_id=message_id, thread_id=opened_thread)


def _sent_at(message: dict[str, Any]) -> dt.datetime | None:
    """Gmail's own moment for this message, if its answer carried one."""
    raw = message.get("internalDate")
    if raw is None:
        return None
    try:
        stamped = int(raw)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(stamped / _MILLISECONDS, dt.UTC)


def send(draft_id: str, *, fetch: Fetch | None = None) -> Sent:
    """Send the composed draft. This is the irreversible one.

    Separate from :func:`create_draft` on purpose: composing can be repeated
    safely and this cannot, so the two are not one call that a retry would
    perform twice. DEC-4 option C is what licenses it at all, and the card asks
    twice before it gets here.
    """
    answer = _api(
        _DRAFTS_SEND_ENDPOINT,
        json_body={"id": draft_id},
        what="Gmail hat die Nachricht nicht verschickt",
        fetch=fetch,
    )
    message_id, thread_id = _ids(answer)
    return Sent(message_id=message_id, thread_id=thread_id, sent_at=_sent_at(answer))


def _header(part: dict[str, Any], name: str) -> str:
    """One header off a Gmail payload, matched case-insensitively as RFC 5322
    requires — Google returns ``Subject`` and ``From`` but promises neither."""
    wanted = name.casefold()
    for header in part.get("headers") or ():
        if isinstance(header, dict) and str(header.get("name", "")).casefold() == wanted:
            return str(header.get("value") or "")
    return ""


def _plain_text(part: dict[str, Any]) -> str:
    """The readable text of a Gmail payload, walking into multipart messages.

    ``text/plain`` first and HTML never: what this reads is either a letter this
    tool wrote — which is plain text by construction — or a journalist's reply,
    where the plain alternative is the one a person typed. Storing a mail client's
    HTML instead would put markup into the contact's file.

    Line endings come back as ``\\n``. Mail is CRLF on the wire, so a letter
    stored here with ``\\n`` and read back through Gmail would otherwise differ
    from itself by two invisible bytes per paragraph — enough to make "is this
    the text we sent" unanswerable by comparison, which is the one question the
    stored copy exists to answer.
    """
    mime = str(part.get("mimeType") or "")
    if mime.startswith("text/plain"):
        body = part.get("body")
        data = str(body.get("data") or "") if isinstance(body, dict) else ""
        return _b64url(data).decode("utf-8", "replace").replace("\r\n", "\n")
    for nested in part.get("parts") or ():
        if isinstance(nested, dict):
            found = _plain_text(nested)
            if found:
                return found
    return ""


def _message(raw: dict[str, Any]) -> ThreadMessage:
    """One Gmail ``Message`` in the shape the rest of this codebase reads."""
    payload = raw.get("payload")
    part = payload if isinstance(payload, dict) else {}
    name, address = parseaddr(_header(part, "From"))
    labels = raw.get("labelIds") or ()
    return ThreadMessage(
        message_id=str(raw.get("id") or ""),
        thread_id=str(raw.get("threadId") or ""),
        from_name=name,
        from_email=address,
        subject=_header(part, "Subject"),
        body=_plain_text(part),
        received_at=_sent_at(raw),
        label_ids=tuple(str(label) for label in labels),
    )


def thread(thread_id: str, *, fetch: Fetch | None = None) -> list[ThreadMessage]:
    """Every message in one conversation, oldest first as Gmail returns them.

    Asked for by thread id and by nothing else. That is DEC-6 option A in one
    line: the only conversations this tool can name are the ones it started, so a
    mailbox full of everything else is neither fetched nor storable.
    """
    answer = _api(
        f"{_THREADS_ENDPOINT}/{urllib.parse.quote(thread_id, safe='')}"
        f"?format={_THREAD_FORMAT}",
        what="Gmail-Verlauf nicht lesbar",
        fetch=fetch,
    )
    return [
        _message(raw)
        for raw in answer.get("messages") or ()
        if isinstance(raw, dict)
    ]


__all__ = [
    "SCOPES",
    "Draft",
    "GmailError",
    "GmailUnauthorized",
    "Link",
    "Profile",
    "Revocation",
    "Sent",
    "ThreadMessage",
    "authorize_url",
    "build_message",
    "connected",
    "create_draft",
    "disconnect",
    "exchange",
    "new_state",
    "profile",
    "scope_words",
    "send",
    "thread",
    "thread_url",
    "token",
    "token_path",
]
