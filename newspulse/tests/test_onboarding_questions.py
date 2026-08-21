"""The kick-off questionnaire: the questions, the answer store and the page.

Three properties carry this feature and all three are tested here.

*Every question declares a target.* A questionnaire whose answers go nowhere in
particular is a form, so a twenty-first question added without saying where it
goes must fail the suite rather than quietly render.

*The three states stay three.* Answered, deliberately passed over, and never
asked are different facts about a mandate's foundation, and only the middle one
means "we asked and there is nothing to get".

*Nothing here adopts anything.* The failure being guarded against is silent
adoption: an onboarding that writes a client's own words into the guide is how a
sentence nobody approved becomes the rule every later text is checked against. So
a full questionnaire is run against a byte-for-byte snapshot of ``client_facts``,
``comms_guide`` and the ``clients`` row.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import i18n, onboarding
from newspulse.models import Base, Client, ClientFact, OnboardingAnswer
from newspulse.web.app import create_app, get_db


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


def _client(session) -> Client:
    c = Client(name="Vermont Robotics", aliases=[], industry="Medizintechnik",
               country="DE", keywords=[], alert_topics=[])
    session.add(c)
    session.commit()
    return c


# --- The question set is data, and it has to hold together ---------------------


def test_every_question_declares_at_least_one_target():
    """The fixture test the whole feature rests on: a question that feeds nothing
    is a form field, and this suite is where that gets caught."""
    for question in onboarding.QUESTIONS:
        assert question.feeds, f"{question.key} declares no target"
        for feed in question.feeds:
            assert isinstance(feed.target, onboarding.Target), question.key


def _question(key: str, *, feeds=(onboarding.Feed(onboarding.Target.NOGO),)):
    return onboarding.Question(
        key, "unternehmen", f"Frage {key}?", "Hilfe.",
        onboarding.InputKind.ZEILE, feeds=feeds,
    )


def test_a_question_set_with_a_targetless_question_is_refused(monkeypatch):
    """The same rule as the fixture test above, enforced where a broken set is
    built rather than where it is read — so it never gets as far as a page."""
    broken = (_question("leer", feeds=()),)
    monkeypatch.setattr(onboarding, "QUESTIONS", broken)
    monkeypatch.setattr(onboarding, "QUESTIONS_BY_KEY", {q.key: q for q in broken})

    with pytest.raises(ValueError, match="declares no target"):
        onboarding._check_question_set()


def test_a_question_set_with_a_duplicate_key_is_refused(monkeypatch):
    """Two questions sharing a key would overwrite each other's stored answer."""
    doubled = (_question("satz"), _question("satz"))
    monkeypatch.setattr(onboarding, "QUESTIONS", doubled)
    monkeypatch.setattr(onboarding, "QUESTIONS_BY_KEY", {q.key: q for q in doubled})

    with pytest.raises(ValueError, match="share a key"):
        onboarding._check_question_set()


def test_a_question_in_an_unknown_section_is_refused(monkeypatch):
    stray = (_question("streuner"),)
    monkeypatch.setattr(onboarding, "QUESTIONS", (
        onboarding.Question(
            "streuner", "gibt-es-nicht", "Frage?", "Hilfe.",
            onboarding.InputKind.ZEILE, feeds=stray[0].feeds,
        ),
    ))
    monkeypatch.setattr(
        onboarding, "QUESTIONS_BY_KEY", {q.key: q for q in onboarding.QUESTIONS}
    )

    with pytest.raises(ValueError, match="no known section"):
        onboarding._check_question_set()


def test_there_are_twenty_questions_in_five_sections():
    assert len(onboarding.QUESTIONS) == 20
    assert onboarding.TOTAL == 20
    assert len(onboarding.SECTIONS) == 5
    grouped = onboarding.by_section()
    assert [len(qs) for _, qs in grouped] == [4, 6, 4, 4, 2]
    assert sum(len(qs) for _, qs in grouped) == onboarding.TOTAL


def test_question_keys_are_unique_and_every_one_sits_in_a_known_section():
    keys = [q.key for q in onboarding.QUESTIONS]
    assert len(set(keys)) == len(keys)
    for question in onboarding.QUESTIONS:
        assert question.section in onboarding.SECTIONS_BY_KEY, question.key


def test_a_no_go_becomes_and_a_field_gets_filled():
    """The verb on the page follows the target, so a rule never reads as a slot."""
    assert onboarding.QUESTIONS_BY_KEY["nie_satz"].verb == "Wird"
    assert onboarding.QUESTIONS_BY_KEY["satz"].verb == "Füllt"


def test_every_question_and_section_string_has_an_english_entry():
    """A German question on an English page is the mixed UI the i18n rule exists
    to prevent — and unlike a stored summary, a question is chrome."""
    known = set(i18n.known_keys())
    for section in onboarding.SECTIONS:
        for text in (section.title, section.short, section.intro):
            assert text in known, text
    for question in onboarding.QUESTIONS:
        assert question.text in known, question.text
        assert question.help in known, question.help
        for feed in question.feeds:
            assert feed.target.value in known, feed.target.value
            if feed.slot:
                assert feed.slot in known, feed.slot


# --- The answer store ----------------------------------------------------------


def test_saving_an_answer_stores_it_with_its_author(session):
    client = _client(session)
    row = onboarding.save_answer(session, client, "satz", "  Roboterarme.  ")

    assert row is not None
    assert row.value == "Roboterarme."
    assert row.answered_by == onboarding.ANSWERED_BY_DEFAULT
    assert row.skipped is False
    assert onboarding.answers(session, client.id)["satz"].value == "Roboterarme."


def test_an_answer_survives_a_fresh_session(factory):
    """Stored as it is given, not held until some final submit."""
    with factory() as writing:
        client = _client(writing)
        onboarding.save_answer(writing, client, "satz", "Roboterarme.")

    with factory() as reading:
        assert onboarding.answers(reading, client.id)["satz"].value == "Roboterarme."


def test_re_answering_overwrites_in_place_and_moves_the_timestamp(session):
    client = _client(session)
    first = onboarding.save_answer(session, client, "satz", "Erste Fassung.")
    # Backdated rather than slept on: the assertion is about the write moving the
    # timestamp, and a test that waits a second to prove it is a test nobody runs.
    # The comparison has to be against this captured value and not against the
    # row's own attribute — `save_answer` hands back the same identity-mapped
    # object, so `second.answered_at > second.answered_at - 1h` would hold even if
    # the write never touched the timestamp at all.
    backdated = first.answered_at - dt.timedelta(hours=1)
    first.answered_at = backdated
    session.commit()

    second = onboarding.save_answer(session, client, "satz", "Zweite Fassung.")

    assert second.id == first.id
    assert second.value == "Zweite Fassung."
    assert second.answered_at > backdated
    assert session.scalar(
        select(func.count()).select_from(OnboardingAnswer)
        .where(OnboardingAnswer.client_id == client.id)
    ) == 1


def test_the_schema_itself_refuses_a_second_row_for_one_question(session):
    """``save_answer`` upserts, but the guarantee is in the table: one mandate has
    one answer per question, and no code path may grow a pile of versions."""
    client = _client(session)
    session.add(OnboardingAnswer(client_id=client.id, key="satz", value="Eins"))
    session.commit()
    session.add(OnboardingAnswer(client_id=client.id, key="satz", value="Zwei"))

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_an_unknown_key_stores_nothing(session):
    client = _client(session)

    assert onboarding.save_answer(session, client, "gibt-es-nicht", "x") is None
    assert onboarding.skip(session, client, "gibt-es-nicht") is None
    assert onboarding.answers(session, client.id) == {}


def test_skipping_is_stored_and_is_not_the_same_as_unanswered(session):
    client = _client(session)
    onboarding.skip(session, client, "schweigen")

    stored = onboarding.answers(session, client.id)
    assert stored["schweigen"].skipped is True
    assert stored["schweigen"].value == ""
    # The third state: never asked leaves no row at all.
    assert "zahlen" not in stored


def test_skipping_an_answered_question_drops_the_value(session):
    """A skipped question still showing yesterday's text would be neither state."""
    client = _client(session)
    onboarding.save_answer(session, client, "schweigen", "Laufendes Verfahren.")
    onboarding.skip(session, client, "schweigen")

    row = onboarding.answers(session, client.id)["schweigen"]
    assert row.skipped is True
    assert row.value == ""


def test_answering_a_skipped_question_spends_the_skip(session):
    client = _client(session)
    onboarding.skip(session, client, "schweigen")
    onboarding.save_answer(session, client, "schweigen", "Doch: das Kartellverfahren.")

    row = onboarding.answers(session, client.id)["schweigen"]
    assert row.skipped is False
    assert row.value == "Doch: das Kartellverfahren."


def test_clearing_an_answer_returns_the_question_to_unanswered(session):
    """An empty row would keep claiming the question had been dealt with."""
    client = _client(session)
    onboarding.save_answer(session, client, "satz", "Roboterarme.")

    assert onboarding.save_answer(session, client, "satz", "") is None
    assert onboarding.answers(session, client.id) == {}


def test_a_list_answer_grows_one_entry_at_a_time(session):
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Verhoeven, CEO")
    onboarding.add_entry(session, client, "sprecher", "Milan Roth, CTO")

    row = onboarding.answers(session, client.id)["sprecher"]
    assert onboarding.entries(row.value) == [
        "Dr. Anna Verhoeven, CEO", "Milan Roth, CTO",
    ]


def test_removing_one_entry_leaves_its_siblings(session):
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Verhoeven, CEO")
    onboarding.add_entry(session, client, "sprecher", "Milan Roth, CTO")

    onboarding.remove_entry(session, client, "sprecher", 0)

    assert onboarding.entries(
        onboarding.answers(session, client.id)["sprecher"].value
    ) == ["Milan Roth, CTO"]


def test_removing_the_last_entry_returns_the_question_to_unanswered(session):
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Verhoeven, CEO")

    onboarding.remove_entry(session, client, "sprecher", 0)

    assert "sprecher" not in onboarding.answers(session, client.id)


def test_an_out_of_range_entry_removal_changes_nothing(session):
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Verhoeven, CEO")

    assert onboarding.remove_entry(session, client, "sprecher", 7) is None
    assert onboarding.entries(
        onboarding.answers(session, client.id)["sprecher"].value
    ) == ["Dr. Anna Verhoeven, CEO"]


def test_an_entry_cannot_be_added_to_a_prose_question(session):
    client = _client(session)

    assert onboarding.add_entry(session, client, "satz", "etwas") is None
    assert onboarding.answers(session, client.id) == {}


# --- Completeness --------------------------------------------------------------


def test_completeness_counts_answered_plus_skipped_and_names_the_remainder(session):
    client = _client(session)
    onboarding.save_answer(session, client, "satz", "Roboterarme.")
    onboarding.save_answer(session, client, "zielgruppe", "Klinikeinkauf.")
    onboarding.skip(session, client, "schweigen")

    progress = onboarding.completeness(session, client.id)

    assert progress.answered == 2
    assert progress.skipped == 1
    assert progress.settled == 3
    assert progress.total == 20
    assert progress.remaining == 17
    assert progress.percent == 15


def test_an_untouched_questionnaire_is_all_remainder(session):
    client = _client(session)
    progress = onboarding.completeness(session, client.id)

    assert progress.settled == 0
    assert progress.remaining == 20
    assert progress.percent == 0
    assert progress.started is False
    assert progress.last_answered_at is None


def test_completeness_reports_each_section_separately(session):
    client = _client(session)
    for key in ("satz", "sprecher", "wettbewerber", "zielgruppe"):
        onboarding.save_answer(session, client, key, "Antwort.")
    onboarding.skip(session, client, "nie_satz")

    by_key = {
        line.section.key: line
        for line in onboarding.completeness(session, client.id).sections
    }

    assert by_key["unternehmen"].settled == 4
    assert by_key["unternehmen"].state is onboarding.Progress.FERTIG
    assert by_key["sagen"].settled == 1
    assert by_key["sagen"].state is onboarding.Progress.TEILWEISE
    assert by_key["ziele"].state is onboarding.Progress.OFFEN


def test_one_mandates_answers_do_not_count_towards_another(session):
    one = _client(session)
    other = Client(name="Nordlicht Energie", aliases=[], keywords=[], alert_topics=[])
    session.add(other)
    session.commit()

    onboarding.save_answer(session, one, "satz", "Roboterarme.")

    assert onboarding.completeness(session, other.id).settled == 0
    assert onboarding.answers(session, other.id) == {}


# --- The page ------------------------------------------------------------------


def test_the_page_renders_every_question_in_its_section(web, session):
    client = _client(session)
    body = web.get(f"/client/{client.id}/kickoff").text

    for question in onboarding.QUESTIONS:
        assert f'id="q-{question.key}"' in body, question.key
        assert question.text in body, question.key
    for section in onboarding.SECTIONS:
        assert f'id="s-{section.key}"' in body, section.key
        assert section.title in body, section.key


def test_every_rendered_question_says_what_it_feeds(web, session):
    """"Füllt Profil · Geschäftsfeld", not a field name. The declaration is the
    reason this is a questionnaire and not a form."""
    client = _client(session)
    body = web.get(f"/client/{client.id}/kickoff").text

    assert body.count('class="q__feeds"') == onboarding.TOTAL
    assert "<b>Profil · Geschäftsfeld</b>" in body
    assert "<b>Guide · Kernbotschaft</b>" in body
    assert "<b>No-Go</b>" in body
    # Every target any question names has to appear somewhere on the page.
    for label in {f.target.value for q in onboarding.QUESTIONS for f in q.feeds}:
        assert f"<b>{label}" in body, label


def test_each_question_posts_to_its_own_route(web, session):
    """Answerable independently: twenty forms, not one submit at the end."""
    client = _client(session)
    body = web.get(f"/client/{client.id}/kickoff").text

    for question in onboarding.QUESTIONS:
        assert f'action="/client/{client.id}/kickoff/{question.key}"' in body


def test_an_answer_is_stored_and_survives_a_reload(web, session):
    client = _client(session)

    saved = web.post(
        f"/client/{client.id}/kickoff/satz",
        data={"value": "Roboterarme, die den Chirurgen führen."},
        follow_redirects=False,
    )

    assert saved.status_code == 303
    assert saved.headers["location"] == f"/client/{client.id}/kickoff#q-satz"
    assert "Roboterarme, die den Chirurgen führen." in web.get(
        f"/client/{client.id}/kickoff"
    ).text


def test_an_htmx_save_returns_only_that_question_and_the_rail(web, session):
    """The whole point of the partial: a save must not touch the other nineteen
    fields, because one of them may be half-typed."""
    client = _client(session)

    resp = web.post(
        f"/client/{client.id}/kickoff/satz",
        data={"value": "Roboterarme."},
        headers={"hx-request": "true"},
    )

    assert resp.status_code == 200
    assert 'id="q-satz"' in resp.text
    assert 'hx-swap-oob="true"' in resp.text
    assert 'id="q-nie_satz"' not in resp.text


def test_the_page_states_how_many_remain_in_words(web, session):
    client = _client(session)
    web.post(f"/client/{client.id}/kickoff/satz", data={"value": "Roboterarme."})

    body = web.get(f"/client/{client.id}/kickoff").text

    assert "19" in body
    assert "Fragen sind noch offen" in body
    assert "19<span> / 20</span>" not in body
    assert "1<span> / 20</span>" in body


def test_skipping_through_the_page_shows_a_state_of_its_own(web, session):
    client = _client(session)

    web.post(f"/client/{client.id}/kickoff/schweigen/skip")
    body = web.get(f"/client/{client.id}/kickoff").text

    assert "q--skipped" in body
    assert "übergangen" in body
    # And the way back out of an accidental skip.
    assert f'action="/client/{client.id}/kickoff/schweigen/clear"' in body


def test_clearing_through_the_page_reopens_the_question(web, session):
    client = _client(session)
    web.post(f"/client/{client.id}/kickoff/schweigen/skip")

    web.post(f"/client/{client.id}/kickoff/schweigen/clear")

    with_session = onboarding.answers(session, client.id)
    assert "schweigen" not in with_session


def test_a_list_question_shows_its_entries_as_chips(web, session):
    client = _client(session)
    web.post(f"/client/{client.id}/kickoff/sprecher",
             data={"value": "Dr. Anna Verhoeven, CEO"})
    web.post(f"/client/{client.id}/kickoff/sprecher",
             data={"value": "Milan Roth, CTO"})

    body = web.get(f"/client/{client.id}/kickoff").text

    assert body.count('class="chipx"') == 2
    assert "Dr. Anna Verhoeven, CEO" in body

    web.post(f"/client/{client.id}/kickoff/sprecher/remove", data={"index": "0"})
    body = web.get(f"/client/{client.id}/kickoff").text
    assert "Dr. Anna Verhoeven, CEO" not in body
    assert "Milan Roth, CTO" in body


def test_the_kickoff_tab_is_reachable_from_the_other_client_pages(web, session):
    client = _client(session)

    body = web.get(f"/client/{client.id}/profil").text

    assert f'href="/client/{client.id}/kickoff"' in body
    assert "Kickoff" in body


def test_the_page_renders_in_english(web, session):
    client = _client(session)
    web.cookies.set(i18n.COOKIE_NAME, "en")

    body = web.get(f"/client/{client.id}/kickoff").text

    assert "Which sentence should we never write about you?" in body
    assert "<b>Profile · Line of business</b>" in body
    assert "Welchen Satz sollen wir über Sie nie schreiben?" not in body


def test_an_unknown_question_is_a_404_not_a_silent_no_op(web, session):
    client = _client(session)

    assert web.post(
        f"/client/{client.id}/kickoff/gibt-es-nicht", data={"value": "x"}
    ).status_code == 404


def test_the_questionnaire_of_a_missing_mandate_is_a_404(web):
    assert web.get("/client/999/kickoff").status_code == 404


# --- The guard: nothing here adopts anything -----------------------------------


def _snapshot(row) -> dict[str, str]:
    """Every column of one row as text, so the comparison is genuinely by value.

    ``repr`` rather than the attributes themselves: the JSON list columns hand
    back a mutable object, and a snapshot holding a reference to it would compare
    equal to itself no matter what happened in between.
    """
    return {c.key: repr(getattr(row, c.key)) for c in inspect(type(row)).column_attrs}


def _foundation(session, client_id: int) -> tuple:
    """A byte-for-byte snapshot of everything an answer must *not* touch.

    The whole ``clients`` row rather than a hand-picked few of its columns —
    including ``comms_guide`` — so a future column is covered without anyone
    remembering to add it here.
    """
    client = session.get(Client, client_id)
    session.refresh(client)
    facts = session.scalars(
        select(ClientFact).where(ClientFact.client_id == client_id)
        .order_by(ClientFact.key)
    ).all()
    return (_snapshot(client), [_snapshot(f) for f in facts])


def test_a_full_questionnaire_writes_to_nothing_but_its_own_table(web, session):
    """The failure this feature is built to avoid: an answer silently becoming a
    fact, a no-go or a guide. Adoption is ONB-02, and it takes a human saying yes.
    """
    client = _client(session)
    client.comms_guide = "Positionierung: führend in der Chirurgierobotik."
    session.add(ClientFact(client_id=client.id, key="ceo", value="Aus dem Netz",
                           source_url="https://example.de", filled_by="gemini"))
    session.commit()
    before = _foundation(session, client.id)

    for question in onboarding.QUESTIONS:
        web.post(
            f"/client/{client.id}/kickoff/{question.key}",
            data={"value": f"Antwort auf {question.key}."},
        )

    assert onboarding.completeness(session, client.id).settled == onboarding.TOTAL
    assert _foundation(session, client.id) == before


def test_skipping_the_whole_questionnaire_also_writes_to_nothing_else(web, session):
    client = _client(session)
    before = _foundation(session, client.id)

    for question in onboarding.QUESTIONS:
        web.post(f"/client/{client.id}/kickoff/{question.key}/skip")

    progress = onboarding.completeness(session, client.id)
    assert progress.skipped == onboarding.TOTAL
    assert progress.answered == 0
    assert progress.remaining == 0
    assert _foundation(session, client.id) == before
