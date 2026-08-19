"""Reading the replies, and filing them against the letter (OUT-05).

Four properties carry the weight here, and each is asserted rather than inferred:

* **Only the threads RauteOS started are asked for.** DEC-6 option A. The fake
  mailbox below holds unrelated conversations — a client contract, a newsletter —
  and the test that matters asserts they are never *requested*, not merely never
  stored. What is not fetched cannot land in a mandate's file.
* **The mailbox's own messages are never a reply.** The outgoing letter sits in
  the very thread being read, and filing it as the journalist's answer would put
  RauteOS's own words in the contact's file as somebody else's.
* **Nothing is interpreted.** A reply sets ``antwort`` and never Absage or
  Veröffentlicht, and an outcome a person already recorded survives untouched.
* **A failed read never costs the sweep.** Google unreachable and access revoked
  are both exercised: they log at ERROR, change no stored row, and leave the
  daily run standing. Standing, not silent — the run says ``partial`` rather than
  claiming a clean morning it did not have.

Nothing here reaches Google: :mod:`newspulse.gmail_link` has one network boundary
and :class:`FakeGmail` replaces it, answering with the payloads the real API
returns.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, contacts, gmail_link, job, mailsync, outreach
from newspulse.ingest import FeedItem
from newspulse.models import (
    OUTCOME_BY_MAILBOX,
    Angle,
    Base,
    Client,
    Outreach,
    OutreachReply,
    OutreachState,
    Run,
    RunStatus,
)
from newspulse.web.app import create_app, get_db

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_MAILBOX = "lucas@raute.example"
_ACCESS = "ya29.access-token"
_REFRESH = "1//refresh-token"

_JOURNALIST = "Marlene Kühn"
_OUTLET = "Handelsblatt"
_ADDRESS = "m.kuehn@handelsblatt.example"

# Ids deliberately unlike anything this codebase could assemble: one turning up
# in a column proves it came out of Gmail's answer.
_THREAD_ID = "18f-9c3f-thread"
_OUR_MESSAGE = "18f-9c3f-message"
_REPLY_ID = "18f-a71b-antwort"
_SECOND_REPLY = "18f-c024-nachfrage"

#: A thread this tool never started, sitting in the same mailbox. Nothing may
#: ever ask for it.
_FOREIGN_THREAD = "18e-0000-mandantenvertrag"

_REPLY_TEXT = (
    "Danke, das ist interessant. Schicken Sie mir die Auswertung gern vorab, "
    "ich melde mich Donnerstag für einen Rückruf."
)

#: Fixed moments, so nothing depends on the wall clock. The reply lands two days
#: after the letter and well before "now", so an ``outcome_at`` taken from it
#: cannot be confused with one this machine stamped.
_RELEASED_AT = dt.datetime(2026, 8, 12, 9, 41, tzinfo=dt.UTC)
_REPLY_AT = dt.datetime(2026, 8, 14, 6, 5, tzinfo=dt.UTC)
_LATER_AT = dt.datetime(2026, 8, 16, 11, 30, tzinfo=dt.UTC)
_NOW = dt.datetime(2026, 8, 19, 9, 0, tzinfo=dt.UTC)


# --- A stand-in for Gmail --------------------------------------------------------


def _payload(
    message_id: str,
    *,
    sender: str,
    body: str,
    at: dt.datetime,
    labels: tuple[str, ...] = (),
    subject: str = "Re: Netzentgelte",
    thread_id: str = _THREAD_ID,
    to: str = "",
) -> dict[str, Any]:
    """One message in the shape ``threads.get?format=full`` actually returns.

    ``to`` is carried because Gmail carries it, and because on this tool's *own*
    message it is the record of where the letter actually went — which is what
    tells the journalist's answer apart from a bounce in the same conversation.
    """
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
    ]
    if to:
        headers.append({"name": "To", "value": to})
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "internalDate": str(int(at.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


def _journalist_reply(
    message_id: str = _REPLY_ID, body: str = _REPLY_TEXT, at: dt.datetime = _REPLY_AT
) -> dict[str, Any]:
    return _payload(
        message_id, sender=f"{_JOURNALIST} <{_ADDRESS}>", body=body, at=at
    )


def _our_letter() -> dict[str, Any]:
    """The outgoing letter that opened the conversation, as Gmail hands it back."""
    return _payload(
        _OUR_MESSAGE,
        sender=f"Lucas Raute <{_MAILBOX}>",
        body="Sehr geehrte Frau Kühn, wir haben Zahlen aus 340 Verfahren.\n",
        at=_RELEASED_AT,
        labels=("SENT",),
        subject="Anschlussdauer statt Netzentgelt",
        to=f"{_JOURNALIST} <{_ADDRESS}>",
    )


class FakeGmail:
    """Gmail's thread endpoint, and nothing else it does not need.

    Any call to an endpoint this sync has no business touching — a search over
    the mailbox, a message list — fails the test where it happens rather than
    quietly returning something plausible.
    """

    def __init__(self, threads: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.threads = threads or {}
        self.calls: list[str] = []
        #: Set to a message to make every thread read fail the way an unreachable
        #: Google does.
        self.unreachable = ""
        #: Thread ids that answer with Gmail's "not found", the way a
        #: conversation deleted in the mailbox does.
        self.missing: set[str] = set()
        #: Set to refuse the refresh token the way Google does after the access
        #: was revoked in the account.
        self.revoked = False

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
            if self.revoked:
                return {"error": "invalid_grant", "error_description": "Token revoked"}
            return {"access_token": _ACCESS, "expires_in": 3599}
        self.calls.append(url)
        assert token == _ACCESS, "a Gmail API call went out without the bearer"
        if "/threads/" in url:
            return self._thread(url)
        raise AssertionError(f"the sync asked for something other than a thread: {url}")

    def _thread(self, url: str) -> dict[str, Any]:
        if self.unreachable:
            raise gmail_link.GmailError(self.unreachable)
        thread_id = url.rsplit("/", 1)[-1].split("?")[0]
        if thread_id in self.missing:
            return {"error": {"code": 404, "message": "Requested entity was not found."}}
        return {"id": thread_id, "messages": list(self.threads.get(thread_id, []))}

    def threads_asked_for(self) -> list[str]:
        return [
            url.rsplit("/", 1)[-1].split("?")[0]
            for url in self.calls
            if "/threads/" in url
        ]


# --- Fixtures --------------------------------------------------------------------


def _enable_foreign_keys(dbapi_connection, _record) -> None:
    """What ``newspulse.db.make_engine`` does for every SQLite connection.

    Without it SQLite ignores ``ON DELETE CASCADE``, and the deleted-letter test
    below would pass against behaviour the real database does not have.
    """
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as sess:
        yield sess


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A configured OAuth client and a directory for the token file."""
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "newspulse.db")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("NEWSPULSE_GMAIL_CLIENT_SECRET", "client-secret")
    return tmp_path


@pytest.fixture
def mailbox(volume):
    """A connected mailbox, written through the module's own writer."""
    gmail_link._write(
        {
            "refresh_token": _REFRESH,
            "access_token": _ACCESS,
            "expires_at": (_NOW + dt.timedelta(hours=1)).isoformat(),
            "scopes": list(gmail_link.SCOPES),
            "email": _MAILBOX,
            "connected_at": _RELEASED_AT.isoformat(),
            "lost": "",
        }
    )
    return volume


@pytest.fixture
def gmail(monkeypatch):
    """Replace the one network boundary; a conversation with our letter in it."""
    fake = FakeGmail({_THREAD_ID: [_our_letter()]})
    monkeypatch.setattr(gmail_link, "_fetch", fake)
    return fake


# --- Seeding ---------------------------------------------------------------------


def _letter(
    session,
    *,
    thread_id: str = _THREAD_ID,
    released_at: dt.datetime | None = _RELEASED_AT,
    state: OutreachState = OutreachState.RAUS,
    outcome_at: dt.datetime | None = None,
    outcome_note: str = "",
    contact_id: int | None = None,
    mandate: str = "Nordlicht Energie",
) -> Outreach:
    """One mandate, one impulse, one letter in whatever state the test needs.

    Built as a row rather than driven through release/send: those are OUT-01's
    and OUT-04's and have their own tests, and every case here turns on a
    *timestamp* that a real release would stamp with the wall clock.
    """
    client = Client(name=mandate, aliases=[], keywords=["Netzentgelte"])
    session.add(client)
    session.flush()
    angle = Angle(
        client_id=client.id,
        generated_at=_RELEASED_AT,
        subject="Netzentgelte: wer zahlt den Ausbau",
        message="Zwei Absätze Positionierung.",
        context="Aus 6 Meldungen der letzten 7 Tage.",
    )
    session.add(angle)
    session.flush()
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist=_JOURNALIST,
        outlet=_OUTLET,
        subject="Anschlussdauer statt Netzentgelt",
        message="Sehr geehrte Frau Kühn, wir haben Zahlen aus 340 Verfahren.",
        generated_at=_RELEASED_AT,
        contact_id=contact_id,
        state=state,
        released_at=released_at,
        released_by="mensch" if released_at else "",
        outcome_at=outcome_at,
        outcome_note=outcome_note,
        gmail_thread_id=thread_id,
        gmail_message_id=_OUR_MESSAGE if released_at else "",
    )
    session.add(row)
    session.commit()
    return row


def _replies(session) -> list[OutreachReply]:
    return list(session.scalars(select(OutreachReply).order_by(OutreachReply.id)).all())


class _NoAnalyzer:
    """An analyzer that must never be called: the sweeps below fetch no feeds."""

    def analyze(self, client, articles):  # pragma: no cover - guard, not behaviour
        raise AssertionError("the analyzer was called on an empty sweep")


@pytest.fixture
def quiet_post_run(monkeypatch):
    """The drafting that follows a sweep with a mandate in the database.

    Every test here is about the mailbox. Left alone, the drafting step after the
    run row can reach the real ``claude -p`` — the sweep swallows the failure, so
    the assertions would still pass while the suite spends a minute in a
    subprocess. (The theme settling beside it is already stubbed for the whole
    suite by ``conftest.no_theme_settling``, for the same reason.)
    """
    from newspulse import angles

    monkeypatch.setattr(angles, "suggest", lambda *args, **kwargs: None)


def _no_feed(*_args, **_kwargs) -> list[FeedItem]:
    """A fetch that returns nothing, so the sweep is only its wiring."""
    return []


# --- What is fetched, and what is not --------------------------------------------


def test_only_the_threads_of_released_letters_are_fetched(session, mailbox, gmail):
    """DEC-6 option A: the only conversations this tool can name are the ones it
    started, so a draft that was never released has nothing to ask about."""
    released = _letter(session)
    _letter(
        session,
        thread_id="18f-draft-thread",
        released_at=None,
        state=OutreachState.ENTWURF,
        mandate="Vermont Robotics",
    )
    _letter(session, thread_id="", mandate="Haaner Pharma")

    mailsync.sync(session, fetch=gmail, now=_NOW)

    assert gmail.threads_asked_for() == [released.gmail_thread_id]


def test_a_letter_released_before_the_window_is_not_asked_about(
    session, mailbox, gmail, caplog
):
    """Every qualifying letter is one request every morning, forever. Without a
    window the daily cost grows with every letter the agency has ever sent, and a
    conversation that ended in March is re-fetched for the rest of time."""
    recent = _letter(session)
    _letter(
        session,
        thread_id="18a-letztes-jahr",
        released_at=_NOW - mailsync._REPLY_WINDOW - dt.timedelta(days=1),
        mandate="Vermont Robotics",
    )

    with caplog.at_level(logging.INFO, logger="newspulse.mailsync"):
        report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert gmail.threads_asked_for() == [recent.gmail_thread_id]
    # Counted and said out loud: a sync that silently reads less than it looks
    # like is indistinguishable from a mailbox with nothing in it.
    assert report.aged_out == 1
    assert any("past the window" in record.getMessage() for record in caplog.records)


def test_a_letter_inside_the_window_is_still_asked_about(session, mailbox, gmail):
    """The other edge of the same rule. Ninety days rather than thirty because a
    pitch answered eight weeks later is an ordinary event in trade press."""
    _letter(
        session,
        released_at=_NOW - mailsync._REPLY_WINDOW + dt.timedelta(days=1),
    )
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert report.aged_out == 0
    assert len(_replies(session)) == 1


def test_the_gmail_query_is_the_thread_id_and_nothing_else(session, mailbox, gmail):
    """The read is addressed to one conversation. There is no search over the
    mailbox anywhere in this path, which is what makes "your mailbox is not read"
    literally true for everything that is not a pitch."""
    _letter(session)

    mailsync.sync(session, fetch=gmail, now=_NOW)

    (url,) = gmail.calls
    assert url.endswith(f"/threads/{_THREAD_ID}?format=full")
    assert "?q=" not in url
    assert "/messages" not in url


def test_a_mailbox_full_of_unrelated_mail_yields_no_rows(session, mailbox, gmail):
    """The foreign conversation is never requested, so its contents cannot be
    stored — the guarantee is in the query, not in a filter afterwards."""
    gmail.threads[_FOREIGN_THREAD] = [
        _payload(
            "18e-0001",
            sender="kanzlei@example.com",
            body="Anbei der Mandantenvertrag zur Unterschrift.",
            at=_REPLY_AT,
            thread_id=_FOREIGN_THREAD,
            subject="Vertrag",
        )
    ]
    _letter(session)

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert _FOREIGN_THREAD not in gmail.threads_asked_for()
    assert report.replies == 0
    assert _replies(session) == []


# --- One reply, stored once ------------------------------------------------------


def test_a_reply_is_stored_with_its_sender_time_and_text(session, mailbox, gmail):
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    (reply,) = _replies(session)
    assert report.replies == 1
    assert reply.outreach_id == row.id
    assert reply.gmail_message_id == _REPLY_ID
    assert (reply.from_name, reply.from_email) == (_JOURNALIST, _ADDRESS)
    # Gmail's own moment for the message, not this machine's clock.
    assert reply.received_at == _REPLY_AT
    assert reply.fetched_at == _NOW
    assert reply.body.strip() == _REPLY_TEXT


def test_a_second_sync_stores_no_second_row_and_moves_no_timestamp(
    session, mailbox, gmail
):
    """Idempotency is Google's message id and the UNIQUE column holding it. The
    ``fetched_at`` assertion is the sharp one: a re-file would rewrite when this
    tool took its copy of somebody else's mail."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    mailsync.sync(session, fetch=gmail, now=_NOW)
    (first,) = _replies(session)
    stamped = (first.id, first.fetched_at, first.received_at)

    again = mailsync.sync(session, fetch=gmail, now=_NOW + dt.timedelta(days=1))

    (only,) = _replies(session)
    assert again.replies == 0
    assert (only.id, only.fetched_at, only.received_at) == stamped


def test_several_replies_in_one_thread_are_all_filed(session, mailbox, gmail):
    """One letter collects several answers, which is why replies are a table and
    not a column."""
    _letter(session)
    gmail.threads[_THREAD_ID] += [
        _journalist_reply(),
        _journalist_reply(_SECOND_REPLY, body="Noch eine Nachfrage.", at=_LATER_AT),
    ]

    mailsync.sync(session, fetch=gmail, now=_NOW)

    assert [reply.gmail_message_id for reply in _replies(session)] == [
        _REPLY_ID,
        _SECOND_REPLY,
    ]


# --- What this mailbox itself sent -----------------------------------------------


def test_the_outgoing_letter_is_never_stored_as_a_reply(session, mailbox, gmail):
    """It is in the very thread being read, and it is this tool's own words.
    Filing it would put RauteOS's letter into the contact's file as an answer."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    mailsync.sync(session, fetch=gmail, now=_NOW)

    (reply,) = _replies(session)
    assert reply.gmail_message_id == _REPLY_ID
    assert reply.from_email == _ADDRESS


def test_a_message_from_the_connected_address_is_ours_whatever_the_labels_say(
    session, mailbox, gmail
):
    """The SENT label is the authority and the address is the belt to its braces.
    A message the account sent from another client may reach the thread without
    the label; it is still not a reply to ourselves."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(
        _payload(
            "18f-unlabelled",
            sender=f"Lucas Raute <{_MAILBOX.upper()}>",
            body="Kurzer Nachtrag von mir.",
            at=_LATER_AT,
        )
    )

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert report.replies == 0
    assert _replies(session) == []


def test_a_draft_sitting_in_the_thread_is_not_a_reply(session, mailbox, gmail):
    """A composed draft lives in the same conversation and has been sent to
    nobody at all."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(
        _payload(
            "18f-entwurf",
            sender=f"Lucas Raute <{_MAILBOX}>",
            body="Noch nicht abgeschickt.",
            at=_LATER_AT,
            labels=("DRAFT",),
        )
    )

    assert mailsync.sync(session, fetch=gmail, now=_NOW).replies == 0


# --- The state the reply may set, and the one it may not -------------------------


def test_the_first_reply_moves_the_letter_to_antwort_at_the_messages_own_time(
    session, mailbox, gmail
):
    """``outcome_at`` says when the thing happened as far as anyone can know it:
    a reply that arrived on Saturday and was read on Monday belongs to Saturday."""
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(row)
    assert row.state == OutreachState.ANTWORT
    assert row.outcome_at == _REPLY_AT
    assert report.answered == 1


@pytest.mark.parametrize(
    "recorded", [OutreachState.ABSAGE, OutreachState.VEROEFFENTLICHT]
)
def test_an_outcome_a_person_recorded_survives_and_the_reply_is_still_stored(
    session, mailbox, gmail, recorded
):
    """"Danke, nichts für uns" and "schicken Sie mehr" are the same event to a
    matcher and opposite events to a consultant, so what a person read out of the
    mail outranks what the sync can see."""
    row = _letter(
        session,
        state=recorded,
        outcome_at=_LATER_AT,
        outcome_note="Telefonisch abgesagt.",
    )
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(row)
    assert row.state == recorded
    assert row.outcome_at == _LATER_AT
    assert row.outcome_note == "Telefonisch abgesagt."
    assert report.replies == 1
    assert len(_replies(session)) == 1


def test_the_outcome_the_sync_writes_is_signed_as_the_machines(
    session, mailbox, gmail
):
    """The ledger's first question about any line is who said it. Stored on the
    row rather than inferred later from state, note and a matching reply time:
    that inference reads a reply row, and a retention rule deleting one would
    turn this line back into a sentence a consultant never typed."""
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(row)
    assert row.outcome_by == OUTCOME_BY_MAILBOX
    assert row.outcome_from_mailbox
    # And it survives the reply row being deleted, which is the whole point of
    # storing it: ``fetched_at`` exists so a retention rule can do exactly this.
    session.delete(_replies(session)[0])
    session.commit()
    session.refresh(row)
    assert row.outcome_from_mailbox


def test_the_outcome_a_person_records_is_signed_as_a_humans(session):
    """The other half of the same rule, and the reason the field is not a
    boolean: a hand-typed outcome is signed the way a release is."""
    row = _letter(session)

    outreach.record_outcome(session, row, OutreachState.ABSAGE, "Telefonisch abgesagt.")

    assert row.outcome_by == outreach.DEFAULT_RELEASED_BY
    assert not row.outcome_from_mailbox


def test_a_later_reply_does_not_move_the_first_ones_timestamp(session, mailbox, gmail):
    """"The first reply moves the letter" means the first: the moment the letter
    stopped being unanswered does not drift forward with every follow-up."""
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    mailsync.sync(session, fetch=gmail, now=_NOW)

    gmail.threads[_THREAD_ID].append(
        _journalist_reply(_SECOND_REPLY, body="Nachfrage.", at=_LATER_AT)
    )
    mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(row)
    assert row.outcome_at == _REPLY_AT
    assert len(_replies(session)) == 2


# --- What is an answer, and what merely shares the conversation ------------------


def test_a_delivery_failure_is_stored_but_is_not_the_journalists_answer(
    session, mailbox, gmail
):
    """Gmail threads a bounce into the conversation it failed to deliver. Read as
    a reply, a letter that never arrived lands in the ledger as answered — signed
    by the machine, at the bounce's own time, and indistinguishable from the real
    thing in the one record DEC-1 exists to make auditable."""
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(
        _payload(
            "18f-unzustellbar",
            sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
            body=f"Address not found. Your message wasn't delivered to {_ADDRESS}.",
            at=_REPLY_AT,
            subject="Delivery Status Notification (Failure)",
        )
    )

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(row)
    assert row.state == OutreachState.RAUS
    assert row.outcome_at is None
    assert report.answered == 0
    # Stored all the same: "this never arrived" is worth having on the file.
    (stored,) = _replies(session)
    assert stored.from_email == "mailer-daemon@googlemail.com"


def test_a_message_older_than_the_release_is_not_an_answer_to_it(
    session, mailbox, gmail
):
    """A conversation can carry a message from before the pitch inside it, and
    ``outcome_at`` is what the contact's file sorts on: taken from such a message
    the answer renders below the release it answers."""
    row = _letter(session)
    before = _RELEASED_AT - dt.timedelta(days=3)
    gmail.threads[_THREAD_ID].append(
        _journalist_reply("18f-vorher", body="Haben Sie dazu Zahlen?", at=before)
    )

    mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(row)
    assert row.state == OutreachState.RAUS
    assert row.outcome_at is None
    # Stored with Gmail's own moment: it is a real message in the conversation.
    (reply,) = _replies(session)
    assert reply.received_at == before
    # And the belt to that brace, where the ledger is actually written: an
    # outcome is never stamped before the release it belongs to.
    assert outreach.record_reply(session, row, at=before)
    assert row.outcome_at == _RELEASED_AT


def test_an_over_long_from_header_is_cut_to_the_columns_own_width(
    session, mailbox, gmail
):
    """A ``From`` header is a stranger's text and nothing upstream bounds it.
    SQLite grows the row without complaint — the exact thing the column width
    exists to prevent — and every other backend raises on the commit."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(
        _payload(
            "18f-lang",
            sender='"' + "A" * 900 + '" <' + "b" * 400 + '@example.com>',
            body="Danke.",
            at=_REPLY_AT,
        )
    )

    mailsync.sync(session, fetch=gmail, now=_NOW)

    (reply,) = _replies(session)
    assert 0 < len(reply.from_name) <= mailsync._FROM_NAME_MAX
    assert 0 < len(reply.from_email) <= mailsync._FROM_EMAIL_MAX


def test_both_letters_in_one_conversation_reach_antwort(session, mailbox, gmail):
    """A letter re-pushed after its row was replaced leaves two rows naming one
    thread. The newer one used to be dropped from the day's work entirely: never
    answered, never counted, and nothing anywhere saying so."""
    first = _letter(session)
    second = _letter(session, mandate="Vermont Robotics")
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    session.refresh(first)
    session.refresh(second)
    assert (first.state, second.state) == (OutreachState.ANTWORT,) * 2
    assert report.answered == 2
    assert report.shared_threads == 1
    # One conversation, one request. The message itself is filed against the
    # letter that opened it, because Google's message id is UNIQUE per row.
    assert gmail.threads_asked_for() == [_THREAD_ID]
    (reply,) = _replies(session)
    assert reply.outreach_id == first.id


# --- When Gmail cannot be read ---------------------------------------------------


def test_an_unreachable_gmail_logs_at_error_and_changes_no_row(
    session, mailbox, gmail, caplog
):
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    gmail.unreachable = "Google ist nicht erreichbar: timed out"

    with caplog.at_level(logging.ERROR, logger="newspulse.mailsync"):
        report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert report.errors
    assert not report.ok
    assert caplog.records and all(r.levelno == logging.ERROR for r in caplog.records)
    session.refresh(row)
    assert _replies(session) == []
    assert row.state == OutreachState.RAUS
    assert row.outcome_at is None


def test_a_revoked_access_is_reported_rather_than_raised(session, mailbox, gmail):
    """Google refusing the refresh token means the connection is over, which is a
    state this tool has to display — not an exception for the sweep to survive."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    gmail.revoked = True

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert report.errors
    assert _replies(session) == []
    # And the connection is now honestly marked as lost, with the reason.
    link = gmail_link.connected()
    assert link is not None
    assert not link.is_connected
    assert "invalid_grant" in link.lost


def test_one_unreadable_thread_does_not_stop_the_next_one(session, mailbox, gmail):
    """A conversation deleted in the mailbox is a fact about that conversation.
    The letters behind it still have answers waiting."""
    _letter(session, thread_id=_FOREIGN_THREAD)
    good = _letter(session, mandate="Vermont Robotics")
    gmail.missing = {_FOREIGN_THREAD}
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert report.replies == 1
    (reply,) = _replies(session)
    assert reply.outreach_id == good.id


def test_deleted_threads_do_not_starve_the_letters_behind_them(
    session, mailbox, gmail
):
    """A thread deleted in the mailbox answers 404 forever. Counted towards the
    give-up, three of them mean every letter behind them is never asked about —
    not once, but every morning, for the whole ninety-day window."""
    starved = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    for index in range(mailsync._MAX_CONSECUTIVE_FAILURES):
        _letter(session, thread_id=f"18f-geloescht-{index}", mandate=f"Mandant {index}")
        gmail.missing.add(f"18f-geloescht-{index}")

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    (reply,) = _replies(session)
    assert reply.outreach_id == starved.id
    assert report.left_unread == 0
    # Reported, never swallowed: the conversations are still gone.
    assert len(report.errors) == mailsync._MAX_CONSECUTIVE_FAILURES


def test_the_sync_gives_up_after_a_run_of_failures(session, mailbox, gmail):
    """Unreachable and revoked answer the same way for every thread, and each
    attempt costs a request timeout. Walking two hundred letters to hear it two
    hundred times would turn a failed read into an hour-long sweep."""
    for index in range(6):
        _letter(session, thread_id=f"18f-thread-{index}", mandate=f"Mandant {index}")
    gmail.unreachable = "Google ist nicht erreichbar: timed out"

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert len(gmail.threads_asked_for()) == mailsync._MAX_CONSECUTIVE_FAILURES
    assert len(report.errors) == mailsync._MAX_CONSECUTIVE_FAILURES
    # And the report says what was read rather than what was selected: it read
    # three of six, and a caller asserting on it was told it had read all six.
    assert report.threads == mailsync._MAX_CONSECUTIVE_FAILURES
    assert report.left_unread == 6 - mailsync._MAX_CONSECUTIVE_FAILURES


# --- With no mailbox connected ---------------------------------------------------


def test_the_sync_is_a_no_op_with_no_mailbox_connected(session, volume, gmail):
    """The ordinary state of an installation that never linked one. Not an error:
    a report that called it one would put a red line in a clean sweep."""
    _letter(session)

    report = mailsync.sync(session, fetch=gmail, now=_NOW)

    assert report == mailsync.SyncReport()
    assert not report.connected
    assert report.ok
    assert gmail.calls == []


def test_the_daily_run_is_unaffected_when_no_mailbox_is_connected(session, volume):
    """The sweep itself, with the real sync wired in and nothing connected."""
    report = job.run(
        session,
        feeds=[],
        fetch=_no_feed,
        analyzer=_NoAnalyzer(),
        now=lambda: _NOW,
    )

    assert report.status is RunStatus.OK
    assert report.errors == []


def test_a_failing_mail_sync_never_fails_the_daily_sweep(
    session, volume, monkeypatch, caplog
):
    """The guard the story asks for. The day's coverage is already stored by the
    time Google is asked anything, and losing it to an unreadable mailbox would
    trade the part that worked for the part that did not.

    Partial and never failed: the run stands, every stored row stands, and the
    post-run work below it still runs. What changes is only that the sweep says
    so — see the test below.
    """

    def _explode(*args, **kwargs):
        raise RuntimeError("Google ist nicht erreichbar")

    monkeypatch.setattr(mailsync, "sync", _explode)

    with caplog.at_level(logging.ERROR, logger="newspulse.job"):
        report = job.run(
            session,
            feeds=[],
            fetch=_no_feed,
            analyzer=_NoAnalyzer(),
            now=lambda: _NOW,
        )

    assert report.status is not RunStatus.FAILED
    assert any("mail sync failed" in record.message for record in caplog.records)
    # And the session is usable afterwards: a caught exception is not a clean
    # transaction, and everything after this point in the sweep runs on it.
    assert session.scalars(select(Outreach)).all() == []


def test_an_unreadable_mailbox_marks_the_run_partial_rather_than_green(
    session, mailbox, gmail, quiet_post_run
):
    """A mailbox nobody can read for a week is a fact about the installation, and
    a run row saying ``ok`` with an empty error list hides it in a log nobody
    tails. The sweep is not failed by it — no stored row moves — but it is not
    clean either, and the row has to say which."""
    _letter(session)
    gmail.unreachable = "Google ist nicht erreichbar: timed out"

    report = job.run(
        session, feeds=[], fetch=_no_feed, analyzer=_NoAnalyzer(), now=lambda: _NOW
    )

    assert report.status is RunStatus.PARTIAL
    assert any(_THREAD_ID in error for error in report.errors)
    stored = session.scalars(select(Run).order_by(Run.id.desc())).first()
    assert stored.status is RunStatus.PARTIAL
    assert any(_THREAD_ID in error for error in stored.errors)


def test_the_run_stamps_the_copy_it_took_with_its_own_clock(
    session, mailbox, gmail, quiet_post_run
):
    """``fetched_at`` is the one field that answers "when did this tool take a
    copy of somebody else's mail". A sweep on a frozen clock has to be able to
    answer it, so the run's clock reaches the sync rather than the wall's."""
    _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())

    job.run(session, feeds=[], fetch=_no_feed, analyzer=_NoAnalyzer(), now=lambda: _NOW)

    (reply,) = _replies(session)
    assert reply.fetched_at == _NOW
    # And the receipt time is still Gmail's own, which is a different fact.
    assert reply.received_at == _REPLY_AT


# --- The contact's file ----------------------------------------------------------


def _seed_rendered(factory) -> int:
    """One released letter to a recorded contact, with a reply already filed."""
    with factory() as session:
        kuehn = contacts.save(
            session, name=_JOURNALIST, outlet=_OUTLET, email=_ADDRESS
        )
        row = _letter(session, contact_id=kuehn.id)
        session.add(
            OutreachReply(
                outreach_id=row.id,
                gmail_message_id=_REPLY_ID,
                from_name=_JOURNALIST,
                from_email=_ADDRESS,
                received_at=_REPLY_AT,
                body=_REPLY_TEXT,
                fetched_at=_NOW,
            )
        )
        outreach.record_reply(session, row, at=_REPLY_AT)
        return kuehn.id


def test_the_reply_shows_in_the_timeline_with_its_sender_time_text_and_source(
    web, factory
):
    """Every line on this page says where it came from, and this is the only one
    written by somebody outside the agency."""
    contact_id = _seed_rendered(factory)

    body = web.get("/contacts", params={"id": contact_id}).text

    assert _JOURNALIST in body
    assert "aus dem Postfach gelesen" in body
    assert _REPLY_TEXT in body
    # The received time, not just the day: "wann kam die Antwort" is its own
    # question. Rendered in the reader's zone, so the date is what is asserted.
    assert "14.08.2026" in body
    assert 'class="tl__reply"' in body


def test_the_replys_marker_is_not_the_one_a_typed_entry_wears(web, factory):
    """A machine-read line and a hand-typed one must not look alike: one is
    somebody's words, the other somebody's judgement.

    The trap this catches is subtle and was live: the reply sets ``antwort``, the
    file draws every outcome as "von Hand eingetragen", and the page therefore
    claimed a consultant had typed a line the sync wrote.
    """
    contact_id = _seed_rendered(factory)

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "aus dem Postfach gelesen" in body
    assert "von Hand eingetragen" not in body


def test_an_outcome_a_person_typed_is_still_drawn_as_one(web, factory):
    """The other half of the same rule: suppressing the sync's own line must not
    suppress the consultant's."""
    with factory() as session:
        kuehn = contacts.save(
            session, name=_JOURNALIST, outlet=_OUTLET, email=_ADDRESS
        )
        row = _letter(
            session,
            contact_id=kuehn.id,
            state=OutreachState.VEROEFFENTLICHT,
            outcome_at=_LATER_AT,
            outcome_note="Stück ist am Freitag erschienen.",
        )
        session.add(
            OutreachReply(
                outreach_id=row.id,
                gmail_message_id=_REPLY_ID,
                from_name=_JOURNALIST,
                from_email=_ADDRESS,
                received_at=_REPLY_AT,
                body=_REPLY_TEXT,
                fetched_at=_NOW,
            )
        )
        session.commit()
        contact_id = kuehn.id

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "von Hand eingetragen" in body
    assert "Stück ist am Freitag erschienen." in body
    assert "aus dem Postfach gelesen" in body


def test_a_reply_carrying_markup_is_rendered_as_text(web, factory):
    """The only text on this page written by somebody outside the agency, and
    the only one nobody here reviewed before it was stored. A mail whose body is
    markup must read as markup, not run as it."""
    with factory() as session:
        kuehn = contacts.save(session, name=_JOURNALIST, outlet=_OUTLET)
        row = _letter(session, contact_id=kuehn.id)
        session.add(
            OutreachReply(
                outreach_id=row.id,
                gmail_message_id=_REPLY_ID,
                from_name="<script>alert(1)</script>",
                from_email=_ADDRESS,
                received_at=_REPLY_AT,
                body="<img src=x onerror=alert(1)> Danke!",
                fetched_at=_NOW,
            )
        )
        session.commit()
        contact_id = kuehn.id

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "<script>alert(1)</script>" not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt; Danke!" in body


def test_the_reply_line_translates_with_the_rest_of_the_chrome(web, factory):
    from newspulse import i18n

    contact_id = _seed_rendered(factory)
    web.cookies.set(i18n.COOKIE_NAME, "en")

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "read from the mailbox" in body
    assert f"Reply from {_JOURNALIST}" in body
    # The journalist's own words are data and stay as they were written.
    assert _REPLY_TEXT in body


def test_the_timeline_orders_a_reply_between_the_release_and_a_later_outcome(
    session,
):
    """Three kinds of line, one descending order. A reply to July's pitch belongs
    where it arrived, not under the letter it answers."""
    row = _letter(session)
    session.add(
        OutreachReply(
            outreach_id=row.id,
            gmail_message_id=_REPLY_ID,
            from_name=_JOURNALIST,
            from_email=_ADDRESS,
            received_at=_REPLY_AT,
            body=_REPLY_TEXT,
            fetched_at=_NOW,
        )
    )
    row.state = OutreachState.VEROEFFENTLICHT
    row.outcome_at = _LATER_AT
    session.commit()
    history = [outreach.HistoryEntry(letter=row, mandate="Nordlicht Energie")]

    events = outreach.timeline(history)

    assert [event.kind for event in events] == [
        outreach.TimelineKind.OUTCOME,
        outreach.TimelineKind.REPLY,
        outreach.TimelineKind.RELEASE,
    ]
    assert events[1].reply is not None
    assert events[1].at == _REPLY_AT


# --- The letter card, on the page the letter was written from --------------------


def _seed_card(factory) -> int:
    """One released letter with a reply filed against it; returns the mandate id."""
    with factory() as session:
        row = _letter(session)
        session.add(
            OutreachReply(
                outreach_id=row.id,
                gmail_message_id=_REPLY_ID,
                from_name=_JOURNALIST,
                from_email=_ADDRESS,
                received_at=_REPLY_AT,
                body=_REPLY_TEXT,
                fetched_at=_NOW,
            )
        )
        outreach.record_reply(session, row, at=_REPLY_AT)
        return row.client_id


def test_the_letter_card_shows_the_answer_in_the_journalists_own_words(web, factory):
    """The locked mock's reply box (features/mocks/gmail-send.html), where the
    consultant actually reads the letter he sent."""
    client_id = _seed_card(factory)

    body = web.get(f"/client/{client_id}/advice").text

    assert _REPLY_TEXT in body
    # In the mock's own box, so the answer reads as somebody else's words rather
    # than as another paragraph of the letter above it.
    assert 'class="reply__from"' in body
    assert "Antwort im selben Verlauf" in body


def test_the_cards_trail_does_not_sign_the_syncs_line_with_a_human(web, factory):
    """The same trap as on the contact's file, on the second page that draws an
    ``outcome_at``: the trail promises "each one a thing a person did", and the
    line the mailbox wrote is not one."""
    client_id = _seed_card(factory)

    body = web.get(f"/client/{client_id}/advice").text

    assert "Antwort im Postfach gelesen" in body
    assert "Ergebnis eingetragen" not in body


def test_the_cards_trail_still_says_so_when_a_person_typed_the_outcome(web, factory):
    """And the consultant's own line keeps the words that say he typed it."""
    with factory() as session:
        row = _letter(session)
        outreach.record_outcome(
            session, row, OutreachState.VEROEFFENTLICHT, "Stück läuft am Freitag."
        )
        client_id = row.client_id

    body = web.get(f"/client/{client_id}/advice").text

    assert "Ergebnis eingetragen" in body
    assert "Stück läuft am Freitag." in body
    assert "Antwort im Postfach gelesen" not in body


def test_a_reply_carrying_markup_is_text_on_the_card_too(web, factory):
    """Nobody at this agency reviewed this text before it was stored, and it is
    rendered on two pages now."""
    with factory() as session:
        row = _letter(session)
        session.add(
            OutreachReply(
                outreach_id=row.id,
                gmail_message_id=_REPLY_ID,
                from_name=_JOURNALIST,
                from_email=_ADDRESS,
                received_at=_REPLY_AT,
                body="<img src=x onerror=alert(1)> Danke!",
                fetched_at=_NOW,
            )
        )
        session.commit()
        client_id = row.client_id

    body = web.get(f"/client/{client_id}/advice").text

    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt; Danke!" in body


# --- Nothing outlives the letter it belonged to ----------------------------------


def test_deleting_a_letter_deletes_its_replies(session, mailbox, gmail):
    """A journalist's own words must not sit in the database with nothing left to
    say what they were an answer to."""
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    mailsync.sync(session, fetch=gmail, now=_NOW)
    assert len(_replies(session)) == 1

    session.delete(row)
    session.commit()

    assert _replies(session) == []


def test_deleting_the_mandate_takes_the_letters_and_their_replies(session, mailbox, gmail):
    """The same guarantee one level up, where the database's own cascade is what
    enforces it: a mandate is deleted with a raw cascade, not by the ORM walking
    every letter."""
    row = _letter(session)
    gmail.threads[_THREAD_ID].append(_journalist_reply())
    mailsync.sync(session, fetch=gmail, now=_NOW)
    client = session.get(Client, row.client_id)

    session.delete(client)
    session.commit()

    assert session.scalars(select(Outreach)).all() == []
    assert _replies(session) == []


# --- The schema ------------------------------------------------------------------


def test_the_migration_creates_the_replies_table(tmp_path, monkeypatch):
    """``alembic upgrade head`` on an empty database reaches 0020, and the one
    constraint the sync's idempotency stands on is there."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert "outreach_replies" in inspector.get_table_names()
        columns = {c["name"]: c for c in inspector.get_columns("outreach_replies")}
        assert set(columns) == {
            "id",
            "outreach_id",
            "gmail_message_id",
            "from_name",
            "from_email",
            "received_at",
            "body",
            "fetched_at",
        }
        assert columns["gmail_message_id"]["nullable"] is False
        unique = {
            tuple(c["column_names"])
            for c in inspector.get_unique_constraints("outreach_replies")
        }
        assert ("gmail_message_id",) in unique
        (key,) = inspector.get_foreign_keys("outreach_replies")
        assert key["referred_table"] == "outreach"
        assert key["options"]["ondelete"].upper() == "CASCADE"
    finally:
        engine.dispose()


def test_the_migration_adds_the_author_of_an_outcome(tmp_path, monkeypatch):
    """0021. Who recorded an outcome is stored beside when, because a machine can
    record one now and both pages that draw it have to say which it was."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("outreach")}
        assert "outcome_by" in columns
        assert columns["outcome_by"]["nullable"] is False
    finally:
        engine.dispose()
