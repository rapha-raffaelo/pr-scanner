"""The contact book as a relationship file: one journalist, everything sent.

DEC-2 put the outreach history here rather than on an "Ausgang" page, and the
consequence that matters is in the query: the file is read **across mandates**.
A journalist is a relationship the agency holds, not a row in a client's
workspace, and "sind wir bei ihr diesen Monat schon mit etwas anderem gewesen"
has no answer inside one mandate. So the mandate is named on every line instead,
and the tests below pin that down first.

Two other things are pinned harder than they look:

* **Drafts never appear.** The file is a record of what left the house. A draft
  reached nobody, and a timeline that shows one is claiming a contact that never
  happened — the same reason ``pitch`` only marks a target off a *released*
  letter.
* **The tallies are counted off the very rows the timeline renders.** Four
  numbers over a list is an invitation to compute them twice and have them
  disagree, and the number is the part that gets quoted.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import contacts, outreach, pitch
from newspulse.models import (
    SILENT_AFTER_DAYS,
    Angle,
    Article,
    Base,
    Client,
    Outreach,
    OutreachState,
    TopicHit,
)
from newspulse.web.app import create_app, get_db

#: Mid-morning UTC on purpose: every rendered date goes through the reader's
#: zone, and a timestamp near midnight would make an assertion about "13.08."
#: depend on which side of the date line the configured zone sits.
_NOW = dt.datetime(2026, 8, 19, 9, 0, tzinfo=dt.UTC)


def _enable_foreign_keys(dbapi_connection, _record) -> None:
    """What ``newspulse.db.make_engine`` does for every SQLite connection.

    Without it SQLite ignores ``ON DELETE SET NULL``, and the deleted-contact
    test below would be passing against behaviour the real database does not
    have — the worst kind of green.
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


# --- Seeding --------------------------------------------------------------------


def _mandate(session, name: str) -> tuple[Client, Angle]:
    """One client with one impulse to hang letters off."""
    client = Client(
        name=name, aliases=[], industry="Energie",
        keywords=["Netzentgelte"], alert_topics=[], country="DE",
    )
    session.add(client)
    session.flush()
    angle = Angle(
        client_id=client.id, generated_at=_NOW,
        subject="Netzentgelte: wer zahlt den Ausbau",
        message="Zwei Absätze Positionierung.",
        context="Mehrere Meldungen zur Anschlussdauer.",
        thesis="Der Kostentreiber ist die Anschlussdauer.",
        article_ids=[],
    )
    session.add(angle)
    session.commit()
    return client, angle


def _letter(
    session,
    client: Client,
    angle: Angle,
    *,
    journalist: str = "Marlene Kühn",
    outlet: str = "Handelsblatt",
    subject: str = "Anschlussdauer statt Netzentgelt",
    contact_id: int | None = None,
    released_at: dt.datetime | None = None,
    state: OutreachState = OutreachState.ENTWURF,
    outcome_at: dt.datetime | None = None,
    outcome_note: str = "",
) -> Outreach:
    """One letter in whatever state the test needs.

    Built as a row rather than driven through ``draft``/``store``/``release``:
    those are OUT-01's and have their own tests, and every case here turns on a
    *timestamp* — newest-first ordering, the 14-day silence, the 90-day pitch
    window — which a real release would stamp with the wall clock.
    """
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist=journalist,
        outlet=outlet,
        subject=subject,
        message="Sehr geehrte Frau Kühn, wir haben Zahlen aus 340 Verfahren.",
        contact_id=contact_id,
        state=state,
        released_at=released_at,
        released_by="mensch" if released_at else "",
        outcome_at=outcome_at,
        outcome_note=outcome_note,
    )
    session.add(row)
    session.commit()
    return row


# --- The file: across mandates, newest first, released only ---------------------


def test_the_file_holds_letters_from_every_mandate_newest_first(session):
    """The whole reason DEC-2 chose the contact book: one journalist, one
    timeline, whichever client the agency was writing for."""
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    nordlicht, energie_angle = _mandate(session, "Nordlicht Energie")
    vermont, robotik_angle = _mandate(session, "Vermont Robotics")
    older = _letter(
        session, vermont, robotik_angle, contact_id=kuehn.id,
        subject="Zulassungsdauer für Chirurgierobotik",
        released_at=_NOW - dt.timedelta(days=94), state=OutreachState.ABSAGE,
    )
    newer = _letter(
        session, nordlicht, energie_angle, contact_id=kuehn.id,
        released_at=_NOW - dt.timedelta(days=6), state=OutreachState.RAUS,
    )

    history = outreach.history_for_contact(session, kuehn.id)

    assert [entry.letter.id for entry in history] == [newer.id, older.id]
    assert [entry.mandate for entry in history] == [
        "Nordlicht Energie",
        "Vermont Robotics",
    ]


def test_a_draft_never_appears_in_the_file(session):
    """A draft reached nobody. Putting it on a relationship timeline would claim
    a contact that never happened."""
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    client, angle = _mandate(session, "Nordlicht Energie")
    _letter(session, client, angle, contact_id=kuehn.id, subject="Nie rausgegangen")
    released = _letter(
        session, client, angle, contact_id=kuehn.id,
        released_at=_NOW - dt.timedelta(days=2), state=OutreachState.RAUS,
    )

    history = outreach.history_for_contact(session, kuehn.id)

    assert [entry.letter.id for entry in history] == [released.id]


def _interleaved(session) -> tuple[int, Outreach, Outreach]:
    """The ordinary week the nested layout got wrong: a reply to a pitch from
    July, typed *after* a second pitch had already gone out last week."""
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    client, angle = _mandate(session, "Nordlicht Energie")
    older = _letter(
        session, client, angle, contact_id=kuehn.id,
        subject="Der teure Weg ans Netz",
        released_at=_NOW - dt.timedelta(days=40),
        state=OutreachState.ANTWORT,
        outcome_at=_NOW - dt.timedelta(days=1),
        outcome_note="Will die Auswertung sehen, Rückruf Donnerstag.",
    )
    newer = _letter(
        session, client, angle, contact_id=kuehn.id,
        subject="Anschlussdauer statt Netzentgelt",
        released_at=_NOW - dt.timedelta(days=5), state=OutreachState.RAUS,
    )
    return kuehn.id, older, newer


def test_a_late_outcome_sorts_above_a_newer_letters_release(session):
    """Newest first means *every* line, not every letter.

    The two kinds of line carry different timestamps, so drawing an outcome under
    the letter it answers files yesterday's reply below last week's release — the
    page would read 14.08. → 18.08. → 10.07. and the AC asks for one descending
    order.
    """
    contact_id, older, newer = _interleaved(session)

    events = outreach.timeline(outreach.history_for_contact(session, contact_id))

    assert [(event.kind, event.letter.id) for event in events] == [
        (outreach.TimelineKind.OUTCOME, older.id),
        (outreach.TimelineKind.RELEASE, newer.id),
        (outreach.TimelineKind.RELEASE, older.id),
    ]
    assert [event.at for event in events] == sorted(
        (event.at for event in events), reverse=True
    )


def test_the_rendered_timeline_runs_newest_first(web, factory):
    with factory() as session:
        contact_id, _, _ = _interleaved(session)

    body = web.get("/contacts", params={"id": contact_id}).text

    # The reply typed yesterday, then last week's letter, then July's.
    assert body.index("18.08.2026") < body.index("14.08.2026")
    assert body.index("14.08.2026") < body.index("10.07.2026")


def test_another_journalists_letters_stay_out_of_this_file(session):
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    reh = contacts.save(session, name="Tobias Reh", outlet="Süddeutsche Zeitung")
    client, angle = _mandate(session, "Nordlicht Energie")
    _letter(
        session, client, angle, contact_id=reh.id,
        journalist="Tobias Reh", outlet="Süddeutsche Zeitung",
        released_at=_NOW - dt.timedelta(days=1), state=OutreachState.RAUS,
    )

    assert outreach.history_for_contact(session, kuehn.id) == []


def test_a_letter_released_before_the_contact_existed_lands_in_the_file(session):
    """The order the pitch list actually invites.

    "Kontakt hinterlegen" sits under a byline the book does *not* have, so the
    letter goes out first and the contact is recorded afterwards. The link on the
    row is written at release and nothing else ever writes it, so without the
    repair in ``contacts.save`` the file would show an empty timeline for a
    journalist the agency demonstrably wrote to.
    """
    client, angle = _mandate(session, "Nordlicht Energie")
    letter = _letter(
        session, client, angle,
        released_at=_NOW - dt.timedelta(days=3), state=OutreachState.RAUS,
    )
    assert letter.contact_id is None

    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")

    history = outreach.history_for_contact(session, kuehn.id)
    assert [entry.letter.id for entry in history] == [letter.id]
    assert outreach.released_count_by_contact(session)[kuehn.id] == 1


def test_the_repair_leaves_a_draft_and_a_foreign_masthead_alone(session):
    """Matched no wider than :func:`contacts.find` matches.

    A draft reached nobody and gets its link when it is released. A letter to the
    same name at a different named masthead is somebody else until the consultant
    says otherwise — a wrong link here files another journalist's letters in this
    file, which is worse than a missing one.
    """
    client, angle = _mandate(session, "Nordlicht Energie")
    draft = _letter(session, client, angle)
    elsewhere = _letter(
        session, client, angle, outlet="Süddeutsche Zeitung",
        released_at=_NOW - dt.timedelta(days=2), state=OutreachState.RAUS,
    )
    no_masthead = _letter(
        session, client, angle, outlet="",
        released_at=_NOW - dt.timedelta(days=4), state=OutreachState.RAUS,
    )

    kuehn = contacts.save(session, name="marlene kühn", outlet="handelsblatt")

    linked = {entry.letter.id for entry in outreach.history_for_contact(session, kuehn.id)}
    # Cased differently on both sides and still the same person; the letter with
    # no masthead of its own is hers too, the way ``find`` falls back to the name.
    assert linked == {no_masthead.id}
    session.refresh(draft)
    session.refresh(elsewhere)
    assert draft.contact_id is None
    assert elsewhere.contact_id is None


def test_the_repair_never_steals_a_letter_from_another_contact(session):
    """Only rows with no link at all are claimed: a release that already resolved
    to somebody stays where the ledger put it."""
    reh = contacts.save(session, name="Tobias Reh", outlet="Süddeutsche Zeitung")
    client, angle = _mandate(session, "Nordlicht Energie")
    theirs = _letter(
        session, client, angle, contact_id=reh.id,
        journalist="Tobias Reh", outlet="Süddeutsche Zeitung",
        released_at=_NOW - dt.timedelta(days=1), state=OutreachState.RAUS,
    )

    contacts.save(session, name="Tobias Reh", outlet="Süddeutsche Zeitung", email="t@sz.example")

    session.refresh(theirs)
    assert theirs.contact_id == reh.id


# --- The four tallies -----------------------------------------------------------


def test_the_tallies_count_the_rows_the_timeline_shows(session):
    """Four numbers over a list, computed from that list rather than beside it —
    the number is the part that gets quoted."""
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    client, angle = _mandate(session, "Nordlicht Energie")
    for state, age in (
        (OutreachState.ANTWORT, 5),
        (OutreachState.VEROEFFENTLICHT, 40),
        (OutreachState.ABSAGE, 60),
        (OutreachState.RAUS, SILENT_AFTER_DAYS + 1),
    ):
        _letter(
            session, client, angle, contact_id=kuehn.id, state=state,
            released_at=_NOW - dt.timedelta(days=age),
            outcome_at=_NOW - dt.timedelta(days=age - 1)
            if state != OutreachState.RAUS
            else None,
        )

    history = outreach.history_for_contact(session, kuehn.id)
    tallies = outreach.tally(history, now=_NOW)

    assert tallies.anschreiben == len(history) == 4
    assert tallies.antworten == 1
    assert tallies.veroeffentlicht == 1
    # Derived, not stored: "raus" plus more than fourteen days with nothing
    # recorded. The three that carry an outcome are not silent whatever their age.
    assert tallies.still == 1


def test_a_letter_still_inside_the_silence_window_is_not_counted_as_silent(session):
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    client, angle = _mandate(session, "Nordlicht Energie")
    _letter(
        session, client, angle, contact_id=kuehn.id, state=OutreachState.RAUS,
        released_at=_NOW - dt.timedelta(days=SILENT_AFTER_DAYS - 1),
    )

    history = outreach.history_for_contact(session, kuehn.id)

    assert outreach.tally(history, now=_NOW).still == 0


# --- The roster -----------------------------------------------------------------


def test_the_roster_counts_released_letters_per_contact(session):
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    reh = contacts.save(session, name="Tobias Reh", outlet="Süddeutsche Zeitung")
    client, angle = _mandate(session, "Nordlicht Energie")
    for _ in range(2):
        _letter(
            session, client, angle, contact_id=kuehn.id,
            released_at=_NOW - dt.timedelta(days=3), state=OutreachState.RAUS,
        )
    _letter(session, client, angle, contact_id=reh.id, journalist="Tobias Reh")

    counts = outreach.released_count_by_contact(session)

    assert counts[kuehn.id] == 2
    # A draft is not a contact, so the number beside a name never inflates.
    assert reh.id not in counts


def test_the_roster_shows_the_count_and_keeps_filtering(web, factory):
    with factory() as session:
        kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
        contacts.save(session, name="Sven Kaltenbach", outlet="Baugewerbe Magazin")
        client, angle = _mandate(session, "Nordlicht Energie")
        _letter(
            session, client, angle, contact_id=kuehn.id,
            released_at=_NOW - dt.timedelta(days=3), state=OutreachState.RAUS,
        )

    whole = web.get("/contacts").text
    assert "Marlene Kühn" in whole
    assert "Sven Kaltenbach" in whole
    assert '<span class="book__n">1</span>' in whole

    for params in ({"q": "Handelsblatt"}, {"search": "Handelsblatt"}):
        narrowed = web.get("/contacts", params=params).text
        assert "Marlene Kühn" in narrowed, params
        assert "Sven Kaltenbach" not in narrowed, params


def test_opening_a_file_keeps_the_filter_and_filtering_keeps_the_file(web, factory):
    """Two bits of state in one query string. Clicking a name must not throw the
    filter away, and filtering must not close the file that is open — a book
    narrowed to one masthead is exactly when you go looking at one of its names."""
    with factory() as session:
        kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
        contacts.save(session, name="Sven Kaltenbach", outlet="Baugewerbe Magazin")
        contact_id = kuehn.id

    narrowed = web.get("/contacts", params={"q": "Handelsblatt"}).text
    assert f"/contacts?id={contact_id}&amp;q=Handelsblatt" in narrowed

    opened = web.get("/contacts", params={"id": contact_id, "q": "Handelsblatt"}).text
    assert f'<input type="hidden" name="id" value="{contact_id}">' in opened
    assert "Sven Kaltenbach" not in opened


# --- The rendered page ----------------------------------------------------------


def _seed_page(factory) -> tuple[int, int]:
    """One journalist with a released letter and a hand-typed reply on it."""
    with factory() as session:
        kuehn = contacts.save(
            session, name="Marlene Kühn", outlet="Handelsblatt",
            email="m.kuehn@handelsblatt.example", beat="Energie und Industrie",
        )
        client, angle = _mandate(session, "Nordlicht Energie")
        _letter(
            session, client, angle, contact_id=kuehn.id,
            subject="Anschlussdauer statt Netzentgelt: Zahlen aus 340 Anschlüssen",
            released_at=_NOW - dt.timedelta(days=6),
            state=OutreachState.ANTWORT,
            outcome_at=_NOW - dt.timedelta(days=4),
            outcome_note="Will die Auswertung sehen, Rückruf Donnerstag.",
        )
        return kuehn.id, client.id


def test_the_page_names_the_date_the_mandate_and_the_subject(web, factory):
    contact_id, _ = _seed_page(factory)

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "13.08.2026" in body
    assert "Nordlicht Energie" in body
    assert "Anschlussdauer statt Netzentgelt: Zahlen aus 340 Anschlüssen" in body
    assert "Anschreiben verschickt" in body


def test_a_hand_typed_entry_is_marked_apart_from_one_the_system_recorded(web, factory):
    """Both lines say where they came from, and they do not look alike: one is a
    sentence somebody typed, the other a timestamp taken when a button was
    pressed, and an auditor reading this page has to be able to tell."""
    contact_id, _ = _seed_page(factory)

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "von Hand eingetragen" in body
    assert "bei der Freigabe festgehalten" in body
    # Two different chips, not one class used twice.
    assert 'class="tl__src tl__src--hand"' in body
    assert 'class="tl__src"' in body
    assert "Will die Auswertung sehen, Rückruf Donnerstag." in body


def test_a_contact_with_no_released_letters_shows_the_file_and_says_so(web, factory):
    """Not a blank pane and not an error: the file is there, and the timeline
    says in words that nothing has gone out yet."""
    with factory() as session:
        kaltenbach = contacts.save(
            session, name="Sven Kaltenbach", outlet="Baugewerbe Magazin"
        )
        contact_id = kaltenbach.id

    resp = web.get("/contacts", params={"id": contact_id})

    assert resp.status_code == 200
    assert "Sven Kaltenbach" in resp.text
    assert "Noch kein Anschreiben freigegeben" in resp.text


def test_an_id_that_matches_nothing_renders_the_book_rather_than_an_error(web, factory):
    with factory() as session:
        contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")

    resp = web.get("/contacts", params={"id": 9999})

    assert resp.status_code == 200
    assert "Marlene Kühn" in resp.text


def test_an_unparseable_id_renders_the_book_too(web, factory):
    """A truncated or copied link is not an error page.

    Declared as ``int`` the parameter was rejected by the framework before the
    route ran, so ``/contacts?id=`` — one character short of a working link —
    answered with a raw 422 JSON document instead of the book.
    """
    _seed_page(factory)

    for stale in ("", "abc", "3.5", " "):
        resp = web.get(f"/contacts?id={stale}")
        assert resp.status_code == 200, (stale, resp.status_code)
        assert "Kontaktbuch" in resp.text, stale


def test_saving_an_edit_returns_to_the_open_file(web, factory):
    """The form carries both bits of state back: the file being edited and the
    filter the roster was narrowed to. Bare ``/contacts`` closes the first and
    drops the second, which is what a missing ``redirect_to`` fell back to."""
    contact_id, _ = _seed_page(factory)
    back = f"/contacts?id={contact_id}&q=Handelsblatt"

    page = web.get(f"/contacts?id={contact_id}&edit={contact_id}&q=Handelsblatt").text
    form = page.split('class="contact-form"')[1].split("</form>")[0]
    assert f'name="redirect_to" value="/contacts?id={contact_id}&amp;q=Handelsblatt"' in form

    posted = web.post(
        "/contacts",
        data={
            "contact_id": str(contact_id), "name": "Marlene Kühn",
            "outlet": "Handelsblatt", "email": "", "phone": "", "beat": "",
            "notes": "", "redirect_to": back,
        },
        follow_redirects=False,
    )

    assert posted.headers["location"] == back


def test_a_backslash_redirect_is_refused(web, factory):
    """``/\\evil.example`` passes a plain ``//`` check and is protocol-relative by
    the time the browser follows it."""
    _seed_page(factory)

    posted = web.post(
        "/contacts",
        data={"name": "Marlene Kühn", "redirect_to": "/\\evil.example"},
        follow_redirects=False,
    )

    assert posted.headers["location"] == "/contacts"


def test_the_file_pane_translates_its_chrome(web, factory):
    """A two-language interface fails as a *mixed* one, so the new pane's labels
    have to switch with the rest of the chrome."""
    from newspulse import i18n

    contact_id, _ = _seed_page(factory)
    web.cookies.set(i18n.COOKIE_NAME, "en")

    body = web.get("/contacts", params={"id": contact_id}).text

    assert "History" in body
    assert "Letters" in body
    assert "typed by hand" in body
    assert "Verlauf" not in body


# --- A deleted contact ----------------------------------------------------------


def test_deleting_a_contact_leaves_the_released_letters_readable(web, factory):
    """The link is the contact book's, the record is the letter's. Losing the
    first must never cost the second — which is why the recipient is written into
    ``journalist``/``outlet`` on the row itself and not only pointed at."""
    contact_id, client_id = _seed_page(factory)
    with factory() as session:
        letter_id = session.scalars(select(Outreach.id)).one()

    web.post(f"/contacts/{contact_id}/delete", follow_redirects=False)

    with factory() as session:
        row = session.get(Outreach, letter_id)
        assert row is not None
        assert row.contact_id is None
        assert row.journalist == "Marlene Kühn"
        assert row.outlet == "Handelsblatt"
        assert row.released_at is not None
        assert row.subject.startswith("Anschlussdauer statt Netzentgelt")

    # And it is still readable where the letter lives: the card names its
    # recipient from its own fields, with no contact row left to ask.
    body = web.get(f"/client/{client_id}/advice").text
    assert "Marlene Kühn" in body
    assert "Handelsblatt" in body


# --- The mark on a pitch target --------------------------------------------------


def _pitchable(session) -> tuple[Client, Angle]:
    """A mandate whose radar carries one bylined story, so ``targets_for`` has
    somebody to propose."""
    client = Client(
        name="Nordlicht Energie", aliases=[], industry="Energie",
        keywords=["Netzentgelte"], alert_topics=[], country="DE",
    )
    session.add(client)
    session.flush()
    article = Article(
        title="Netzentgelte steigen erneut", url="https://ex.de/n1",
        source="Handelsblatt", author="Marlene Kühn",
        published_at=_NOW - dt.timedelta(days=2),
        fetched_at=_NOW - dt.timedelta(days=2),
        summary_text=None, language="de", title_hash="n1000001",
    )
    session.add(article)
    session.flush()
    session.add(TopicHit(article_id=article.id, client_id=client.id, found_at=_NOW))
    angle = Angle(
        client_id=client.id, generated_at=_NOW,
        subject="Netzentgelte: wer zahlt den Ausbau",
        message="m", context="c", thesis="t", article_ids=[article.id],
    )
    session.add(angle)
    session.commit()
    return client, angle


def _kuehn(targets: list[pitch.PitchTarget]) -> pitch.PitchTarget:
    return next(t for t in targets if t.journalist == "Marlene Kühn")


def test_a_target_already_written_to_about_this_angle_carries_the_date(session):
    """The list stops proposing the same subject to the same person twice — with
    the date, because the second approach may still be right and the reader is
    the one deciding."""
    client, angle = _pitchable(session)
    sent = _NOW - dt.timedelta(days=7)
    _letter(session, client, angle, released_at=sent, state=OutreachState.RAUS)

    target = _kuehn(pitch.targets_for(session, client, angle, now=_NOW))

    assert target.already_pitched_at == sent


def test_a_letter_older_than_the_window_leaves_the_target_unmarked(session):
    client, angle = _pitchable(session)
    _letter(
        session, client, angle, state=OutreachState.RAUS,
        released_at=_NOW - dt.timedelta(days=pitch.ALREADY_PITCHED_DAYS + 1),
    )

    target = _kuehn(pitch.targets_for(session, client, angle, now=_NOW))

    assert target.already_pitched_at is None


def test_a_draft_to_the_same_recipient_leaves_the_target_unmarked(session):
    """A draft reached nobody, so it cannot be a reason to hold back."""
    client, angle = _pitchable(session)
    _letter(session, client, angle)

    target = _kuehn(pitch.targets_for(session, client, angle, now=_NOW))

    assert target.already_pitched_at is None


def test_a_letter_for_a_different_angle_leaves_this_target_unmarked(session):
    """The mark is about *this* subject: the same journalist written to about
    something else last week is a normal thing to do."""
    client, angle = _pitchable(session)
    other = Angle(
        client_id=client.id, generated_at=_NOW, subject="Ein anderes Thema",
        message="m", context="c", thesis="t", article_ids=[],
    )
    session.add(other)
    session.commit()
    _letter(
        session, client, other, state=OutreachState.RAUS,
        released_at=_NOW - dt.timedelta(days=3),
    )

    target = _kuehn(pitch.targets_for(session, client, angle, now=_NOW))

    assert target.already_pitched_at is None


def test_the_recipient_is_matched_however_the_name_was_cased(session):
    """The feed writes "Marlene Kühn" and the letter may carry "marlene kühn" —
    the book matches case-insensitively, and so must the mark."""
    client, angle = _pitchable(session)
    sent = _NOW - dt.timedelta(days=4)
    _letter(
        session, client, angle, journalist="marlene kühn", outlet="handelsblatt",
        released_at=sent, state=OutreachState.RAUS,
    )

    target = _kuehn(pitch.targets_for(session, client, angle, now=_NOW))

    assert target.already_pitched_at == sent


def test_the_same_person_at_another_masthead_spelling_still_marks_the_target(session):
    """The link the ledger wrote beats the two strings on the row.

    One masthead arrives from two feeds as "Handelsblatt" and "Handelsblatt
    Online", so a mark that keys only on (journalist, outlet) misses the letter
    that demonstrably went to this contact — and the second identical approach,
    the one this mark exists to prevent, goes out unwarned.
    """
    client, angle = _pitchable(session)
    kuehn = contacts.save(session, name="Marlene Kühn", outlet="Handelsblatt")
    sent = _NOW - dt.timedelta(days=4)
    row = _letter(
        session, client, angle, journalist="Marlene Kühn",
        outlet="Handelsblatt Online", contact_id=kuehn.id,
        released_at=sent, state=OutreachState.RAUS,
    )

    target = _kuehn(pitch.targets_for(session, client, angle, now=_NOW))

    assert row.contact_id == kuehn.id
    assert target.contact_id == kuehn.id
    assert target.already_pitched_at == sent


def test_the_pitch_list_shows_the_date_it_already_went_out(web, factory):
    with factory() as session:
        client, angle = _pitchable(session)
        _letter(
            session, client, angle, state=OutreachState.RAUS,
            released_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=7),
        )
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    assert "schon angeschrieben am" in body
