"""Connecting one Gmail account (OUT-03).

Nothing in this file reaches Google. Every network call in ``gmail_link`` goes
through an injected ``fetch``, and :class:`FakeGoogle` is what the tests inject —
either explicitly, or by replacing the module's one boundary function so the web
routes exercise the real code path with a stub behind it.

Two properties are worth more than the rest and are asserted directly rather than
inferred: the consent screen asks for exactly the two scopes DEC-4 licenses, and
the refresh token exists only in a 0600 file beside the database — never in a
table, never in the log.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import stat
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, gmail_link
from newspulse.models import Angle, Base, Client, Contact, Outreach, OutreachState
from newspulse.web.app import create_app, get_db
from newspulse.web.routes.settings import GmailNotice

# The values used throughout. Spelled here so a test that asserts a token never
# reaches the log or the database has one distinctive string to look for.
_REFRESH = "1//refresh-token-that-must-never-leak"
_ACCESS = "ya29.access-token"
_MAILBOX = "lucas@raute.example"
_NOW = dt.datetime(2026, 8, 19, 9, 0, tzinfo=dt.UTC)


# --- A stand-in for Google ------------------------------------------------------


@dataclass
class Call:
    """One request ``gmail_link`` would have made."""

    url: str
    form: dict[str, str] | None
    token: str


@dataclass
class FakeGoogle:
    """Answers the three endpoints, and remembers what it was asked.

    Replies are per-endpoint rather than positional, so a test that adds a call
    somewhere does not silently shift another test's answers.
    """

    token_replies: list[dict[str, Any]] = field(default_factory=list)
    profile_reply: dict[str, Any] = field(default_factory=dict)
    revoke_reply: dict[str, Any] = field(default_factory=dict)
    unreachable: bool = False
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, url: str, *, form: dict[str, str] | None = None, token: str = ""
    ) -> dict[str, Any]:
        self.calls.append(Call(url, form, token))
        if self.unreachable:
            raise gmail_link.GmailError("Google ist nicht erreichbar: test")
        if url.endswith("/token"):
            assert self.token_replies, f"unscripted token call: {form}"
            return self.token_replies.pop(0)
        if url.endswith("/revoke"):
            return self.revoke_reply
        if "profile" in url:
            return self.profile_reply
        raise AssertionError(f"unexpected endpoint {url}")

    def urls(self) -> list[str]:
        return [call.url for call in self.calls]


def _granted(**extra: Any) -> dict[str, Any]:
    """A successful token response, the shape Google actually returns."""
    return {
        "access_token": _ACCESS,
        "refresh_token": _REFRESH,
        "expires_in": 3599,
        "scope": " ".join(gmail_link.SCOPES),
        "token_type": "Bearer",
        **extra,
    }


def _google(**extra: Any) -> FakeGoogle:
    """A Google that grants consent and answers the profile as ``_MAILBOX``."""
    return FakeGoogle(
        token_replies=[_granted()],
        profile_reply={"emailAddress": _MAILBOX, "messagesTotal": 12345},
        **extra,
    )


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A configured OAuth client and a database directory to write beside.

    ``DATABASE_PATH`` is monkeypatched on the module rather than through the env,
    because config resolves it once at import (its own docstring says so).
    """
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "newspulse.db")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(config, "BASE_URL", "https://rauteos.example")
    return tmp_path


@pytest.fixture
def unconfigured(volume, monkeypatch):
    """The same machine with no OAuth client at all."""
    monkeypatch.delenv("NEWSPULSE_GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("NEWSPULSE_GMAIL_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "")
    monkeypatch.setattr(config, "GMAIL_CLIENT_SECRET", "")
    return volume


# --- Configuration --------------------------------------------------------------


def test_without_a_client_id_the_integration_is_not_configured(unconfigured):
    assert config.gmail_configured() is False


def test_an_id_without_a_secret_is_not_configured(volume, monkeypatch):
    """Half a client gets through consent and fails at the exchange — after the
    operator has already handed over their mailbox."""
    monkeypatch.delenv("NEWSPULSE_GMAIL_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(config, "GMAIL_CLIENT_SECRET", "")
    assert config.gmail_configured() is False


def test_the_redirect_uri_is_derived_from_the_base_url(volume):
    """One spelling of the address, because Google matches it character for
    character against what is registered in the Cloud console."""
    assert config.gmail_redirect_uri() == (
        "https://rauteos.example/settings/gmail/callback"
    )


def test_the_token_file_sits_beside_the_database(volume):
    assert gmail_link.token_path().parent == config.DATABASE_PATH.parent


# --- The consent screen ---------------------------------------------------------


def test_consent_asks_for_exactly_the_two_scopes_dec_4_licenses(volume):
    """DEC-4 locked "lesen und senden". Not modify, not mail.google.com."""
    query = parse_qs(urlparse(gmail_link.authorize_url("s")).query)
    assert query["scope"][0].split() == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]


def test_the_authorisation_url_carries_the_state_and_asks_for_a_refresh_token(volume):
    query = parse_qs(urlparse(gmail_link.authorize_url("state-42")).query)
    assert query["state"] == ["state-42"]
    # Without both of these Google hands back an access token that dies in an
    # hour, and the daily sync with it.
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [config.gmail_redirect_uri()]


def test_authorize_url_refuses_when_no_oauth_client_is_configured(unconfigured):
    with pytest.raises(gmail_link.GmailError):
        gmail_link.authorize_url("s")


def test_two_states_are_never_the_same():
    assert gmail_link.new_state() != gmail_link.new_state()


# --- Completing the round trip --------------------------------------------------


def test_a_completed_exchange_stores_the_refresh_token_in_a_0600_file(volume):
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)

    path = gmail_link.token_path()
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["refresh_token"] == _REFRESH


def test_the_connected_address_is_read_back_from_the_gmail_profile(volume):
    """Not from what anybody typed: the panel's claim is only worth as much as
    the call that proves it."""
    google = _google()
    link = gmail_link.exchange("auth-code", fetch=google)

    assert link.email == _MAILBOX
    assert gmail_link.connected().email == _MAILBOX
    assert gmail_link._PROFILE_ENDPOINT in google.urls()


def test_the_granted_scopes_come_from_googles_answer_not_from_the_request(volume):
    """A person may untick a permission on the consent screen. Naming both
    anyway would describe a connection that does not exist."""
    google = _google()
    google.token_replies = [
        _granted(scope="https://www.googleapis.com/auth/gmail.readonly")
    ]
    link = gmail_link.exchange("auth-code", fetch=google)

    assert link.scopes == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert link.scope_words == ["Nachrichten lesen"]


def test_a_refused_code_raises_and_stores_nothing(volume):
    google = FakeGoogle(token_replies=[{"error": "invalid_grant"}])
    with pytest.raises(gmail_link.GmailError):
        gmail_link.exchange("stale-code", fetch=google)
    assert not gmail_link.token_path().exists()
    assert gmail_link.connected() is None


def test_an_exchange_without_a_refresh_token_stores_nothing(volume):
    """An hour-long connection is worse than none: it looks connected and dies
    unattended overnight."""
    google = _google()
    google.token_replies = [_granted(refresh_token="")]
    with pytest.raises(gmail_link.GmailError):
        gmail_link.exchange("auth-code", fetch=google)
    assert not gmail_link.token_path().exists()


def test_a_mailbox_that_cannot_be_read_is_not_recorded_as_connected(volume):
    """The profile is fetched before anything is written."""
    google = FakeGoogle(
        token_replies=[_granted()],
        profile_reply={"error": {"code": 403, "message": "insufficient permissions"}},
    )
    with pytest.raises(gmail_link.GmailError):
        gmail_link.exchange("auth-code", fetch=google)
    assert not gmail_link.token_path().exists()


# --- Living with the token ------------------------------------------------------


def test_a_valid_access_token_is_returned_without_asking_google(volume):
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    before = len(google.calls)

    assert gmail_link.token(fetch=google, now=_NOW) == _ACCESS
    assert len(google.calls) == before


def test_an_expired_access_token_is_refreshed_transparently(volume):
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    google.token_replies = [{"access_token": "ya29.fresh", "expires_in": 3599}]

    # An hour and a bit later: the stored token is past its stated lifetime.
    later = _now_after(seconds=3600 + 60)
    assert gmail_link.token(fetch=google, now=later) == "ya29.fresh"

    # And the fresh one is kept, so the next call is free again.
    assert gmail_link.token(fetch=google, now=later) == "ya29.fresh"
    refreshes = [c for c in google.calls if c.form and "refresh_token" in c.form]
    assert len(refreshes) == 1
    assert refreshes[0].form["grant_type"] == "refresh_token"


def test_a_revoked_refresh_token_disconnects_with_a_reason_instead_of_raising(volume):
    """Access withdrawn in the Google account is a state to display, not an
    exception for every caller to remember to catch."""
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    google.token_replies = [
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    ]

    assert gmail_link.token(fetch=google, now=_now_after(seconds=7200)) is None

    link = gmail_link.connected()
    assert link is not None
    assert link.is_connected is False
    assert "revoked" in link.lost
    # The credential is gone, not merely ignored.
    assert "refresh_token" not in json.loads(gmail_link.token_path().read_text())


def test_a_transient_token_error_keeps_the_stored_credential(volume):
    """"Google had a bad minute" must not delete a working connection."""
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    google.token_replies = [{"error": "internal_failure"}]

    with pytest.raises(gmail_link.GmailError):
        gmail_link.token(fetch=google, now=_now_after(seconds=7200))
    assert json.loads(gmail_link.token_path().read_text())["refresh_token"] == _REFRESH
    assert gmail_link.connected().is_connected is True


def test_the_stored_connection_survives_a_restart(volume):
    """The whole reason the token is a file: a redeploy must not ask for consent
    again. Nothing here holds state, so re-reading is the restart."""
    gmail_link.exchange("auth-code", fetch=_google())
    assert gmail_link.connected().email == _MAILBOX


def test_profile_uses_the_access_token_as_a_bearer(volume):
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)

    found = gmail_link.profile(fetch=google)
    assert found.email == _MAILBOX
    assert found.messages_total == 12345
    assert google.calls[-1].token == _ACCESS


def test_profile_refuses_when_nothing_is_connected(volume):
    with pytest.raises(gmail_link.GmailError):
        gmail_link.profile(fetch=_google())


def test_a_token_refused_before_its_expiry_is_renewed_rather_than_reported(volume):
    """Google invalidates access tokens early — a password change or a revoked
    session does it — so the stored expiry is not the last word on whether one
    still works. A 401 inside the stored hour is a refresh, not a failure.
    """
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    google.token_replies = [{"access_token": "ya29.renewed", "expires_in": 3599}]
    refusals = {"left": 1}

    def refuse_once(url: str, *, form: Any = None, token: str = "") -> dict[str, Any]:
        """Gmail refuses the stored token once, the way a password change does."""
        if "profile" in url and refusals["left"]:
            refusals["left"] -= 1
            return {"error": {"code": 401, "message": "Invalid Credentials"}}
        return google(url, form=form, token=token)

    found = gmail_link.profile(fetch=refuse_once)

    assert found.email == _MAILBOX
    # It did not wait for the stored expiry: a refresh went out, and the profile
    # was read with the token it returned.
    assert [c for c in google.calls if c.form and "refresh_token" in c.form]
    assert google.calls[-1].token == "ya29.renewed"


def test_a_corrupt_token_file_reads_as_not_connected(volume):
    """A bad byte on the volume must not take the Settings page with it."""
    gmail_link.token_path().write_text("{not json")
    assert gmail_link.connected() is None


def test_a_token_file_that_is_not_text_reads_as_not_connected(volume, web):
    """A torn write or a truncated volume restore leaves bytes that are not UTF-8
    at all — and ``read_text`` answers that with a UnicodeDecodeError, which is a
    ValueError, not an OSError. Unnamed, it travelled out of ``connected()`` and
    took the whole Settings render with it.
    """
    gmail_link.token_path().write_bytes(b"\xff\xfe\x00nicht lesbar")

    assert gmail_link.connected() is None
    assert web.get("/settings").status_code == 200


# --- Disconnecting --------------------------------------------------------------


def test_disconnect_revokes_at_google_and_deletes_the_file(volume):
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)

    assert gmail_link.disconnect(fetch=google) is gmail_link.Revocation.REVOKED

    revocations = [c for c in google.calls if c.url.endswith("/revoke")]
    assert revocations and revocations[0].form == {"token": _REFRESH}
    assert not gmail_link.token_path().exists()
    assert gmail_link.connected() is None


def test_disconnect_deletes_the_file_even_when_google_cannot_be_reached(volume):
    """A "disconnect" that leaves a working credential on the volume because a
    request failed is the failure this is written to avoid."""
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    google.unreachable = True

    assert gmail_link.disconnect(fetch=google) is gmail_link.Revocation.REFUSED
    assert not gmail_link.token_path().exists()


# --- The secret stays put -------------------------------------------------------


def test_no_token_value_is_ever_written_to_the_log(volume, caplog):
    """Every path: connect, refresh, revoked, disconnect."""
    caplog.set_level(logging.DEBUG, logger="newspulse.gmail_link")
    google = _google()
    gmail_link.exchange("auth-code", fetch=google)
    google.token_replies = [
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    ]
    gmail_link.token(fetch=google, now=_now_after(seconds=7200))
    gmail_link.disconnect(fetch=google)

    logged = caplog.text
    assert _REFRESH not in logged
    assert _ACCESS not in logged
    # The address is the thing worth logging, and it is there.
    assert _MAILBOX in logged


def _now_after(*, seconds: int) -> dt.datetime:
    """A moment ``seconds`` after the exchange in these tests ran."""
    return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)


# --- The panel and the routes ---------------------------------------------------


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def web(factory):
    app = create_app()

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


@pytest.fixture
def google(monkeypatch):
    """Replace the module's single network boundary, so the routes run for real.

    The routes call ``gmail_link.exchange`` with no ``fetch`` of their own — a
    request handler has no business knowing how Google is reached — and this is
    what it resolves to instead.
    """
    stub = _google()
    monkeypatch.setattr(gmail_link, "_fetch", stub)
    return stub


def _connect(web: TestClient, code: str = "auth-code") -> Any:
    """Walk the real flow: start, take the state cookie, come back with a code."""
    started = web.get("/settings/gmail/start", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    return web.get(
        f"/settings/gmail/callback?code={code}&state={state}", follow_redirects=False
    )


def test_the_panel_says_it_is_not_set_up_and_offers_no_connect_button(
    unconfigured, web
):
    body = web.get("/settings").text
    assert "Nicht eingerichtet" in body
    # It never renders a button that would fail at Google with invalid_client.
    assert "/settings/gmail/start" not in body
    # And it points at the note that says how to set it up.
    assert "docs/deployment.md" in body


def test_starting_the_flow_redirects_to_google_with_a_state(volume, web):
    response = web.get("/settings/gmail/start", follow_redirects=False)

    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert target.netloc == "accounts.google.com"
    query = parse_qs(target.query)
    assert query["scope"][0].split() == list(gmail_link.SCOPES)
    # The state travels in the URL *and* in a cookie, and the callback compares.
    assert query["state"][0] == web.cookies.get("newspulse_gmail_state")


def test_a_callback_with_the_wrong_state_connects_nothing(volume, web, google):
    web.get("/settings/gmail/start", follow_redirects=False)

    response = web.get(
        "/settings/gmail/callback?code=auth-code&state=not-the-one",
        follow_redirects=False,
    )

    assert GmailNotice.STATE.value in response.headers["location"]
    assert not gmail_link.token_path().exists()
    assert google.calls == []


def test_a_callback_with_no_state_at_all_connects_nothing(volume, web, google):
    response = web.get(
        "/settings/gmail/callback?code=auth-code", follow_redirects=False
    )

    assert GmailNotice.STATE.value in response.headers["location"]
    assert not gmail_link.token_path().exists()


def test_a_hand_crafted_state_is_refused_rather_than_crashing(volume, web, google):
    """The value comes off the query string, so it can be anything at all —
    including the non-ASCII that a constant-time compare refuses to take."""
    web.get("/settings/gmail/start", follow_redirects=False)

    response = web.get(
        "/settings/gmail/callback?code=auth-code&state=übergabe", follow_redirects=False
    )

    assert response.status_code == 303
    assert GmailNotice.STATE.value in response.headers["location"]
    assert not gmail_link.token_path().exists()


def test_a_completed_callback_shows_the_address_and_the_granted_scopes(
    volume, web, google
):
    _connect(web)

    body = web.get("/settings").text
    assert _MAILBOX in body
    # The permissions in words, not as scope URLs.
    assert "Nachrichten lesen" in body
    assert "Nachrichten senden" in body
    assert "gmail.readonly" not in body
    assert "Postfach trennen" in body


def test_no_token_value_reaches_the_database(volume, web, google, factory):
    """The refresh token lives in a file for exactly this reason: neither a
    database copy nor the Excel export may carry it off the machine."""
    _connect(web)

    with factory() as session:
        for table in Base.metadata.sorted_tables:
            for row in session.execute(select(table)).all():
                assert _REFRESH not in str(tuple(row)), table.name
                assert _ACCESS not in str(tuple(row)), table.name


def test_declining_consent_returns_a_plain_german_line_and_stores_nothing(
    volume, web, google
):
    started = web.get("/settings/gmail/start", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    response = web.get(
        f"/settings/gmail/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )

    assert GmailNotice.DECLINED.value in response.headers["location"]
    assert not gmail_link.token_path().exists()
    body = web.get("/settings?gmail=abgelehnt").text
    assert "Die Freigabe wurde bei Google abgebrochen." in body


def test_the_state_cookie_is_spent_after_one_callback(volume, web, google):
    """One attempt, one state — a replayed callback finds nothing to match."""
    _connect(web)
    assert not web.cookies.get("newspulse_gmail_state")


def test_a_failed_exchange_lands_on_the_panel_rather_than_a_stack_trace(
    volume, web, monkeypatch
):
    monkeypatch.setattr(
        gmail_link, "_fetch", FakeGoogle(token_replies=[{"error": "invalid_client"}])
    )
    response = _connect(web)

    assert GmailNotice.FAILED.value in response.headers["location"]
    assert not gmail_link.token_path().exists()


def test_disconnect_revokes_and_leaves_every_stored_letter_untouched(
    volume, web, google, factory
):
    released = _a_released_letter(factory)
    _connect(web)

    response = web.post("/settings/gmail/disconnect", follow_redirects=False)

    assert GmailNotice.DISCONNECTED.value in response.headers["location"]
    assert any(call.url.endswith("/revoke") for call in google.calls)
    assert not gmail_link.token_path().exists()
    with factory() as session:
        letter = session.get(Outreach, released)
        assert letter is not None
        assert letter.state == OutreachState.RAUS
        assert letter.message == "Sehr geehrte Frau Kühn, …"
        assert session.scalar(select(Contact).limit(1)) is not None


def test_a_live_connection_stays_visible_without_a_client_id(
    volume, web, google, monkeypatch
):
    """Clearing or rotating NEWSPULSE_GMAIL_CLIENT_ID revokes nothing: the refresh
    token is still on the volume, and it still reads and sends. A panel that
    answered "nicht eingerichtet" to that hid the connected address and the only
    button that can hand the access back, leaving revocation to an SSH session.
    """
    _connect(web)
    monkeypatch.delenv("NEWSPULSE_GMAIL_CLIENT_ID", raising=False)
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "")

    body = web.get("/settings").text
    assert _MAILBOX in body
    assert "/settings/gmail/disconnect" in body


def test_a_refresh_does_not_resurrect_a_disconnected_credential(volume, web, google):
    """A refresh is a network call, and a disconnect can land inside it. Writing
    the whole record back afterwards put the revoked refresh token on disk again —
    so "trennen" no longer deleted the file, and the panel named a mailbox whose
    access had already ended.
    """
    _connect(web)
    google.token_replies = [{"access_token": "ya29.renewed", "expires_in": 3599}]
    reached_google = google.__call__

    def racy(url: str, *, form: Any = None, token: str = "") -> dict[str, Any]:
        """Somebody presses "Postfach trennen" while the refresh is in flight."""
        gmail_link.disconnect(fetch=lambda *a, **k: {})
        return reached_google(url, form=form, token=token)

    gmail_link.token(fetch=racy, now=_now_after(seconds=7200))

    assert not gmail_link.token_path().exists()
    assert gmail_link.connected() is None


def test_a_cross_site_disconnect_is_refused(volume, web, google):
    """The app authenticates with HTTP Basic, so a browser attaches the operator's
    credentials to a form POST from any site — and this POST revokes access at a
    third party. A post whose Origin names another host changes nothing.
    """
    _connect(web)

    response = web.post(
        "/settings/gmail/disconnect",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert gmail_link.token_path().exists()
    assert not any(call.url.endswith("/revoke") for call in google.calls)


def test_the_panel_explains_a_connection_google_ended(volume, web, google):
    """Back to disconnected *with the reason*, rather than a silently empty
    panel that reads as though nobody ever connected anything."""
    _connect(web)
    google.token_replies = [
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    ]
    gmail_link.token(now=_now_after(seconds=7200))

    body = web.get("/settings").text
    assert "Der Zugriff wurde bei Google entzogen" in body
    assert "Postfach verbinden" in body
    assert _MAILBOX not in body


def _a_released_letter(factory) -> int:
    """One released letter with its recipient in the contact book."""
    with factory() as session:
        client = Client(name="Nordlicht Energie")
        session.add(client)
        session.flush()
        angle = Angle(
            client_id=client.id,
            generated_at=_NOW,
            subject="Netzentgelte",
            message="Die Anschlussdauer ist der Kostentreiber.",
            context="Sechs Meldungen.",
            thesis="Die Debatte greift zu kurz.",
            overclaim="Netzentgelte sind egal.",
            statements=["Anschlussdauer ist Infrastruktur."],
        )
        contact = Contact(name="Marlene Kühn", outlet="Handelsblatt")
        session.add_all([angle, contact])
        session.flush()
        letter = Outreach(
            client_id=client.id,
            angle_id=angle.id,
            generated_at=_NOW,
            journalist="Marlene Kühn",
            outlet="Handelsblatt",
            subject="Anschlussdauer statt Netzentgelt",
            message="Sehr geehrte Frau Kühn, …",
            contact_id=contact.id,
            state=OutreachState.RAUS,
            released_at=_NOW,
            released_by="mensch",
        )
        session.add(letter)
        session.commit()
        return letter.id
