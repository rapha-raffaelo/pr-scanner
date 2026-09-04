"""The contact book, and the click that leads into it.

"Kannst du es so machen, dass der PR-Berater auf den Journalisten klicken kann
und dann im Kontaktbuch landet? Wenn er in der Vergangenheit Kontaktdaten
abgespeichert hat, liegen sie dort, wenn nicht kann er das mit einem simplen
Edit machen."

That is also the only honest way for this tool to hold contact details at all:
it proposes who to approach from what the press actually published, and the
person supplies how to reach them. Nothing here derives, guesses or completes an
address, and the last test in this file is the one that keeps it that way.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import contacts, pitch
from newspulse.models import Angle, Article, Base, Client, Contact, TopicHit
from newspulse.web.app import create_app, get_db

_NOW = dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


# --- The book itself ------------------------------------------------------------


def test_a_contact_is_found_however_the_name_was_typed(factory):
    """A feed says "Maria Berg" and a person types "maria berg"; a second row for
    the same journalist is how a contact book stops being trusted."""
    with factory() as session:
        contacts.save(session, name="Maria Berg", outlet="Textilwirtschaft",
                      email="m.berg@example.de")

        found = contacts.find(session, "maria berg", "TEXTILWIRTSCHAFT")

        assert found is not None and found.email == "m.berg@example.de"


def test_saving_the_same_person_twice_updates_rather_than_duplicates(factory):
    with factory() as session:
        contacts.save(session, name="Maria Berg", outlet="Textilwirtschaft")
        contacts.save(session, name="Maria Berg", outlet="Textilwirtschaft",
                      email="neu@example.de")

        assert session.scalar(select(func.count()).select_from(Contact)) == 1
        assert contacts.find(session, "Maria Berg").email == "neu@example.de"


def test_the_same_journalist_at_a_new_outlet_is_a_new_contact(factory):
    """Somebody who moves masthead is reachable differently; keeping one row
    would silently overwrite the address that still works."""
    with factory() as session:
        contacts.save(session, name="Maria Berg", outlet="Textilwirtschaft",
                      email="alt@tw.de")
        contacts.save(session, name="Maria Berg", outlet="Handelsblatt",
                      email="neu@hb.de")

        assert session.scalar(select(func.count()).select_from(Contact)) == 2


def test_a_contact_needs_a_name(factory):
    with factory() as session:
        with pytest.raises(ValueError):
            contacts.save(session, name="   ", outlet="Textilwirtschaft")


# --- The click from the pitch list ----------------------------------------------


def _seed_pitch(session) -> tuple[Client, Angle]:
    client = Client(
        name="Zalando", aliases=[], industry="Modehandel",
        keywords=["Retouren"], alert_topics=[], country="DE",
    )
    session.add(client)
    session.flush()
    article = Article(
        title="Retouren im Modehandel sinken", url="https://ex.de/r1",
        source="Textilwirtschaft", author="Maria Berg",
        published_at=_NOW - dt.timedelta(days=2), fetched_at=_NOW,
        summary_text=None, language="de", title_hash="r1000001",
    )
    session.add(article)
    session.flush()
    session.add(TopicHit(article_id=article.id, client_id=client.id, found_at=_NOW))
    angle = Angle(
        client_id=client.id, generated_at=_NOW, subject="s", message="m",
        context="c", thesis="t", article_ids=[article.id],
    )
    session.add(angle)
    session.commit()
    return client, angle


def test_the_name_links_into_the_contact_book(factory, client):
    with factory() as session:
        subject, _ = _seed_pitch(session)
        client_id = subject.id

    body = client.get(f"/client/{client_id}/advice").text

    assert "/contacts?name=Maria%20Berg" in body or "/contacts?name=Maria+Berg" in body


def test_an_unknown_byline_opens_a_prefilled_form(factory, client):
    """"Wenn nicht, kann er das mit einem simplen Edit machen" — so the form
    arrives carrying what the feed already knew."""
    with factory() as session:
        _seed_pitch(session)

    body = client.get(
        "/contacts", params={"name": "Maria Berg", "outlet": "Textilwirtschaft"}
    ).text

    assert "Noch kein Eintrag für" in body
    assert 'value="Maria Berg"' in body
    assert 'value="Textilwirtschaft"' in body


def test_a_known_byline_opens_on_what_was_recorded(factory, client):
    with factory() as session:
        _seed_pitch(session)
        contacts.save(session, name="Maria Berg", outlet="Textilwirtschaft",
                      email="m.berg@example.de", beat="Handel")

    body = client.get(
        "/contacts", params={"name": "Maria Berg", "outlet": "Textilwirtschaft"}
    ).text

    assert "Noch kein Eintrag für" not in body
    assert 'value="m.berg@example.de"' in body
    assert 'value="Handel"' in body


def test_a_stored_address_shows_up_beside_the_draft(factory, client):
    """The point of the round trip: next time, the answer is already there."""
    with factory() as session:
        subject, _ = _seed_pitch(session)
        contacts.save(session, name="Maria Berg", outlet="Textilwirtschaft",
                      email="m.berg@example.de")
        client_id = subject.id

    body = client.get(f"/client/{client_id}/advice").text

    assert "mailto:m.berg@example.de" in body


def test_saving_from_the_pitch_list_returns_to_the_draft(factory, client):
    with factory() as session:
        subject, _ = _seed_pitch(session)
        client_id = subject.id

    resp = client.post(
        "/contacts",
        data={
            "name": "Maria Berg", "outlet": "Textilwirtschaft",
            "email": "m.berg@example.de",
            "redirect_to": f"/client/{client_id}/advice",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/client/{client_id}/advice"


def test_an_offsite_redirect_is_refused(factory, client):
    with factory() as session:
        _seed_pitch(session)

    resp = client.post(
        "/contacts",
        data={"name": "Maria Berg", "redirect_to": "//evil.example.com"},
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/contacts"


# --- The line that must not move ------------------------------------------------


def test_no_address_is_ever_produced_without_someone_typing_it(factory, client):
    """The whole reason the book exists. A byline with no recorded contact must
    offer the empty form, never a plausible address derived from the name and the
    masthead — that would be used, and would reach the wrong person."""
    with factory() as session:
        subject, angle = _seed_pitch(session)
        targets = pitch.targets_for(session, subject, angle, now=_NOW)
        client_id = subject.id

    assert targets
    assert all(t.contact_email == "" for t in targets)

    body = client.get(f"/client/{client_id}/advice").text
    assert "mailto:" not in body
    assert "Kontakt hinterlegen" in body


# --- The position at the masthead ------------------------------------------------
#
# "was wir noch haben ist die Position im Kontaktbuch, das sollte hinzugefügt
# werden. hier schreiben wir am besten immer die Position im Unternehmen."
#
# The book knew the masthead and the beat, and neither answers the question a
# pitch turns on: two people cover banking at the same paper, and the one who
# runs the desk is approached differently from the one who files to them.


def test_the_position_is_stored_and_read_back(factory):
    with factory() as session:
        contacts.save(
            session,
            name="Andreas Kröner",
            outlet="Handelsblatt",
            position="Leiter Ressort Banken und Versicherer",
        )

        found = contacts.find(session, "Andreas Kröner", "Handelsblatt")

        assert found.position == "Leiter Ressort Banken und Versicherer"


def test_the_position_is_its_own_field_and_not_the_beat(factory):
    """Two different questions: what they cover, and who they are at the desk.
    Folding either into the other loses the one a pitch actually turns on."""
    with factory() as session:
        contacts.save(
            session,
            name="Andreas Kröner",
            outlet="Handelsblatt",
            beat="Finanzen, Wirtschaft",
            position="Leiter Ressort Banken und Versicherer",
        )

        found = contacts.find(session, "Andreas Kröner", "Handelsblatt")

        assert found.beat == "Finanzen, Wirtschaft"
        assert found.position == "Leiter Ressort Banken und Versicherer"


def test_an_entry_saved_without_a_position_has_none_rather_than_null(factory):
    """Non-null with an empty default, like every other optional string here: an
    entry that predates the column has no position, not an unknown one."""
    with factory() as session:
        contacts.save(session, name="Freie Autorin", outlet="")

        assert contacts.find(session, "Freie Autorin").position == ""


def test_the_file_shows_the_position_beside_the_masthead(client, factory):
    with factory() as session:
        contacts.save(
            session,
            name="Andreas Kröner",
            outlet="Handelsblatt",
            beat="Finanzen, Wirtschaft",
            position="Leiter Ressort Banken und Versicherer",
        )
        stored = contacts.find(session, "Andreas Kröner", "Handelsblatt").id

    body = client.get("/contacts", params={"id": stored}).text

    assert "Leiter Ressort Banken und Versicherer" in body
    # Masthead, then role, then beat — one line, in that order.
    assert body.index("Handelsblatt") < body.index("Leiter Ressort Banken")


def test_the_form_offers_the_position_prefilled(client, factory):
    with factory() as session:
        contacts.save(
            session,
            name="Andreas Kröner",
            outlet="Handelsblatt",
            position="Leiter Ressort Banken und Versicherer",
        )
        stored = contacts.find(session, "Andreas Kröner", "Handelsblatt").id

    body = client.get("/contacts", params={"edit": stored}).text

    assert 'name="position"' in body
    assert 'value="Leiter Ressort Banken und Versicherer"' in body


def test_saving_through_the_form_records_the_position(client, factory):
    client.post(
        "/contacts",
        data={
            "name": "Andreas Kröner",
            "outlet": "Handelsblatt",
            "position": "Leiter Ressort Banken und Versicherer",
        },
        follow_redirects=False,
    )

    with factory() as session:
        found = contacts.find(session, "Andreas Kröner", "Handelsblatt")
        assert found.position == "Leiter Ressort Banken und Versicherer"


def test_recording_an_address_leaves_the_position_alone(factory):
    """The letter card's one-field write must not blank it, the same way it must
    not blank the phone or the notes."""
    with factory() as session:
        contacts.save(
            session,
            name="Andreas Kröner",
            outlet="Handelsblatt",
            position="Leiter Ressort Banken und Versicherer",
        )

        contacts.remember_address(
            session,
            name="Andreas Kröner",
            outlet="Handelsblatt",
            email="kroener@handelsblatt.com",
        )

        found = contacts.find(session, "Andreas Kröner", "Handelsblatt")
        assert found.email == "kroener@handelsblatt.com"
        assert found.position == "Leiter Ressort Banken und Versicherer"
