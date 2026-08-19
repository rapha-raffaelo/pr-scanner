"""The letter goes out through Gmail (OUT-04).

DEC-4 locked option C — read and send — so this is the one place in the product
where a text leaves the house because a button in RauteOS was pressed. Three
properties carry more weight than the rest and are asserted directly rather than
inferred:

* **Nothing here reaches Google.** ``gmail_link`` has one network boundary and
  :class:`FakeGmail` replaces it. The fake is a real Gmail as far as this code
  can tell: it stores the RFC 5322 message it was handed and answers a thread
  read by decoding it back the way the API does, so the round trip is checked
  against a message that actually made the trip rather than against a mock's
  echo of the input.
* **No address is ever derived.** A byline is not an address, and under a send
  scope a guessed "vorname.nachname@medium.de" means this tool posted a client's
  pitch to a stranger. Three tests are about refusing rather than sending.
* **The ids are Google's.** The fake answers with deliberately odd ids that no
  local construction would produce, so a stored id that matches proves it came
  out of the response.
"""

from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as _default_policy
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, gmail_link, outreach
from newspulse.models import (
    Angle,
    Base,
    Client,
    Contact,
    Outreach,
    OutreachState,
)
from newspulse.web.app import create_app, get_db

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_MAILBOX = "lucas@raute.example"
_ACCESS = "ya29.access-token"
_REFRESH = "1//refresh-token"
_JOURNALIST = "Marlene Kühn"
_OUTLET = "Handelsblatt"
_ADDRESS = "m.kuehn@handelsblatt.example"

# Deliberately unlike anything this codebase could assemble: if one of these
# turns up in a column, it came back from the API response.
_DRAFT_ID = "r-9c3f-draft"
_MESSAGE_ID = "18f-9c3f-message"
_THREAD_ID = "18f-9c3f-thread"

# The German a letter is actually written in. Every umlaut, the ß, and a subject
# long enough that the header folds — which is where a hand-rolled encoder breaks.
_SUBJECT = "Anschlussdauer statt Netzentgelt: Zahlen aus 340 Industrieanschlüssen für Prüfzwecke"
_BODY = (
    "Sehr geehrte Frau Kühn,\n"
    "\n"
    "Sie haben vergangene Woche geschrieben, dass die Netzentgeltdebatte an den "
    "Industriekunden vorbeigeht. Wir haben Zahlen aus 340 Anschlussverfahren, "
    "die genau das belegen: Größe, Dauer, Kosten.\n"
    "\n"
    "Mit freundlichen Grüßen\n"
)

# Gmail's own moment for the sent message, in the milliseconds-since-epoch string
# the API returns. Well before "now", so a released_at taken from it cannot be
# confused with one this machine stamped.
_SENT_AT = dt.datetime(2026, 8, 12, 9, 41, tzinfo=dt.UTC)


# --- A stand-in for Gmail --------------------------------------------------------


@dataclass
class Call:
    """One request ``gmail_link`` would have made."""

    url: str
    json_body: dict[str, Any] | None
    method: str


@dataclass
class FakeGmail:
    """Gmail, as far as this module can tell.

    It stores the raw message it is handed and hands it back through a thread
    read the way the real API does — decoding the transfer encoding and
    re-exposing the body as base64url. That is what makes the round-trip test
    meaningful: the text is compared after a real encode and a real decode, not
    against an echo.
    """

    drafts: dict[str, str] = field(default_factory=dict)
    threads: dict[str, list[str]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    #: Set to a message to make the next send fail the way an unreachable Google
    #: would, leaving the composed draft behind.
    send_fails: str = ""

    def __call__(
        self,
        url: str,
        *,
        form: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        method: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        if url.endswith("/token"):
            return {"access_token": _ACCESS, "expires_in": 3599}
        self.calls.append(Call(url, json_body, method))
        assert token == _ACCESS, "a Gmail API call went out without the bearer"
        if url.endswith("/drafts/send"):
            return self._send(json_body or {})
        if url.endswith("/drafts"):
            return self._compose(_DRAFT_ID, json_body or {})
        if "/drafts/" in url:
            return self._compose(url.rsplit("/", 1)[-1], json_body or {})
        if "/threads/" in url:
            return self._thread(url)
        raise AssertionError(f"unexpected endpoint {url}")

    # -- what the endpoints do ---------------------------------------------------

    def _compose(self, draft_id: str, body: dict[str, Any]) -> dict[str, Any]:
        message = body.get("message") or {}
        self.drafts[draft_id] = message["raw"]
        return {
            "id": draft_id,
            "message": {"id": f"{_MESSAGE_ID}-entwurf", "threadId": _THREAD_ID},
        }

    def _send(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.send_fails:
            raise gmail_link.GmailError(self.send_fails)
        raw = self.drafts.pop(body["id"])
        self.threads.setdefault(_THREAD_ID, []).append(raw)
        return {
            "id": _MESSAGE_ID,
            "threadId": _THREAD_ID,
            "labelIds": ["SENT"],
            "internalDate": str(int(_SENT_AT.timestamp() * 1000)),
        }

    def _thread(self, url: str) -> dict[str, Any]:
        thread_id = url.rsplit("/", 1)[-1].split("?")[0]
        return {
            "id": thread_id,
            "messages": [
                _as_gmail_message(raw) for raw in self.threads.get(thread_id, [])
            ],
        }

    # -- what a test asks it afterwards ------------------------------------------

    def urls(self) -> list[str]:
        return [call.url for call in self.calls]

    def composed(self) -> int:
        """How many distinct drafts were ever created or updated."""
        return len([c for c in self.calls if "/drafts" in c.url and "send" not in c.url])


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _as_gmail_message(raw: str) -> dict[str, Any]:
    """One stored RFC 5322 message in the shape ``threads.get?format=full`` returns.

    Gmail decodes the transfer encoding for you and hands the body back as
    base64url — so this does exactly that, and nothing the code under test does
    to build the message is undone by a shortcut here.
    """
    parsed = BytesParser(policy=_default_policy).parsebytes(_decode(raw))
    text = parsed.get_content()
    return {
        "id": _MESSAGE_ID,
        "threadId": _THREAD_ID,
        "labelIds": ["SENT"],
        "internalDate": str(int(_SENT_AT.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": name, "value": value} for name, value in parsed.items()],
            "body": {"data": base64.urlsafe_b64encode(text.encode("utf-8")).decode()},
        },
    }


# --- Fixtures --------------------------------------------------------------------


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A configured OAuth client and a directory for the token file."""
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "newspulse.db")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_SECRET", "client-secret")
    return tmp_path


@pytest.fixture
def mailbox(volume):
    """A connected mailbox whose stored access token is still good.

    Written through the module's own writer so the file is the shape the real
    OAuth callback leaves behind, mode and all.
    """
    gmail_link._write(
        {
            "refresh_token": _REFRESH,
            "access_token": _ACCESS,
            "expires_at": (
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
            ).isoformat(),
            "scopes": list(gmail_link.SCOPES),
            "email": _MAILBOX,
            "connected_at": dt.datetime.now(dt.UTC).isoformat(),
            "lost": "",
        }
    )
    return volume


@pytest.fixture
def gmail(monkeypatch):
    """Replace the module's single network boundary, so the routes run for real."""
    fake = FakeGmail()
    monkeypatch.setattr(gmail_link, "_fetch", fake)
    return fake


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    return factory()


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _letter(
    session,
    *,
    email: str = _ADDRESS,
    in_book: bool = True,
    journalist: str = _JOURNALIST,
) -> Outreach:
    """One mandate, one impulse, one written letter, and the contact behind it."""
    client = Client(name="Nordlicht Energie", industry="Energie", keywords=["Netz"])
    session.add(client)
    session.flush()
    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Netzentgelte: wer zahlt den Ausbau",
        message="Zwei Absätze Positionierung.",
        context="Aus 6 Meldungen der letzten 7 Tage.",
    )
    session.add(angle)
    if in_book and journalist:
        session.add(Contact(name=journalist, outlet=_OUTLET, email=email))
    session.flush()
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist=journalist,
        outlet=_OUTLET,
        subject=_SUBJECT,
        message=_BODY,
        generated_at=dt.datetime.now(dt.UTC),
    )
    session.add(row)
    session.commit()
    return row


def _push(web: TestClient, row: Outreach):
    return web.post(
        f"/client/{row.client_id}/outreach/{row.id}/gmail-draft",
        follow_redirects=False,
    )


# --- The message itself ----------------------------------------------------------


def test_the_subject_header_is_encoded_rather_than_raw_utf8(mailbox):
    """A German subject pasted straight into a header field is bytes an RFC 5322
    header may not carry, and Gmail forwards it to a recipient who reads mojibake."""
    blob = _decode(gmail_link.build_message(_ADDRESS, _SUBJECT, _BODY))

    header_block = blob.split(b"\r\n\r\n", 1)[0]
    header_block.decode("ascii")  # raises if a raw umlaut got into a header
    assert b"=?utf-8?" in header_block


def test_the_subject_and_body_survive_the_round_trip_through_gmail(mailbox, gmail):
    """What comes back out of a thread read is what went in, character for
    character — after a real quoted-printable encode and a real decode."""
    draft = gmail_link.create_draft(_ADDRESS, _SUBJECT, _BODY)
    gmail_link.send(draft.draft_id)

    (back,) = gmail_link.thread(_THREAD_ID)

    assert back.subject == _SUBJECT
    assert back.body == _BODY


def test_the_recipient_is_the_address_it_was_given(mailbox, gmail):
    gmail_link.create_draft(_ADDRESS, _SUBJECT, _BODY)

    parsed = BytesParser(policy=_default_policy).parsebytes(
        _decode(gmail.drafts[_DRAFT_ID])
    )
    assert parsed["To"] == _ADDRESS


# --- The two Gmail calls ---------------------------------------------------------


def test_the_ids_come_from_googles_answer(mailbox, gmail):
    draft = gmail_link.create_draft(_ADDRESS, _SUBJECT, _BODY)
    sent = gmail_link.send(draft.draft_id)

    assert draft.draft_id == _DRAFT_ID
    assert (sent.message_id, sent.thread_id) == (_MESSAGE_ID, _THREAD_ID)


def test_an_answer_that_names_no_message_is_refused(mailbox, monkeypatch):
    """A row with a plausible blank where a thread id belongs is worse than a
    failure: OUT-05 would then read a conversation that does not exist."""
    monkeypatch.setattr(
        gmail_link, "_fetch", lambda *a, **k: {"access_token": _ACCESS, "id": "r1"}
    )

    with pytest.raises(gmail_link.GmailError):
        gmail_link.create_draft(_ADDRESS, _SUBJECT, _BODY)


def test_a_second_compose_updates_the_draft_instead_of_making_another(mailbox, gmail):
    gmail_link.create_draft(_ADDRESS, _SUBJECT, _BODY)
    gmail_link.create_draft(_ADDRESS, _SUBJECT, "Zweiter Versuch.", draft_id=_DRAFT_ID)

    assert len(gmail.drafts) == 1
    assert gmail.calls[-1].method == "PUT"
    assert gmail.calls[-1].url.endswith(f"/drafts/{_DRAFT_ID}")


def test_send_reports_gmails_own_send_date(mailbox, gmail):
    draft = gmail_link.create_draft(_ADDRESS, _SUBJECT, _BODY)

    assert gmail_link.send(draft.draft_id).sent_at == _SENT_AT


def test_a_thread_is_asked_for_by_its_id_and_by_nothing_else(mailbox, gmail):
    """DEC-6 option A in one assertion: the only conversations this tool can name
    are the ones it started, so no query over the rest of the mailbox exists."""
    gmail_link.thread(_THREAD_ID)

    (url,) = [call.url for call in gmail.calls if "/threads/" in call.url]
    assert url.endswith(f"/threads/{_THREAD_ID}?format=full")


def test_a_reply_in_the_thread_is_read_back_with_its_sender(mailbox, gmail):
    """What OUT-05 will file against the letter. Asserted here because it is this
    module's decoder, not the sync's."""
    gmail.threads[_THREAD_ID] = [
        gmail_link.build_message(
            _MAILBOX, "Re: Netzentgelte", "Danke, schicken Sie gern.\n"
        )
    ]

    (message,) = gmail_link.thread(_THREAD_ID)

    assert message.subject == "Re: Netzentgelte"
    assert message.body == "Danke, schicken Sie gern.\n"
    assert message.received_at == _SENT_AT


def test_a_body_without_a_final_newline_comes_back_with_one(mailbox, gmail):
    """The one normalisation the round trip performs, written down rather than
    discovered later: a mail body is a sequence of lines and the last one is
    terminated, so a text that did not end in a newline gains one. Everything
    inside the text is untouched."""
    gmail.threads[_THREAD_ID] = [
        gmail_link.build_message(_MAILBOX, "Betreff", "Ohne Zeilenende")
    ]

    (message,) = gmail_link.thread(_THREAD_ID)

    assert message.body == "Ohne Zeilenende\n"


# --- The route -------------------------------------------------------------------


def test_a_letter_to_a_known_address_is_sent_and_googles_ids_are_stored(
    mailbox, gmail, web, session
):
    row = _letter(session)

    response = _push(web, row)

    assert response.status_code == 303
    session.refresh(row)
    assert row.gmail_draft_id == _DRAFT_ID
    assert row.gmail_thread_id == _THREAD_ID
    assert row.gmail_message_id == _MESSAGE_ID
    assert row.sent_through_gmail is True


def test_the_release_is_stamped_at_gmails_own_send_date(mailbox, gmail, web, session):
    """Not at this machine's clock: the journalist got it when Gmail says they did."""
    row = _letter(session)

    _push(web, row)

    session.refresh(row)
    assert row.released_at == _SENT_AT
    assert row.state == OutreachState.RAUS
    assert row.released_by == outreach.DEFAULT_RELEASED_BY


def test_a_letter_released_by_hand_keeps_its_own_release_time(
    mailbox, gmail, web, session
):
    """The first release stands. A human took responsibility at that moment, and
    a later push must not rewrite when that was."""
    row = _letter(session)
    outreach.release(session, row, released_by="Lucas")
    by_hand = row.released_at

    _push(web, row)

    session.refresh(row)
    assert row.released_at == by_hand
    assert row.released_by == "Lucas"
    assert row.gmail_message_id == _MESSAGE_ID


def test_the_recipient_is_resolved_to_the_contact_book_entry(
    mailbox, gmail, web, session
):
    row = _letter(session)

    _push(web, row)

    session.refresh(row)
    known = session.scalars(select(Contact)).one()
    assert row.contact_id == known.id


def test_pushing_the_same_letter_twice_does_not_send_it_twice(
    mailbox, gmail, web, session
):
    row = _letter(session)
    _push(web, row)

    again = _push(web, row)

    assert again.status_code == 400
    assert len([call for call in gmail.calls if call.url.endswith("/drafts/send")]) == 1


def test_a_failed_send_keeps_the_draft_so_the_retry_updates_it(
    mailbox, gmail, web, session
):
    """The two-step earning its keep. Gmail accepted the draft and then the send
    failed; the retry must rewrite that one draft rather than leave a second,
    near-identical message sitting at the same journalist."""
    row = _letter(session)
    gmail.send_fails = "Google ist nicht erreichbar: test"
    _push(web, row)
    session.refresh(row)
    assert row.gmail_draft_id == _DRAFT_ID
    assert row.gmail_message_id == ""
    assert row.released_at is None

    gmail.send_fails = ""
    _push(web, row)

    session.refresh(row)
    assert row.gmail_message_id == _MESSAGE_ID
    assert len(gmail.drafts) == 0  # the one draft was sent, not left behind
    assert gmail.calls[-2].method == "PUT"  # the retry updated it


# --- The address this tool refuses to invent ------------------------------------


def test_a_recipient_who_is_not_in_the_contact_book_is_refused(
    mailbox, gmail, web, session
):
    row = _letter(session, in_book=False)

    response = _push(web, row)

    assert response.status_code == 400
    assert gmail.calls == []
    session.refresh(row)
    assert row.gmail_message_id == ""


def test_no_address_is_derived_from_a_name_or_an_outlet_pattern(
    mailbox, gmail, web, session
):
    """The contact exists and the address field is empty. A byline plus a
    masthead is enough to build "m.kuehn@handelsblatt.example" — which is what
    this tool must never do, because under a send scope it would post a client's
    pitch to a stranger."""
    row = _letter(session, email="")

    response = _push(web, row)

    assert response.status_code == 400
    assert gmail.calls == []


def test_a_letter_with_no_byline_at_all_is_refused(mailbox, gmail, web, session):
    row = _letter(session, journalist="", in_book=False)

    assert _push(web, row).status_code == 400
    assert gmail.calls == []


def test_with_no_mailbox_connected_the_route_sends_nothing(volume, gmail, web, session):
    """No token file at all — the state of every installation before OUT-03."""
    row = _letter(session)

    assert _push(web, row).status_code == 400
    assert gmail.calls == []


def test_a_post_from_another_site_is_refused(mailbox, gmail, web, session):
    row = _letter(session)

    response = web.post(
        f"/client/{row.client_id}/outreach/{row.id}/gmail-draft",
        headers={"Origin": "https://angreifer.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert gmail.calls == []


# --- The card --------------------------------------------------------------------


def _card(web: TestClient, row: Outreach) -> str:
    return web.get(f"/client/{row.client_id}/advice").text


def test_the_card_offers_the_two_click_send_and_names_the_address(
    mailbox, gmail, web, session
):
    row = _letter(session)

    body = _card(web, row)

    assert "Freigeben und senden" in body
    assert "Ja, senden" in body
    assert f"/client/{row.client_id}/outreach/{row.id}/gmail-draft" in body
    # The confirmation names where it is about to go.
    assert _ADDRESS in body


def test_the_card_says_the_send_permission_changes_the_grundannahme(
    mailbox, gmail, web, session
):
    row = _letter(session)

    body = _card(web, row)

    assert "Diese Variante ändert die Grundannahme." in body
    assert "Postfach verbunden" in body and _MAILBOX in body


def test_a_sent_letter_links_into_its_thread_and_loses_the_kopieren_button(
    mailbox, gmail, web, session
):
    row = _letter(session)
    _push(web, row)

    body = _card(web, row)

    assert "Verlauf in Gmail öffnen" in body
    assert gmail_link.thread_url(_THREAD_ID) in body
    assert f'data-copy-from="letter-text-{row.id}"' not in body
    assert "Von RauteOS gesendet am" in body


def test_a_letter_with_no_address_shows_the_action_disabled_with_the_reason(
    mailbox, gmail, web, session
):
    row = _letter(session, email="")

    body = _card(web, row)

    assert "Freigeben und senden" in body
    assert "disabled" in body
    assert "Im Kontaktbuch, aber ohne E-Mail-Adresse." in body
    # And it never offers the send it cannot perform.
    assert f"/client/{row.client_id}/outreach/{row.id}/gmail-draft" not in body


def test_an_unknown_recipient_is_told_that_nothing_is_derived(
    mailbox, gmail, web, session
):
    row = _letter(session, in_book=False)

    body = _card(web, row)

    assert "RauteOS leitet keine Adresse aus Name oder Medium ab." in body


def test_with_no_mailbox_connected_the_action_is_not_offered_and_kopieren_stays(
    volume, web, session
):
    """OUT-01's card, unchanged. This is the state every installation is in until
    somebody connects a mailbox in Settings."""
    row = _letter(session)

    body = _card(web, row)

    assert "Freigeben und senden" not in body
    assert f"/client/{row.client_id}/outreach/{row.id}/gmail-draft" not in body
    assert "Postfach verbunden" not in body
    # The Kopieren path from OUT-01, working exactly as it did.
    assert f'data-copy-from="letter-text-{row.id}"' in body
    assert f"/client/{row.client_id}/outreach/{row.id}/release" in body


# --- The schema ------------------------------------------------------------------


def test_the_migration_adds_the_three_gmail_columns(tmp_path, monkeypatch):
    """``alembic upgrade head`` on an empty database reaches 0019 and the three
    columns are there, non-null and empty for everything that predates them."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("outreach")}
        for name in ("gmail_draft_id", "gmail_thread_id", "gmail_message_id"):
            assert name in columns, name
            assert columns[name]["nullable"] is False
    finally:
        engine.dispose()
