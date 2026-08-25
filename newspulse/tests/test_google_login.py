"""Sign in with Google, and the properties that make it worth the change.

Most of these are about what must *not* work. A login is one of the few places
where the interesting cases are all failures, so they get the coverage: a forged
cookie, an expired one, one belonging to somebody taken off the list, a callback
that never started here, an ID token minted for another application, and the old
shared password after it has been superseded.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from newspulse import config
from newspulse.web import auth, google_auth
from newspulse.web.app import create_app

_CLIENT_ID = "rauteos.apps.googleusercontent.com"
_ALLOWED = "raphaelmankopf@gmail.com"
_STRANGER = "wer.anders@gmail.com"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """Google sign-in switched on, with the session key on a temp disk."""
    monkeypatch.setenv("NEWSPULSE_GOOGLE_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("NEWSPULSE_GOOGLE_CLIENT_SECRET", "shh")
    monkeypatch.setenv("NEWSPULSE_ALLOWED_EMAILS", f"{_ALLOWED},lucas.neurauter@gmail.com")
    monkeypatch.setenv("NEWSPULSE_BASE_URL", "https://rauteos.test")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "np.db")
    monkeypatch.delenv("NEWSPULSE_SESSION_SECRET", raising=False)
    return tmp_path


@pytest.fixture
def web(configured):
    return TestClient(create_app(), follow_redirects=False)


def _id_token(**claims) -> str:
    """A JWT shaped like Google's, with a signature nothing here checks.

    Legitimate in the test for the same reason it is legitimate in production:
    the payload is only ever read from the body of a direct call to Google's
    token endpoint, so its provenance comes from TLS rather than the signature.
    """
    payload = {
        "iss": "https://accounts.google.com",
        "aud": _CLIENT_ID,
        "exp": int(time.time()) + 600,
        "email": _ALLOWED,
        "email_verified": True,
        "sub": "12345",
    }
    payload.update(claims)
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{raw}.signature"


def _google_returns(monkeypatch, token: str) -> None:
    monkeypatch.setattr(
        google_auth, "_post_form", lambda *a, **k: {"id_token": token}
    )


def _signed_in(web) -> TestClient:
    """A client holding a valid session for the allowed address."""
    web.cookies.set(
        google_auth.SESSION_COOKIE,
        google_auth.issue_session(google_auth.Identity(_ALLOWED, "12345")),
    )
    return web


# --- The guard ----------------------------------------------------------------


def test_an_anonymous_browser_is_sent_to_the_sign_in_page(web):
    response = web.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 303
    # The destination is carried along, so a bookmarked deep link survives.
    assert response.headers["location"] == "/login?next=%2F"


def test_a_signed_in_browser_is_let_through(web):
    assert _signed_in(web).get("/login").status_code == 303


def test_the_old_password_stops_working_once_google_is_configured(web, monkeypatch):
    """The point of the migration. Leaving basic auth accepted "just in case"
    would mean the allow-list is bypassable by anyone who still has the shared
    password, which is most of what we are moving away from."""
    monkeypatch.setattr(config, "AUTH_USER", "Lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "CaptainComms")
    response = web.get("/", auth=("Lucas", "CaptainComms"), headers={"Accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_stylesheet_and_the_sign_in_page_need_no_session(web):
    """Otherwise the login page renders unstyled, or not at all."""
    assert web.get("/static/app.css").status_code == 200
    assert web.get("/login").status_code == 200


def test_an_htmx_poll_gets_a_status_rather_than_a_login_page(web):
    """The run-status header polls itself. Swapping a sign-in form into that
    element would paint a login page inside the dashboard chrome."""
    response = web.get("/partials/runmeta", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


# --- The session cookie -------------------------------------------------------


def test_a_tampered_cookie_is_refused(web, configured):
    good = google_auth.issue_session(google_auth.Identity(_ALLOWED, "12345"))
    body, _, signature = good.partition(".")
    forged = json.dumps({"email": _STRANGER, "sub": "x", "exp": int(time.time()) + 600})
    swapped = base64.urlsafe_b64encode(forged.encode()).decode().rstrip("=")
    assert google_auth.read_session(f"{swapped}.{signature}") is None


def test_an_expired_session_is_refused(configured):
    stale = google_auth.issue_session(
        google_auth.Identity(_ALLOWED, "12345"), now=time.time() - google_auth.SESSION_DAYS * 86400 - 10
    )
    assert google_auth.read_session(stale) is None


def test_removing_an_address_ends_that_session_at_the_next_request(configured, monkeypatch):
    """The reason the allow-list is re-read rather than trusted from the cookie:
    revoking access has to take effect now, not in thirty days."""
    token = google_auth.issue_session(google_auth.Identity(_ALLOWED, "12345"))
    assert google_auth.read_session(token) == _ALLOWED
    monkeypatch.setenv("NEWSPULSE_ALLOWED_EMAILS", "lucas.neurauter@gmail.com")
    assert google_auth.read_session(token) is None


def test_the_session_key_survives_a_restart(configured):
    """A key regenerated per boot signs everyone out on every deploy, which is
    indistinguishable from a broken login."""
    first = google_auth.session_secret()
    assert google_auth.session_secret() == first
    assert google_auth.secret_path().exists()
    assert google_auth.secret_path().stat().st_mode & 0o777 == 0o600


# --- The callback -------------------------------------------------------------


def test_a_callback_without_a_matching_state_is_refused(web):
    """Without this, anyone who can make the browser fetch a URL can complete a
    sign-in with a code of their choosing."""
    response = web.get("/auth/google/callback?code=abc&state=made-up")
    assert response.status_code == 303
    assert "abgelaufen" in response.headers["location"]


def test_an_allowed_address_is_signed_in(web, monkeypatch):
    start = web.get("/auth/google/start?next=%2Ftoday")
    state = web.cookies[google_auth.STATE_COOKIE]
    assert "accounts.google.com" in start.headers["location"]

    _google_returns(monkeypatch, _id_token())
    response = web.get(f"/auth/google/callback?code=abc&state={state}")

    assert response.status_code == 303
    assert response.headers["location"] == "/today"
    assert google_auth.read_session(web.cookies[google_auth.SESSION_COOKIE]) == _ALLOWED


def test_an_address_that_is_not_on_the_list_is_named_as_such(web, monkeypatch):
    """Its own message: "wrong password" for an account that was never on the
    list sends the person to reset a credential that is not the problem."""
    web.get("/auth/google/start")
    state = web.cookies[google_auth.STATE_COOKIE]
    _google_returns(monkeypatch, _id_token(email=_STRANGER))

    response = web.get(f"/auth/google/callback?code=abc&state={state}")

    assert "nicht_freigegeben" in response.headers["location"]
    assert google_auth.SESSION_COOKIE not in web.cookies


@pytest.mark.parametrize(
    "claims, why",
    [
        ({"aud": "someone-elses.apps.googleusercontent.com"}, "issued for another app"),
        ({"iss": "https://evil.example"}, "not from Google"),
        ({"email_verified": False}, "address never proven"),
        ({"exp": int(time.time()) - 10}, "already expired"),
        ({"email": ""}, "no address at all"),
    ],
)
def test_an_id_token_that_fails_its_claims_is_rejected(configured, claims, why):
    with pytest.raises(google_auth.SignInError):
        google_auth._verify_claims(
            json.loads(base64.urlsafe_b64decode(
                _id_token(**claims).split(".")[1] + "=="
            ))
        ), why


# --- Open redirect ------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["https://evil.example/steal", "//evil.example", "http://evil.example", "javascript:alert(1)"],
)
def test_the_next_parameter_cannot_leave_the_site(hostile):
    """A login page that reflects an absolute URL is an open redirect, and an
    open redirect on the domain the two of them trust is worth real money to
    whoever is phishing them."""
    from newspulse.web.redirects import local_target

    assert local_target(hostile) == "/"


def test_a_relative_path_is_kept(configured):
    from newspulse.web.redirects import local_target

    assert local_target("/client/3/advice?tab=x") == "/client/3/advice?tab=x"


# --- Booting ------------------------------------------------------------------


def test_google_alone_satisfies_the_public_bind_guard(configured, monkeypatch):
    """Otherwise moving off basic auth would make the server refuse to start."""
    monkeypatch.setattr(config, "AUTH_USER", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    auth.require_auth_for_public_bind("0.0.0.0")  # must not raise


def test_neither_mechanism_still_refuses_to_boot_publicly(monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    monkeypatch.setenv("NEWSPULSE_GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("NEWSPULSE_GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_ID", "")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "")
    monkeypatch.setattr(config, "GMAIL_CLIENT_SECRET", "")
    with pytest.raises(SystemExit):
        auth.require_auth_for_public_bind("0.0.0.0")


# --- One mailbox, several spellings -------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "raphaelmankopf@gmail.com",
        "raphael.mankopf@gmail.com",      # Gmail ignores dots
        "RaphaelMankopf@Gmail.com",       # and case
        "raphaelmankopf+rauteos@gmail.com",  # and anything after a plus
        "raphaelmankopf@googlemail.com",  # the other domain for the same inbox
    ],
)
def test_every_spelling_of_one_gmail_address_is_the_same_person(configured, spelling):
    """Google returns whichever spelling the account was registered with, which
    is not necessarily the one somebody typed into the allow-list. An exact
    string comparison would lock out a person whose address is, to Google,
    identical to the one on the list."""
    assert google_auth.is_allowed(spelling)


@pytest.mark.parametrize(
    "other",
    [
        "raphael.mankopf@arrakis.finance",  # dots may separate real mailboxes
        "raphaelmankopf@example.com",
        "raphaelmankopf@gmail.com.evil.test",
        "",
        "   ",
    ],
)
def test_folding_stops_at_gmail(configured, other):
    """Outside Gmail a dot or a plus can genuinely separate two mailboxes, so
    folding them would let one address stand in for another — the allow-list
    would then admit people it was never given."""
    assert not google_auth.is_allowed(other)


def test_the_sign_in_page_does_not_publish_who_may_sign_in(web, configured):
    """/login is served to anyone who finds the URL.

    It used to print both allowed addresses under the button, which handed a
    stranger the exact two accounts whose compromise opens the whole client
    portfolio: a target list, published by the page whose job is to keep them
    out. Somebody who belongs here already knows which of their addresses to
    pick; somebody who does not is told after Google, by name, in the error.
    """
    body = web.get("/login").text

    assert _ALLOWED not in body
    assert "lucas.neurauter@gmail.com" not in body
    # And the page still says there is a list, so a refusal later is not a
    # surprise about a rule nobody mentioned.
    assert "freigegebene Konten" in body


def test_an_empty_secret_file_is_taken_over_rather_than_crashing(configured):
    """A key file that exists and is empty used to raise FileExistsError out of
    every request: the O_EXCL open sat outside the handler written for it."""
    path = google_auth.secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"   \n")

    first = google_auth.session_secret()

    assert first.strip()
    assert google_auth.session_secret() == first, "and it stays put afterwards"
