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

from newspulse import guide, i18n, onboarding
from newspulse import profile as profiles
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
        # Every optional string the template puts through ``t()`` too: a note or a
        # placeholder added later would otherwise render German on the English
        # page without failing anything here.
        for optional in (question.note, question.placeholder):
            if optional:
                assert optional in known, optional
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


def test_a_save_whose_read_missed_the_winning_row_stores_the_answer_anyway(
    session, monkeypatch
):
    """The losing half of two concurrent first saves of the same question.

    The routes are sync, so FastAPI runs them in a threadpool and two requests
    genuinely interleave: two open tabs, or "Speichern" and "Übergehen" clicked in
    turn, which htmx does not serialise because they are different elements. The
    loser reads before the winner's insert commits, sees nothing, and inserts
    straight into ``uq_onboarding_answers_key``. Unhandled that is an HTTP 500 and
    a sentence transcribed from a call that exists nowhere else.

    The missed read is injected rather than raced for: the interleaving is the
    whole behaviour under test, and a barrier between two threads would prove it
    on a quiet machine and flake on a busy one. The constraint doing the rejecting
    is the real one.
    """
    client = _client(session)
    onboarding.save_answer(session, client, "satz", "Was der andere Tab getippt hat.")

    real_stored = onboarding._stored
    reads: list[int] = []

    def _misses_the_first_read(*args):
        reads.append(1)
        return None if len(reads) == 1 else real_stored(*args)

    monkeypatch.setattr(onboarding, "_stored", _misses_the_first_read)

    row = onboarding.save_answer(session, client, "satz", "Meine eigene Antwort.")

    assert row is not None
    assert row.value == "Meine eigene Antwort."
    assert session.scalar(
        select(func.count()).select_from(OnboardingAnswer)
        .where(OnboardingAnswer.client_id == client.id)
    ) == 1


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


def test_the_same_entry_submitted_twice_stays_one_entry(session):
    """A double submit is not a second spokesperson. Two identical chips cannot be
    told apart, and the delete button on either one removes whichever index it
    happens to carry."""
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Verhoeven, CEO")
    onboarding.add_entry(session, client, "sprecher", "  dr. anna verhoeven, ceo ")

    row = onboarding.answers(session, client.id)["sprecher"]
    assert onboarding.entries(row.value) == ["Dr. Anna Verhoeven, CEO"]


def test_an_entry_holding_a_newline_stays_one_entry(session):
    """Entries are separated by newlines, so one holding a newline would come back
    as two on the next render: a name nobody typed as a name."""
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Anna Verhoeven\nMilan Roth")

    row = onboarding.answers(session, client.id)["sprecher"]
    assert onboarding.entries(row.value) == ["Anna Verhoeven Milan Roth"]


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


def test_an_emptied_field_does_not_delete_the_stored_answer(web, session):
    """The field saves on blur. Selecting an answer to retype it and then being
    interrupted must not destroy a sentence transcribed from a call."""
    client = _client(session)
    web.post(f"/client/{client.id}/kickoff/satz", data={"value": "Roboterarme."})

    web.post(f"/client/{client.id}/kickoff/satz", data={"value": "   "})

    assert onboarding.answers(session, client.id)["satz"].value == "Roboterarme."


def test_an_answered_question_offers_the_deliberate_way_back_to_unanswered(web, session):
    """Deleting stays possible — it just stops being a side effect of a blur."""
    client = _client(session)
    web.post(f"/client/{client.id}/kickoff/satz", data={"value": "Roboterarme."})

    body = web.get(f"/client/{client.id}/kickoff").text
    block = body.split('id="q-satz"')[1].split('id="q-sprecher"')[0]
    assert f'action="/client/{client.id}/kickoff/satz/clear"' in block
    # And the one-click path that would have destroyed the same answer is not
    # offered beside it: skipping drops the value, so from answered the way to
    # passed over runs through the delete button and is two deliberate acts.
    assert f'action="/client/{client.id}/kickoff/satz/skip"' not in block

    web.post(f"/client/{client.id}/kickoff/satz/clear")
    assert "satz" not in onboarding.answers(session, client.id)


def test_a_list_field_is_not_committed_on_blur(web, session):
    """For prose the field *is* the answer, so saving a partial one is the point.
    For a list it is an append box: a half-typed name would become a chip that can
    only be corrected by deleting it and starting over."""
    client = _client(session)
    body = web.get(f"/client/{client.id}/kickoff").text

    block = body.split('id="q-sprecher"')[1].split('id="q-wettbewerber"')[0]
    assert 'hx-trigger="submit"' in block
    prose = body.split('id="q-satz"')[1].split('id="q-sprecher"')[0]
    assert 'hx-trigger="change, submit"' in prose


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


def test_a_malformed_entry_index_changes_nothing_and_still_answers_in_html(web, session):
    """Every failure on this page is a 404 or a no-op, and every response is HTML.
    A missing or non-numeric ``index`` used to be FastAPI's raw 422 JSON body."""
    client = _client(session)
    web.post(f"/client/{client.id}/kickoff/sprecher",
             data={"value": "Dr. Anna Verhoeven, CEO"})

    for bad in ({}, {"index": "abc"}):
        response = web.post(f"/client/{client.id}/kickoff/sprecher/remove", data=bad)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    assert onboarding.entries(
        onboarding.answers(session, client.id)["sprecher"].value
    ) == ["Dr. Anna Verhoeven, CEO"]


def test_the_page_carries_the_placeholders_the_locked_mock_specifies(web, session):
    """The mock's placeholders state the *shape* of the answer where the help line
    states why the question is asked, so dropping them is not a duplicate removed
    but guidance removed."""
    client = _client(session)

    body = web.get(f"/client/{client.id}/kickoff").text

    for expected in (
        "Weitere Person, Rolle, Themen",
        "Unternehmen, und in einem Halbsatz warum",
        "Thema, und ob Schweigen oder eine Sprachregelung gilt",
        "Behauptung, und womit Sie dagegenhalten können",
    ):
        assert f'placeholder="{expected}"' in body, expected


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


# --- The conversion: what the answers are offered as (ONB-02) -------------------
#
# One rule runs through every test below and it is the same one ONB-01 ends on:
# each of these produces an *offer*. A proposal on a profile field, a draft guide,
# a competitor to click — and until somebody clicks, ``client_facts``,
# ``comms_guide`` and the comparison set say exactly what they said before.


def _answer(session, client, key: str, value: str) -> None:
    onboarding.save_answer(session, client, key, value)


def _kickoff(session, client) -> None:
    """A half-answered questionnaire, the way one actually comes back from a call.

    Two sections carry answers and three do not, which is the state the draft has
    to be able to describe rather than fill in.
    """
    _answer(session, client, "satz", "Wir bauen Roboterarme für OP-Säle.")
    _answer(session, client, "zielgruppe", "Klinikleitungen und Chefärzte.")
    _answer(session, client, "nie_satz", "Vermont ersetzt den Chirurgen.")
    _answer(session, client, "zahlen", "Umsatzzahlen werden nie genannt.")
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Klar, CTO, Technik")


def _distilled(text: str = "Positionierung: Chirurgierobotik aus Vermont."):
    """An injected distillation. No test in this file reaches a model."""
    return lambda *args, **kwargs: text


# --- Answers that map to a profile field ---------------------------------------


def test_an_answer_becomes_a_proposal_on_the_field_its_slot_names(session):
    client = _client(session)
    _answer(session, client, "satz", "Wir bauen Roboterarme für OP-Säle.")

    proposals = onboarding.to_proposals(session, client.id)

    assert [(p.key, p.value) for p in proposals] == [
        ("geschaeftsfeld", "Wir bauen Roboterarme für OP-Säle.")
    ]


def test_a_proposal_names_the_questionnaire_as_its_source_and_has_no_url(session):
    """Nobody published the kick-off call. A citation linking nowhere would be a
    worse lie than no link at all."""
    client = _client(session)
    _answer(session, client, "zielgruppe", "Klinikleitungen.")

    proposal = onboarding.to_proposals(session, client.id)[0]

    assert proposal.source_title == onboarding.SOURCE_NAME
    assert proposal.source_url == ""
    assert proposal.filled_by == onboarding.SOURCE_NAME


def test_to_proposals_stores_nothing(session):
    client = _client(session)
    _kickoff(session, client)

    onboarding.to_proposals(session, client.id)

    assert session.query(ClientFact).count() == 0


def test_a_skipped_question_proposes_nothing(session):
    """"Asked, and there is no answer" is not an answer to adopt."""
    client = _client(session)
    onboarding.skip(session, client, "satz")

    assert onboarding.to_proposals(session, client.id) == []


def test_an_unanswered_questionnaire_proposes_nothing(session):
    client = _client(session)

    assert onboarding.to_proposals(session, client.id) == []


def test_two_questions_feeding_one_field_arrive_as_a_single_proposal(session):
    """The profile has one line for spokespeople, and the consultant wants both
    halves of what the client said on it."""
    client = _client(session)
    onboarding.add_entry(session, client, "sprecher", "Dr. Anna Klar, CTO, Technik")
    _answer(session, client, "interview", "Interviews nur zur Technik, nie zu Preisen.")

    proposals = onboarding.to_proposals(session, client.id)

    assert [p.key for p in proposals] == ["sprecher"]
    assert "Dr. Anna Klar, CTO, Technik" in proposals[0].value
    assert "Interviews nur zur Technik, nie zu Preisen." in proposals[0].value


def test_every_profile_slot_the_questionnaire_names_fills_a_real_field():
    """The fixture test one level below "every question declares a target": a
    question promising "Füllt Profil · Sprecher" against a profile that has no
    such field would be a promise the tool cannot keep."""
    for question in onboarding.QUESTIONS:
        for feed in question.feeds:
            if feed.target is onboarding.Target.PROFIL:
                assert feed.slot in onboarding._PROFILE_SLOTS, question.key
                assert (
                    onboarding._PROFILE_SLOTS[feed.slot] in profiles.FIELDS_BY_KEY
                ), question.key


# --- The draft guide -----------------------------------------------------------


def test_the_draft_is_returned_and_no_guide_is_written(session):
    """The whole posture of this story in one assertion: generating proposes."""
    client = _client(session)
    _kickoff(session, client)

    draft = onboarding.to_guide_draft(session, client, invoke=_distilled())

    assert draft.text
    assert client.comms_guide in (None, "")
    assert session.get(Client, client.id).comms_guide in (None, "")


def test_the_guide_is_written_only_once_the_draft_is_saved(session):
    client = _client(session)
    _kickoff(session, client)
    draft = onboarding.to_guide_draft(session, client, invoke=_distilled())

    guide.save(session, client, draft.text)

    assert session.get(Client, client.id).comms_guide == draft.text


def test_the_draft_carries_every_no_go_verbatim(session):
    """A rule that has been reworded is a different rule, so the client's own
    sentences are in the draft word for word even where the model paraphrased
    them."""
    client = _client(session)
    _kickoff(session, client)

    draft = onboarding.to_guide_draft(
        session,
        client,
        invoke=_distilled("No-Gos: Bitte keine Aussagen über den Ersatz von Ärzten."),
    )

    assert "Vermont ersetzt den Chirurgen." in draft.text
    assert "Umsatzzahlen werden nie genannt." in draft.text
    assert set(draft.verbatim) == {
        "Vermont ersetzt den Chirurgen.",
        "Umsatzzahlen werden nie genannt.",
    }


def test_the_verbatim_block_lists_every_rule_even_ones_already_quoted(session):
    """A complete block, not the leftovers the model happened to miss: whoever
    reads the saved guide has to be able to point at one place and say "these are
    the client's rules, in the client's words"."""
    client = _client(session)
    _answer(session, client, "nie_satz", "Vermont ersetzt den Chirurgen.")
    _answer(session, client, "zahlen", "Umsatzzahlen werden nie genannt.")

    draft = onboarding.to_guide_draft(
        session, client, invoke=_distilled("No-Gos: Vermont ersetzt den Chirurgen.")
    )

    block = draft.text.split(onboarding._NOGO_HEADING)[1]
    assert "Vermont ersetzt den Chirurgen." in block
    assert "Umsatzzahlen werden nie genannt." in block


def test_a_partly_answered_questionnaire_names_the_sections_that_had_no_answer(session):
    """Rather than inventing content for them, which reads exactly like an
    answered section."""
    client = _client(session)
    _kickoff(session, client)

    draft = onboarding.to_guide_draft(session, client, invoke=_distilled())

    assert [s.key for s in draft.missing] == ["ziele", "medien", "zusammenarbeit"]
    for section in draft.missing:
        assert section.title in draft.text


def test_a_fully_answered_questionnaire_names_no_gaps(session):
    client = _client(session)
    for question in onboarding.QUESTIONS:
        _answer(session, client, question.key, f"Antwort auf {question.key}.")

    draft = onboarding.to_guide_draft(session, client, invoke=_distilled())

    assert draft.missing == ()
    assert draft.has_gaps is False


def test_the_distillation_reads_the_questions_and_sees_which_had_no_answer(session):
    """The prompt forbids inventing what is not in the documents. A question
    standing there without an answer is the strongest version of that."""
    client = _client(session)
    _answer(session, client, "satz", "Wir bauen Roboterarme für OP-Säle.")
    seen: list[str] = []

    onboarding.to_guide_draft(
        session, client, invoke=lambda prompt, **kwargs: seen.append(prompt) or "Guide."
    )

    assert "Wir bauen Roboterarme für OP-Säle." in seen[0]
    assert "(nicht beantwortet)" in seen[0]


def test_a_skipped_question_reaches_the_prompt_as_deliberately_unanswered(session):
    client = _client(session)
    _answer(session, client, "satz", "Roboterarme.")
    onboarding.skip(session, client, "nie_satz")
    seen: list[str] = []

    onboarding.to_guide_draft(
        session, client, invoke=lambda prompt, **kwargs: seen.append(prompt) or "Guide."
    )

    assert "übergangen" in seen[0]


def test_an_unanswered_questionnaire_refuses_rather_than_drafting_nothing(session):
    """"No answers yet" and "the model failed" have to stay distinguishable."""
    client = _client(session)

    with pytest.raises(onboarding.ExtractionError):
        onboarding.to_guide_draft(session, client, invoke=_distilled())


def test_the_draft_stays_inside_the_guide_budget_and_keeps_the_rules_whole(session):
    """Something has to give when both halves are long, and it is never the
    client's own sentences."""
    client = _client(session)
    _answer(session, client, "nie_satz", "Vermont ersetzt den Chirurgen.")

    draft = onboarding.to_guide_draft(
        session, client, invoke=_distilled("x" * (guide.GUIDE_MAX_CHARS + 500))
    )

    assert len(draft.text) <= guide.GUIDE_MAX_CHARS
    assert "Vermont ersetzt den Chirurgen." in draft.text


# --- The answers as a source of the guide --------------------------------------


def test_the_answers_are_filed_as_a_guide_source(session):
    """So the guide's provenance says it came from the kick-off, alongside the
    uploaded documents."""
    client = _client(session)
    _kickoff(session, client)

    onboarding.to_guide_draft(session, client, invoke=_distilled())

    stored = guide.sources(session, client.id)
    assert [s.filename for s in stored] == [onboarding.SOURCE_NAME]
    assert "Wir bauen Roboterarme für OP-Säle." in stored[0].text


def test_regenerating_replaces_the_kickoff_source_rather_than_piling_up(session):
    """Six near-identical copies of the questionnaire would push the real
    documents out of the distillation's character budget."""
    client = _client(session)
    _kickoff(session, client)
    onboarding.to_guide_draft(session, client, invoke=_distilled())

    _answer(session, client, "wortwahl", "Immer Kundinnen und Kunden, nie User.")
    onboarding.to_guide_draft(session, client, invoke=_distilled())

    stored = guide.sources(session, client.id)
    assert len(stored) == 1
    assert "Immer Kundinnen und Kunden, nie User." in stored[0].text


def test_an_uploaded_document_survives_the_kickoff_source(session):
    """Two ways in, both sources. The brand book is not replaced by the call."""
    client = _client(session)
    _kickoff(session, client)
    guide.store_source(session, client, "markenbuch.pdf", "Wir schreiben sachlich.")

    onboarding.to_guide_draft(session, client, invoke=_distilled())

    assert {s.filename for s in guide.sources(session, client.id)} == {
        "markenbuch.pdf",
        onboarding.SOURCE_NAME,
    }


# --- The named competitors -----------------------------------------------------


def test_a_named_competitor_is_offered_with_the_reason_beside_it(session):
    client = _client(session)
    _answer(
        session, client, "wettbewerber",
        "Intuitive Surgical, weil sie den Markt definieren",
    )

    named = onboarding.to_rivals(session, client)

    assert [(r.name, r.reason) for r in named] == [
        ("Intuitive Surgical", "weil sie den Markt definieren")
    ]


def test_a_named_competitor_is_never_linked_automatically(session):
    """A wrong competitor does not merely look odd — it lands in the share-of-voice
    arithmetic and quietly changes a number the agency reports."""
    client = _client(session)
    _answer(session, client, "wettbewerber", "Intuitive Surgical, weil sie führen")

    onboarding.to_rivals(session, client)

    assert list(client.competitors) == []
    assert session.query(Client).count() == 1


def test_a_competitor_already_in_the_comparison_set_is_not_offered_again(session):
    client = _client(session)
    rival = Client(name="Intuitive Surgical", aliases=[], keywords=[],
                   alert_topics=[], is_competitor=True)
    session.add(rival)
    session.flush()
    client.competitors.append(rival)
    session.commit()
    _answer(session, client, "wettbewerber", "Intuitive Surgical, weil sie führen")

    assert onboarding.to_rivals(session, client) == []


def test_a_competitor_named_without_a_reason_is_still_offered(session):
    client = _client(session)
    _answer(session, client, "wettbewerber", "Intuitive Surgical")

    named = onboarding.to_rivals(session, client)

    assert [(r.name, r.reason) for r in named] == [("Intuitive Surgical", "")]


def test_the_mandate_itself_is_never_offered_as_its_own_competitor(session):
    client = _client(session)
    _answer(session, client, "wettbewerber", f"{client.name}, aus Versehen genannt")

    assert onboarding.to_rivals(session, client) == []


# --- DEC-2: an answer that contradicts what was researched ---------------------


def test_an_accepted_answer_wins_and_the_researched_value_stays_visible(session):
    """DEC-2 option A. The person who runs the company outranks the page written
    about it, and the disagreement stays legible instead of being erased."""
    client = _client(session)
    profiles.save(session, client, "geschaeftsfeld", "Medizintechnik-Zulieferer",
                  source_url="https://beispiel.de/ueber-uns", source_title="Website",
                  filled_by="gemini-2.5-flash")

    profiles.save(session, client, "geschaeftsfeld", "Wir bauen Roboterarme.",
                  source_title=onboarding.SOURCE_NAME,
                  filled_by=onboarding.SOURCE_NAME, supersede=True)

    fact = profiles.stored(session, client.id)["geschaeftsfeld"]
    assert fact.value == "Wir bauen Roboterarme."
    assert fact.filled_by == onboarding.SOURCE_NAME
    assert fact.superseded_value == "Medizintechnik-Zulieferer"
    assert fact.superseded_filled_by == "gemini-2.5-flash"
    assert fact.superseded_source_url == "https://beispiel.de/ueber-uns"
    assert fact.is_disputed is True


def test_an_answer_that_agrees_with_the_stored_value_records_no_disagreement(session):
    """A "die Recherche sagte" line under a value nothing ever contradicted would
    be provenance theatre."""
    client = _client(session)
    profiles.save(session, client, "sitz", "Bern", filled_by="gemini-2.5-flash")

    profiles.save(session, client, "sitz", "Bern", supersede=True,
                  filled_by=onboarding.SOURCE_NAME)

    assert profiles.stored(session, client.id)["sitz"].is_disputed is False


def test_a_field_nothing_stood_in_is_filled_without_a_supersession(session):
    client = _client(session)

    profiles.save(session, client, "sitz", "Bern", supersede=True,
                  filled_by=onboarding.SOURCE_NAME)

    assert profiles.stored(session, client.id)["sitz"].is_disputed is False


def test_dropping_the_old_value_ends_the_disagreement(session):
    """Kept so the reader can see the web said something else, not so it stays on
    the page forever."""
    client = _client(session)
    profiles.save(session, client, "sitz", "Zug", filled_by="gemini-2.5-flash")
    profiles.save(session, client, "sitz", "Bern", supersede=True,
                  filled_by=onboarding.SOURCE_NAME)

    profiles.forget_superseded(session, client.id, "sitz")

    fact = profiles.stored(session, client.id)["sitz"]
    assert fact.value == "Bern"
    assert fact.is_disputed is False


# --- The pages -----------------------------------------------------------------


def test_the_profile_offers_the_kickoff_answer_and_names_its_source(factory, web):
    with factory() as session:
        client = _client(session)
        _answer(session, client, "satz", "Wir bauen Roboterarme für OP-Säle.")
        client_id = client.id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Wir bauen Roboterarme für OP-Säle." in body
    assert onboarding.SOURCE_NAME in body
    with factory() as session:
        assert session.query(ClientFact).count() == 0, "offered, not adopted"


def test_a_kickoff_answer_is_offered_even_where_the_consultant_filled_the_field(
    factory, web
):
    """The reverse of the research rule, on purpose: the machine may never
    overrule the consultant, but the client contradicting the file is exactly the
    case this page exists to surface."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "geschaeftsfeld", "Medizintechnik")
        _answer(session, client, "satz", "Wir bauen Roboterarme für OP-Säle.")
        client_id = client.id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Wir bauen Roboterarme für OP-Säle." in body


def test_an_answer_that_says_what_the_field_already_says_is_not_offered(factory, web):
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "geschaeftsfeld", "Roboterarme.")
        _answer(session, client, "satz", "Roboterarme.")
        client_id = client.id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Vorschläge für das Profil" not in body


def test_accepting_through_the_page_shows_both_values_with_their_provenance(
    factory, web
):
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "geschaeftsfeld", "Medizintechnik-Zulieferer",
                      source_url="https://beispiel.de/ueber-uns",
                      source_title="Website", filled_by="gemini-2.5-flash")
        _answer(session, client, "satz", "Wir bauen Roboterarme für OP-Säle.")
        client_id = client.id

    web.post(f"/client/{client_id}/profil/accept", data={"key": "geschaeftsfeld"},
             follow_redirects=False)
    body = web.get(f"/client/{client_id}/profil").text

    with factory() as session:
        fact = profiles.stored(session, client_id)["geschaeftsfeld"]
    assert fact.value == "Wir bauen Roboterarme für OP-Säle."
    assert fact.superseded_value == "Medizintechnik-Zulieferer"
    # Both values on the page, each with where it came from.
    assert "Medizintechnik-Zulieferer" in body
    assert "https://beispiel.de/ueber-uns" in body
    assert onboarding.SOURCE_NAME in body


def test_the_page_drops_the_old_value_when_asked(factory, web):
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "sitz", "Zug", filled_by="gemini-2.5-flash")
        profiles.save(session, client, "sitz", "Bern", supersede=True,
                      filled_by=onboarding.SOURCE_NAME)
        client_id = client.id

    web.post(f"/client/{client_id}/profil/sitz/forget", follow_redirects=False)

    with factory() as session:
        assert profiles.stored(session, client_id)["sitz"].is_disputed is False


def test_the_profile_states_how_much_of_the_kickoff_is_answered(factory, web):
    with factory() as session:
        client = _client(session)
        _kickoff(session, client)
        client_id = client.id

    body = web.get(f"/client/{client_id}/profil").text

    assert f"5/{onboarding.TOTAL}" in body
    assert "Fragen aus dem Kickoff beantwortet oder übergangen" in body


def test_a_mandate_with_no_questionnaire_says_so_on_its_profile(factory, web):
    with factory() as session:
        client_id = _client(session).id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Kein Fragebogen beantwortet" in body


def test_the_client_list_carries_the_completeness_line(factory, web):
    with factory() as session:
        client = _client(session)
        _kickoff(session, client)

    body = web.get("/clients").text

    assert f"5/{onboarding.TOTAL}" in body
    assert "Fundament" in body


def test_the_client_list_says_plainly_when_there_is_no_questionnaire(factory, web):
    with factory() as session:
        _client(session)

    body = web.get("/clients").text

    assert "kein Fragebogen" in body


def test_the_guide_page_offers_a_draft_from_the_kickoff(factory, web):
    with factory() as session:
        client = _client(session)
        _kickoff(session, client)
        client_id = client.id

    body = web.get(f"/client/{client_id}/guide").text

    assert f"/client/{client_id}/guide/kickoff" in body
    assert "Entwurf aus dem Kickoff" in body


def test_the_guide_page_says_when_there_is_nothing_to_draft_from(factory, web):
    with factory() as session:
        client_id = _client(session).id

    body = web.get(f"/client/{client_id}/guide").text

    assert f"/client/{client_id}/guide/kickoff" not in body
    assert "noch unbeantwortet" in body


def test_the_drafted_guide_is_editable_and_unsaved_until_it_is_submitted(
    factory, web, monkeypatch
):
    """The acceptance criterion in one pass: generate, and the guide is still
    empty; the draft comes back in a field the consultant can change first."""
    monkeypatch.setattr(
        onboarding, "invoke_with_fallback",
        lambda *args, **kwargs: "Positionierung: Chirurgierobotik.",
    )
    with factory() as session:
        client = _client(session)
        _kickoff(session, client)
        client_id = client.id

    body = web.post(f"/client/{client_id}/guide/kickoff").text

    assert "Positionierung: Chirurgierobotik." in body
    assert "Vermont ersetzt den Chirurgen." in body, "the no-go, verbatim"
    assert '<textarea class="guide__area proposal__area"' in body
    with factory() as session:
        assert session.get(Client, client_id).comms_guide in (None, "")


def test_the_rivals_page_offers_what_the_client_named(factory, web):
    with factory() as session:
        client = _client(session)
        _answer(session, client, "wettbewerber",
                "Intuitive Surgical, weil sie den Markt definieren")
        client_id = client.id

    body = web.get(f"/client/{client_id}/wettbewerb").text

    assert "Im Kickoff genannt" in body
    assert "Intuitive Surgical" in body
    with factory() as session:
        client = session.get(Client, client_id)
        assert list(client.competitors) == [], "offered, never linked"


# --- The guard again, now that there is something to adopt ---------------------


def test_converting_a_questionnaire_adopts_none_of_it(web, session):
    """Proposals computed, a draft generated, competitors offered — and the
    profile, the guide and the comparison set are byte-for-byte unchanged."""
    client = _client(session)
    _kickoff(session, client)
    session.add(ClientFact(client_id=client.id, key="ceo", value="Aus dem Netz",
                           source_url="https://beispiel.de", filled_by="gemini"))
    session.commit()
    before = _foundation(session, client.id)

    onboarding.to_proposals(session, client.id)
    onboarding.to_rivals(session, client)
    onboarding.to_guide_draft(session, client, invoke=_distilled())

    assert _foundation(session, client.id) == before
    assert list(session.get(Client, client.id).competitors) == []


def test_every_new_kickoff_conversion_string_has_an_english_entry():
    """The same rule as the questionnaire's own strings: a German line on an
    English page is the mixed UI the i18n table exists to prevent."""
    known = set(i18n.known_keys())

    assert onboarding.SOURCE_NAME in known
    for key in onboarding._PROFILE_SLOTS.values():
        field = profiles.FIELDS_BY_KEY[key]
        assert field.label in known, field.label
