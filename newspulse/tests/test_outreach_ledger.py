"""The outreach ledger: a letter released, and what came back.

The loop from impulse to journalist ended at a Kopieren button, so nothing in the
tool knew whether anything had gone out. These tests are about the record that
closes it, and three of them are about refusals rather than features: a ledger
that can be made to claim a fact nobody entered is worse than no ledger, because
it is read as evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import types
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import brain, outreach
from newspulse.models import (
    SILENT_AFTER_DAYS,
    Angle,
    Base,
    Client,
    Contact,
    Outreach,
    OutreachState,
)
from newspulse.pitch import PitchTarget
from newspulse.schemas import PersonalMessage
from newspulse.web.app import create_app, get_db

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


_TARGET = PitchTarget(
    outlet="Börsen-Zeitung",
    journalist="Jason Nelson",
    reason="schreibt über das Themenfeld",
    evidence=("Was ist Arc? Die Stablecoin-Kette von Circle",),
    about_client=0,
)


def _mandate(session) -> tuple[Client, Angle]:
    client = Client(name="Alpha AG", industry="Neobroker", keywords=["Verwahrung"])
    session.add(client)
    session.flush()
    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Verfügbarkeit als Risikoparameter",
        message="Zwei Absätze Positionierung.",
        context="Laut CoinDesk stand die Kette kurz still.",
        thesis="Die Liveness der Kette ist ein eigener Risikoparameter.",
        overclaim="Solana ist unzuverlässig, also wandert Liquidität ab.",
    )
    session.add(angle)
    session.commit()
    return client, angle


def _write(session, client, angle, target=_TARGET, message: str = "Erster Versuch.") -> Outreach:
    """One drafted letter, through the real ``draft``/``store`` path."""
    written = outreach.draft(
        session,
        client,
        angle,
        target,
        invoke=lambda *a, **k: json.dumps(
            {
                "subject": "Verfügbarkeit als Risikoparameter",
                "message": message,
                "hook": "Hat vergangene Woche über Circles Kette geschrieben.",
            }
        ),
    )
    return outreach.store(session, client, angle, written, target)


# --- Releasing -------------------------------------------------------------------


def test_a_fresh_letter_is_a_draft(session):
    client, angle = _mandate(session)

    row = _write(session, client, angle)

    assert row.state == OutreachState.ENTWURF
    assert row.released_at is None
    assert row.released_by == ""


def test_releasing_records_the_time_the_person_and_the_state(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)

    outreach.release(session, row)

    assert row.state == OutreachState.RAUS
    assert row.released_at is not None
    assert row.released_by == "mensch"


def test_a_release_with_no_name_is_signed_mensch(session):
    """There are no user accounts in this tool. What the field records is that a
    person and not the machine did it, the same distinction ``ClientFact`` draws."""
    client, angle = _mandate(session)
    row = _write(session, client, angle)

    outreach.release(session, row, released_by="   ")

    assert row.released_by == outreach.DEFAULT_RELEASED_BY == "mensch"


def test_a_named_releaser_is_kept(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)

    outreach.release(session, row, released_by="Lucas")

    assert row.released_by == "Lucas"


def test_releasing_twice_leaves_the_first_timestamp_alone(session):
    """A double submit or a reloaded page must not rewrite when the decision was
    made — that timestamp is the whole point of the record."""
    client, angle = _mandate(session)
    row = _write(session, client, angle)

    outreach.release(session, row, released_by="Lucas")
    first, first_by = row.released_at, row.released_by
    outreach.release(session, row, released_by="jemand anderes")

    assert row.released_at == first
    assert row.released_by == first_by


def test_two_overlapping_releases_keep_the_first_stamp(tmp_path):
    """FastAPI runs these sync routes in a threadpool: a double-click gives two
    requests their own sessions, and both read the row unreleased. Only a guard
    the database re-checks at write time lets exactly one of them stamp it —
    an in-Python ``if row.released_at`` check passes in both."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    try:
        Base.metadata.create_all(engine)
        make = sessionmaker(bind=engine, expire_on_commit=False)
        with make() as setup:
            client, angle = _mandate(setup)
            row_id = _write(setup, client, angle).id
        with make() as s1, make() as s2:
            # Both requests load the row before either commits: the stale read
            # that defeats any application-level idempotency check.
            r1, r2 = s1.get(Outreach, row_id), s2.get(Outreach, row_id)

            outreach.release(s1, r1, released_by="erste")
            outreach.release(s2, r2, released_by="zweite")

            with make() as check:
                final = check.get(Outreach, row_id)
                assert final.released_by == "erste"
                assert final.released_at == r1.released_at
            # The losing request reads the winner's record back rather than
            # believing its own stale copy.
            assert r2.released_by == "erste"
    finally:
        engine.dispose()


def test_releasing_stores_the_contact_when_the_book_has_one(session):
    client, angle = _mandate(session)
    session.add(Contact(name="Jason Nelson", outlet="Börsen-Zeitung", email="jn@bz.de"))
    session.commit()
    row = _write(session, client, angle)

    outreach.release(session, row)

    stored = session.scalars(select(Contact)).one()
    assert row.contact_id == stored.id


def test_releasing_leaves_the_contact_null_rather_than_guessing(session):
    """No entry, no link. The letter names its own recipient in ``journalist``
    and ``outlet``; a guessed link would be read as one a human made."""
    client, angle = _mandate(session)
    row = _write(session, client, angle)

    outreach.release(session, row)

    assert row.contact_id is None


# --- The outcome -----------------------------------------------------------------


def test_recording_an_outcome_sets_the_state_the_note_and_the_time(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)

    outreach.record_outcome(
        session, row, OutreachState.ANTWORT, "Will die Auswertung sehen."
    )

    assert row.state == OutreachState.ANTWORT
    assert row.outcome_note == "Will die Auswertung sehen."
    assert row.outcome_at is not None


def test_the_note_is_stored_as_typed(session):
    """The one text on this row a person wrote. The house rule on dashes polices
    generated prose; rewriting what a human typed would be a different thing."""
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)
    typed = "Rückruf am Donnerstag — will die Zahlen vorab, ohne Sperrfrist"

    outreach.record_outcome(session, row, OutreachState.ANTWORT, typed)

    assert row.outcome_note == typed


def test_an_outcome_on_a_letter_nobody_released_is_refused(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)

    with pytest.raises(ValueError, match="freigegeben"):
        outreach.record_outcome(session, row, OutreachState.ANTWORT, "hat geantwortet")

    assert row.state == OutreachState.ENTWURF
    assert row.outcome_at is None
    assert row.outcome_note == ""


def test_a_state_that_is_not_an_outcome_is_refused(session):
    """``entwurf`` is where a letter starts and ``raus`` is what releasing does.
    Neither is a thing that came back."""
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)

    for not_an_outcome in (OutreachState.ENTWURF, OutreachState.RAUS):
        with pytest.raises(ValueError):
            outreach.record_outcome(session, row, not_an_outcome)

    assert row.state == OutreachState.RAUS


def test_an_unknown_state_is_refused_rather_than_stored(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)

    with pytest.raises(ValueError, match="Unbekanntes Ergebnis"):
        outreach.record_outcome(session, row, "vielleicht")

    assert row.state == OutreachState.RAUS


# --- Silence, which nobody enters ------------------------------------------------


def test_a_letter_released_today_is_not_silent(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)

    assert outreach.is_silent(row) is False


def test_a_released_letter_with_no_outcome_goes_silent_after_the_threshold(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)
    row.released_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=SILENT_AFTER_DAYS)
    session.commit()

    assert outreach.is_silent(row) is True
    assert outreach.days_out(row) == SILENT_AFTER_DAYS


def test_a_draft_is_never_silent_however_old(session):
    """Silence is about a letter that went out. One that never did is simply
    unreleased, and calling it silent would invent an event."""
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    row.generated_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)
    session.commit()

    assert outreach.is_silent(row) is False
    assert outreach.days_out(row) == 0


def test_a_letter_with_an_outcome_is_not_silent(session):
    client, angle = _mandate(session)
    row = _write(session, client, angle)
    outreach.release(session, row)
    row.released_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)
    outreach.record_outcome(session, row, OutreachState.ABSAGE, "Kein Thema für sie.")

    assert outreach.is_silent(row) is False


# --- A released letter is frozen -------------------------------------------------


def test_redrafting_a_draft_still_overwrites_it(session):
    """Unchanged behaviour while nothing has left the house: two drafts at the
    same journalist are two attempts, not two pitches."""
    client, angle = _mandate(session)
    _write(session, client, angle, message="Erster Versuch.")
    _write(session, client, angle, message="Zweiter Versuch.")

    rows = session.scalars(select(Outreach)).all()
    assert len(rows) == 1
    assert rows[0].message == "Zweiter Versuch."


def test_redrafting_after_a_release_makes_a_new_row(session):
    client, angle = _mandate(session)
    released = _write(session, client, angle, message="Der Brief, der rausging.")
    outreach.release(session, released)

    _write(session, client, angle, message="Der neue Versuch.")

    rows = session.scalars(select(Outreach).order_by(Outreach.id)).all()
    assert len(rows) == 2
    assert rows[1].state == OutreachState.ENTWURF


def test_the_released_letter_keeps_its_own_subject_and_message(session):
    """The record of what a journalist received cannot be overwritten by a text
    nobody sent — that is the data loss this rule exists to stop."""
    client, angle = _mandate(session)
    released = _write(session, client, angle, message="Der Brief, der rausging.")
    outreach.release(session, released)
    released_id, subject = released.id, released.subject

    _write(session, client, angle, message="Der neue Versuch.")
    session.expire_all()
    frozen = session.get(Outreach, released_id)

    assert frozen.message == "Der Brief, der rausging."
    assert frozen.subject == subject
    assert frozen.state == OutreachState.RAUS


def test_a_release_landing_in_stores_read_write_window_is_not_overwritten(
    session, monkeypatch
):
    """``store`` reads the draft, then writes. A release can commit in between —
    the rewrite job finishing while the consultant clicks the button — and the
    released text must survive that: the freeze has to hold at the database's
    write, not in the copy the read returned. Simulated here by handing ``store``
    a read that still claims the released row is a draft."""
    client, angle = _mandate(session)
    released = _write(session, client, angle, message="Der Brief, der rausging.")
    outreach.release(session, released)

    stale_read = types.SimpleNamespace(first=lambda: released)
    monkeypatch.setattr(session, "scalars", lambda *a, **k: stale_read)
    redraft = PersonalMessage(
        subject="Neuer Betreff", message="Der neue Versuch.", hook="weil",
        brain_version=brain.version(session),
    )
    stored = outreach.store(session, client, angle, redraft, _TARGET)
    monkeypatch.undo()

    session.expire_all()
    frozen = session.get(Outreach, released.id)
    assert frozen.message == "Der Brief, der rausging."
    assert frozen.state == OutreachState.RAUS
    # The redraft was not lost either — it became a new row beside the record.
    assert stored.id != released.id
    assert stored.message == "Der neue Versuch."
    assert stored.state == OutreachState.ENTWURF


def test_a_second_release_and_redraft_only_touches_the_newest_draft(session):
    client, angle = _mandate(session)
    outreach.release(session, _write(session, client, angle, message="Erster."))
    _write(session, client, angle, message="Zweiter.")
    _write(session, client, angle, message="Dritter.")

    messages = [
        row.message
        for row in session.scalars(select(Outreach).order_by(Outreach.id)).all()
    ]
    assert messages == ["Erster.", "Dritter."]


# --- The routes ------------------------------------------------------------------


def test_the_release_route_records_the_release(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        client_id, row_id = client.id, row.id

    resp = web.post(
        f"/client/{client_id}/outreach/{row_id}/release", follow_redirects=False
    )

    assert resp.status_code == 303
    with factory() as session:
        stored = session.get(Outreach, row_id)
        assert stored.state == OutreachState.RAUS
        assert stored.released_by == "mensch"


def test_the_outcome_route_stores_what_was_typed(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        outreach.release(session, row)
        client_id, row_id = client.id, row.id

    resp = web.post(
        f"/client/{client_id}/outreach/{row_id}/outcome",
        data={"state": "antwort", "note": "Rückruf am Donnerstag"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    with factory() as session:
        stored = session.get(Outreach, row_id)
        assert stored.state == OutreachState.ANTWORT
        assert stored.outcome_note == "Rückruf am Donnerstag"


def test_an_outcome_on_an_unreleased_letter_is_a_400_and_changes_nothing(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        client_id, row_id = client.id, row.id

    resp = web.post(
        f"/client/{client_id}/outreach/{row_id}/outcome",
        data={"state": "antwort", "note": "hat geantwortet"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    with factory() as session:
        stored = session.get(Outreach, row_id)
        assert stored.state == OutreachState.ENTWURF
        assert stored.outcome_note == ""
        assert stored.outcome_at is None


def test_a_letter_belonging_to_another_mandate_is_refused(factory, web):
    """A letter id is a small integer in a URL, and releasing someone else's
    pitch must not be one guess away."""
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        other = Client(name="Beta GmbH")
        session.add(other)
        session.commit()
        other_id, row_id = other.id, row.id

    resp = web.post(
        f"/client/{other_id}/outreach/{row_id}/release", follow_redirects=False
    )

    assert resp.status_code == 404
    with factory() as session:
        assert session.get(Outreach, row_id).state == OutreachState.ENTWURF


def test_a_cross_site_form_post_cannot_mint_a_release(factory, web):
    """The ledger's claim is "a human released this". A hostile page auto-submitting
    a form POST rides the browser's cached Basic-auth credentials — but the browser
    also names that page in ``Origin``, and a foreign name is refused. The same
    POST from the app's own page still passes."""
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        client_id, row_id = client.id, row.id

    forged = web.post(
        f"/client/{client_id}/outreach/{row_id}/release",
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert forged.status_code == 403
    with factory() as session:
        assert session.get(Outreach, row_id).released_at is None

    own_page = web.post(
        f"/client/{client_id}/outreach/{row_id}/release",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )

    assert own_page.status_code == 303
    with factory() as session:
        assert session.get(Outreach, row_id).released_at is not None


# --- The card --------------------------------------------------------------------


def _badges(html: str) -> list[str]:
    """Every state badge on the page, in order."""
    return re.findall(r'class="state__badge[^"]*">([^<]+)<', html)


def test_a_draft_card_shows_exactly_one_state_and_offers_the_release(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        client_id, row_id = client.id, row.id

    body = web.get(f"/client/{client_id}/advice").text

    assert _badges(body) == ["Entwurf"]
    assert f"/outreach/{row_id}/release" in body
    # The card says out loud that pressing it sends nothing: this build has no
    # mailbox, and a button that read as "send" would be a lie about that.
    assert "Nichts geht von hier aus raus." in body


def test_a_released_card_shows_verschickt_and_its_trail(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        outreach.release(session, row, released_by="Lucas")
        client_id, row_id = client.id, row.id

    body = web.get(f"/client/{client_id}/advice").text

    assert _badges(body) == ["Verschickt"]
    assert "Freigegeben am" in body
    assert "von Lucas freigegeben und verschickt" in body
    # The release control is gone once there is a release to show, so a second
    # click cannot restate a decision that is already on the record.
    assert f"/outreach/{row_id}/release" not in body


def test_a_letter_with_an_outcome_shows_that_state_and_its_note(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        outreach.release(session, row)
        outreach.record_outcome(
            session, row, OutreachState.VEROEFFENTLICHT, "Zahl im Stück vom 16.08."
        )
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    assert _badges(body) == ["Veröffentlicht"]
    # Under the badge as prose, not only prefilled into the form: the note is
    # what a reader of the card needs, and a form value is not readable.
    assert '<span class="trail__note">Zahl im Stück vom 16.08.</span>' in body
    assert "Ergebnis eingetragen" in body


def test_a_silent_letter_is_marked_beside_its_state_not_instead_of_it(factory, web):
    """Silence is "verschickt" plus time. A badge that replaced the release would
    claim the letter had never gone out."""
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        outreach.release(session, row)
        row.released_at = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=SILENT_AFTER_DAYS + 1
        )
        session.commit()
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    assert _badges(body) == ["Verschickt", "Ohne Reaktion"]
    assert f"seit {SILENT_AFTER_DAYS + 1} Tagen still" in body


def test_the_outcome_form_is_only_offered_once_there_is_something_to_answer(
    factory, web
):
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        client_id, row_id = client.id, row.id

    assert f"/outreach/{row_id}/outcome" not in web.get(f"/client/{client_id}/advice").text

    with factory() as session:
        outreach.release(session, session.get(Outreach, row_id))

    assert f"/outreach/{row_id}/outcome" in web.get(f"/client/{client_id}/advice").text


def test_the_outcome_form_preselects_no_outcome_on_a_fresh_release(factory, web):
    """On a letter in "raus" no option matches the state, and the browser would
    quietly select the first one — a stray "Eintragen" click would then record a
    reply nobody had. The placeholder must hold the selection until an outcome
    is chosen on purpose, and hand it over once one is recorded."""
    with factory() as session:
        client, angle = _mandate(session)
        row = _write(session, client, angle)
        outreach.release(session, row)
        client_id, row_id = client.id, row.id

    body = web.get(f"/client/{client_id}/advice").text

    assert '<option value="" disabled selected>' in body
    assert 'selected>Antwort' not in body

    with factory() as session:
        outreach.record_outcome(
            session, session.get(Outreach, row_id), OutreachState.ANTWORT, "kam zurück"
        )

    body = web.get(f"/client/{client_id}/advice").text

    assert '<option value="" disabled>' in body
    assert 'selected>Antwort' in body


def test_the_card_chrome_translates(factory, web):
    """A mixed-language card is the failure the i18n suite exists to catch, and
    every string on this strip is chrome."""
    from newspulse import i18n

    with factory() as session:
        client, angle = _mandate(session)
        _write(session, client, angle)
        client_id = client.id

    web.cookies.set(i18n.COOKIE_NAME, "en")
    body = web.get(f"/client/{client_id}/advice").text

    assert _badges(body) == ["Draft"]
    assert "Released and sent" in body
    assert "not released yet" in body


def test_the_released_trail_translates_and_never_shows_the_mensch_token(factory, web):
    """The trail is chrome too. Two failures guarded here: the stored default
    "mensch" leaking onto the card (client_profile's filled_by never shows it),
    and a named release glued from fragments into English words in German word
    order ("by Lucas released and sent")."""
    from newspulse import i18n

    with factory() as session:
        client, angle = _mandate(session)
        outreach.release(session, _write(session, client, angle))
        client_id = client.id

    web.cookies.set(i18n.COOKIE_NAME, "en")
    body = web.get(f"/client/{client_id}/advice").text

    assert "Released and sent" in body
    assert "mensch" not in body

    with factory() as session:
        client, angle = _mandate(session)
        outreach.release(session, _write(session, client, angle), released_by="Lucas")
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    assert "released and sent by Lucas" in body
    assert "by Lucas released" not in body


def test_every_state_label_has_an_english_entry():
    """A new ``OutreachState`` must not silently render as its German value."""
    from newspulse import i18n

    for state in OutreachState:
        label = outreach.STATE_LABELS[state]
        assert i18n.translate(label, "en") != label, label


# --- The migration ---------------------------------------------------------------


def _alembic_config() -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return cfg


def _redirect_database(tmp_path, monkeypatch) -> Path:
    """Point alembic's env.py at a throwaway file for one test."""
    from newspulse import config

    db_path = tmp_path / "ledger.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    return db_path


def test_upgrade_head_on_an_empty_database_creates_the_ledger_columns(
    tmp_path, monkeypatch
):
    db_path = _redirect_database(tmp_path, monkeypatch)

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("outreach")}
        assert {
            "contact_id",
            "state",
            "released_at",
            "released_by",
            "outcome_at",
            "outcome_note",
        } <= columns
    finally:
        engine.dispose()


def test_letters_written_before_the_ledger_read_back_as_drafts(tmp_path, monkeypatch):
    """They were written when releasing did not exist, so none of them can
    honestly claim a human sent it."""
    db_path = _redirect_database(tmp_path, monkeypatch)
    cfg = _alembic_config()
    command.upgrade(cfg, "0017_client_facts")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO clients (name, aliases, industry, country, keywords, "
                    "alert_topics, active, created_at) VALUES ('Alpha AG', '[]', "
                    "'Neobroker', 'DE', '[]', '[]', 1, '2026-08-01 06:10:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO angles (client_id, generated_at, subject, message, "
                    "context, credibility, thesis, overclaim, statements, article_ids) "
                    "VALUES (1, '2026-08-01 06:10:00', 's', 'm', 'c', '', '', '', "
                    "'[]', '[]')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO outreach (angle_id, client_id, generated_at, "
                    "journalist, outlet, subject, message, hook) VALUES (1, 1, "
                    "'2026-08-01 06:20:00', 'Jason Nelson', 'Börsen-Zeitung', "
                    "'Betreff', 'Der alte Brief.', 'weil')"
                )
            )

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT state, released_at, released_by, outcome_note, contact_id, "
                    "message FROM outreach"
                )
            ).one()
        assert row[0] == OutreachState.ENTWURF.value
        assert row[1] is None
        assert row[2] == ""
        assert row[3] == ""
        assert row[4] is None
        # And the letter itself survived the rebuild the new foreign key forces.
        assert row[5] == "Der alte Brief."
    finally:
        engine.dispose()


def test_the_state_column_refuses_a_value_outside_the_set(tmp_path, monkeypatch):
    """The closed set is a CHECK in the database, not only a Python enum: a raw
    INSERT is exactly how a stray string gets into a column like this."""
    import sqlalchemy.exc

    db_path = _redirect_database(tmp_path, monkeypatch)
    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO outreach (angle_id, client_id, generated_at, "
                        "message, state) VALUES (1, 1, '2026-08-01 06:20:00', 'm', "
                        "'vielleicht')"
                    )
                )
    finally:
        engine.dispose()


def test_recording_a_release_over_an_objection_costs_a_second_click(session, web=None):
    """The cross-check is advisory and the consultant may overrule it — that is
    the design, and blocking the button would put a model in charge of a
    decision a person is accountable for.

    What the flag has to buy is that overruling is a decision rather than an
    oversight. The release button used to sit there enabled beside "Zweitmodell
    rät ab", one click from writing "wer hat das freigegeben" into the ledger
    over a standing objection.
    """
    from fastapi.testclient import TestClient

    from newspulse.web.app import create_app, get_db

    client, angle = _mandate(session)
    row = _write(session, client, angle)
    row.review = "Die genannten Artikel sind nicht belegt."
    row.reviewed_by = "gemini-2.5-flash"
    row.review_ok = False
    session.commit()

    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    body = TestClient(app).get(f"/client/{client.id}/advice").text

    card = body.split(f"outreach/{row.id}/release", 1)[0][-1400:]
    assert "Ja, eintragen" in body, "the confirmation exists"
    assert "<details" in card, "and the button is behind it"
