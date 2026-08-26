"""The question set a mandate is measured on, and the pass that measures it.

Nothing here reaches a model and nothing here reaches the network. The answers
are real-shaped German model answers kept as files under
``fixtures/visibility/``, the way the RSS payloads already are, so the reading
tests assert against text somebody can open and check rather than against a
string invented inside an assertion.

Two properties get the most attention, because they are the two that would put a
wrong number in a client report:

* an answer that does not name the mandate and an answer nobody got are
  different facts, and the stored rows have to keep them apart;
* whether the mandate was named is decided against its stored aliases in code,
  never on the reading model's word.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, gemini, visibility
from newspulse.analyzer import BackendError
from newspulse.models import (
    Base,
    Client,
    VisibilityAnswer,
    VisibilityBand,
    VisibilityQuestion,
    VisibilityRun,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ANSWERS = Path(__file__).parent / "fixtures" / "visibility"

_NOW = dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC)

#: The question the two ``auswahl`` fixtures were captured against.
_AUSWAHL = "Welche Anbieter für Solaranlagen mit Speicher gibt es in Deutschland?"
_KATEGORIE = "Lohnt sich eine Photovoltaikanlage 2026 noch?"


def _answer(name: str) -> str:
    return (_ANSWERS / f"{name}.txt").read_text("utf-8")


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as open_session:
        yield open_session


@pytest.fixture(autouse=True)
def feature_on(monkeypatch):
    """The feature and its window, pinned so a test states what it relies on.

    ``config`` resolves once at import, so these are patched on the module rather
    than through the environment.
    """
    monkeypatch.setattr(config, "VISIBILITY_ENABLED", True)
    monkeypatch.setattr(config, "VISIBILITY_EVERY_DAYS", 7)


@pytest.fixture
def mandate(session) -> Client:
    """Enpal with two stored competitors, and one company that is neither.

    ``EON Solar`` is deliberately *not* in the competitor set: it appears in the
    fixtures so the intersection has something to leave out.
    """
    client = Client(name="Enpal", aliases=["Enpal B.V."], industry="Solarenergie")
    rivals = [
        Client(name="1Komma5°", is_competitor=True),
        Client(name="Zolar", is_competitor=True),
    ]
    session.add_all([client, *rivals])
    session.flush()
    client.competitors.extend(rivals)
    session.commit()
    return client


# --- Stand-ins for the two model calls -------------------------------------------


def _never_called(*args, **kwargs):
    raise AssertionError("a model call was spent where none was allowed")


def _asker(answers: dict[str, str]):
    """A provider: a question in, the captured answer out."""

    def _ask(question: str) -> str:
        assert question in answers, f"asked something nobody captured: {question!r}"
        return answers[question]

    return _ask


def _reader(readings: dict[str, dict]):
    """The reading model, keyed on a phrase that only one answer contains."""

    def _invoke(prompt: str, **_) -> str:
        for marker, payload in readings.items():
            if marker in prompt:
                return json.dumps(payload)
        raise AssertionError("no captured reading matches this prompt")

    return _invoke


def _panel(*rows: tuple[str, str]):
    """The panel model, returning one proposal per (question, band) pair."""

    def _invoke(prompt: str, **_) -> str:
        return json.dumps({"fragen": [{"frage": q, "band": b} for q, b in rows]})

    return _invoke


# --- The proposal: banded, and never about the client itself ---------------------


def test_every_one_of_the_four_bands_survives_generation(session, mandate):
    proposed = visibility.propose(
        session,
        mandate,
        invoke=_panel(
            ("Ist Enpal seriös?", "marke"),
            ("Welche Anbieter für Solaranlagen gibt es?", "auswahl"),
            ("Worauf muss ich bei einer Solaranlage achten?", "kategorie"),
            ("Wie senke ich meine Stromkosten dauerhaft?", "problem"),
        ),
    )

    assert [p.band for p in proposed] == [
        VisibilityBand.MARKE,
        VisibilityBand.AUSWAHL,
        VisibilityBand.KATEGORIE,
        VisibilityBand.PROBLEM,
    ]


def test_a_proposal_without_a_recognised_band_is_dropped_not_defaulted(session, mandate):
    """A band nobody recognises must not be filed under one of the four: the band
    decides which share the question is counted in, and a misfiled question reads
    exactly as correctly as a right one."""
    proposed = visibility.propose(
        session,
        mandate,
        invoke=_panel(
            ("Wie senke ich meine Stromkosten?", "kaufabsicht"),
            ("Worauf muss ich bei einer Solaranlage achten?", "kategorie"),
        ),
    )

    assert [p.text for p in proposed] == ["Worauf muss ich bei einer Solaranlage achten?"]


def test_a_proposal_with_an_empty_band_is_dropped(session, mandate):
    proposed = visibility.propose(
        session, mandate, invoke=_panel(("Was kostet eine Solaranlage?", ""))
    )

    assert proposed == []


def test_a_question_naming_the_client_outside_the_brand_band_is_rejected(session, mandate):
    """A question that names the client cannot measure whether the client is
    found: the answer is already in the question."""
    proposed = visibility.propose(
        session,
        mandate,
        invoke=_panel(
            ("Ist Enpal eine gute Wahl für eine Solaranlage?", "auswahl"),
            ("Welche Anbieter für Solaranlagen gibt es?", "auswahl"),
        ),
    )

    assert [p.text for p in proposed] == ["Welche Anbieter für Solaranlagen gibt es?"]


def test_a_question_naming_a_stored_alias_outside_the_brand_band_is_rejected(
    session, mandate
):
    """The alias is the same claim written differently, and the point of storing
    aliases is that the tool reads them as the same company."""
    proposed = visibility.propose(
        session,
        mandate,
        invoke=_panel(("Was kostet eine Anlage von Enpal B.V.?", "kategorie")),
    )

    assert proposed == []


def test_a_question_naming_the_client_in_the_genitive_is_rejected(session, mandate):
    """German writes the name into the sentence as "Enpals", and a word-boundary
    match ends at the name — so without the genitive form the guard would pass a
    question whose answer is already in the question."""
    proposed = visibility.propose(
        session,
        mandate,
        invoke=_panel(
            ("Was kostet Enpals Solaranlage mit Speicher?", "kategorie"),
            ("Welche Anbieter für Solaranlagen gibt es?", "auswahl"),
        ),
    )

    assert [p.text for p in proposed] == ["Welche Anbieter für Solaranlagen gibt es?"]


def test_the_brand_band_may_name_the_client(session, mandate):
    """The one band whose whole job is the brand. Refusing it here would leave
    the set unable to measure what an assistant says about the mandate at all."""
    proposed = visibility.propose(
        session, mandate, invoke=_panel(("Ist Enpal seriös?", "marke"))
    )

    assert [p.text for p in proposed] == ["Ist Enpal seriös?"]


def test_a_proposal_is_not_stored(session, mandate):
    visibility.propose(
        session, mandate, invoke=_panel(("Was kostet eine Solaranlage?", "kategorie"))
    )

    assert session.scalars(select(VisibilityQuestion)).all() == []
    assert visibility.accepted(session, mandate) == []


def test_a_question_the_mandate_already_carries_is_not_offered_again(session, mandate):
    visibility.accept(session, mandate, _KATEGORIE, VisibilityBand.KATEGORIE, now=_NOW)

    proposed = visibility.propose(
        session,
        mandate,
        invoke=_panel(
            (_KATEGORIE, "kategorie"),
            ("Worauf muss ich bei einer Solaranlage achten?", "kategorie"),
        ),
    )

    assert [p.text for p in proposed] == ["Worauf muss ich bei einer Solaranlage achten?"]


def test_the_feature_switch_stops_a_proposal_before_any_call(session, mandate, monkeypatch):
    monkeypatch.setattr(config, "VISIBILITY_ENABLED", False)

    assert visibility.propose(session, mandate, invoke=_never_called) == []


# --- Accepting: the only thing that stores a question ----------------------------


def test_a_mandate_has_an_empty_set_until_a_question_is_accepted(session, mandate):
    assert visibility.accepted(session, mandate) == []

    visibility.accept(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL, now=_NOW)

    assert [q.text for q in visibility.accepted(session, mandate)] == [_AUSWAHL]


def test_accepting_the_same_wording_twice_yields_one_question(session, mandate):
    """A double submit must not make one question count twice in a share."""
    first = visibility.accept(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL, now=_NOW)
    again = visibility.accept(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL, now=_NOW)

    assert again.id == first.id
    assert len(visibility.accepted(session, mandate)) == 1


def test_accepting_a_band_that_is_not_one_of_the_four_raises(session, mandate):
    with pytest.raises(ValueError):
        visibility.accept(session, mandate, _AUSWAHL, "kaufabsicht", now=_NOW)


def test_the_accepted_set_is_capped_at_twenty_four_questions(session, mandate):
    for index in range(visibility.MAX_QUESTIONS):
        visibility.accept(
            session, mandate, f"Frage {index}?", VisibilityBand.KATEGORIE, now=_NOW
        )

    with pytest.raises(visibility.SetFull):
        visibility.accept(session, mandate, "Eine mehr?", VisibilityBand.KATEGORIE, now=_NOW)

    assert len(visibility.accepted(session, mandate)) == visibility.MAX_QUESTIONS


def test_a_retired_question_leaves_the_set_and_keeps_its_answers(session, mandate):
    """Retired rather than deleted, because the answers point at the row: a
    delete would take every measurement it was part of with it."""
    question = visibility.accept(
        session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL, now=_NOW
    )
    visibility.measure(
        session,
        mandate,
        ask={visibility.PROVIDER_CLAUDE: _asker({_AUSWAHL: _answer("auswahl_claude")})},
        invoke=_reader({"1Komma5° verkauft": _READING_CLAUDE}),
        now=_NOW,
    )

    visibility.retire(session, question)

    assert visibility.accepted(session, mandate) == []
    assert session.scalars(select(VisibilityAnswer)).all() != []


# --- Reading one captured answer -------------------------------------------------
#
# The extraction the reading model performs, as it comes back for each fixture.

_READING_CLAUDE = {
    "unternehmen": ["1Komma5°", "Enpal", "Zolar", "EON Solar"],
    "quellen": ["Verbraucherzentrale", "Finanztip"],
}
_READING_GEMINI = {
    "unternehmen": ["Zolar", "EON Solar", "1Komma5°"],
    "quellen": ["Finanztip"],
}
_READING_KATEGORIE = {
    "unternehmen": ["Enpal", "Zolar", "1Komma5°"],
    "quellen": [],
}


def test_position_is_the_rank_of_the_first_appearance_among_the_companies_named(mandate):
    reading = visibility.read_answer(
        mandate,
        _answer("auswahl_claude"),
        _READING_CLAUDE["unternehmen"],
        _READING_CLAUDE["quellen"],
    )

    assert reading.named is True
    assert reading.companies == ("1Komma5°", "Enpal", "Zolar", "EON Solar")
    assert reading.position == 2


def test_an_answer_that_does_not_name_the_mandate_carries_no_position(mandate):
    reading = visibility.read_answer(
        mandate,
        _answer("auswahl_gemini"),
        _READING_GEMINI["unternehmen"],
        _READING_GEMINI["quellen"],
    )

    assert reading.named is False
    assert reading.position is None


def test_only_stored_competitors_count_as_rivals_and_the_rest_as_market(mandate):
    """EON Solar is named in the same answer and is nobody's stored competitor,
    so it is market. Counting it as a rival would move a share-of-voice figure."""
    reading = visibility.read_answer(
        mandate,
        _answer("auswahl_claude"),
        _READING_CLAUDE["unternehmen"],
        _READING_CLAUDE["quellen"],
    )

    assert reading.rivals == ("1Komma5°", "Zolar")
    assert "EON Solar" in reading.companies


def test_a_company_counts_as_named_through_a_stored_alias(session):
    """The mandate's own name never appears; the alias does, and the alias is the
    same company written the way a headline and a model write it."""
    client = Client(name="IB-7 Beauty Tech GmbH", aliases=["IB-7"])
    session.add(client)
    session.commit()

    reading = visibility.read_answer(
        client, "Im Bereich KI-Hautpflege ist vor allem IB-7 zu nennen.", ["IB-7"], []
    )

    assert reading.named is True
    assert reading.position == 1


def test_an_answer_naming_the_mandate_in_the_genitive_counts_as_named(mandate):
    """The wrong number in the direction a client acts on: an answer that opens
    with "Enpals Angebot" named the mandate, and storing named=False for it is
    exactly the reading this feature exists to prevent."""
    reading = visibility.read_answer(
        mandate,
        "Enpals Angebot umfasst Planung und Montage; Zolar vermittelt nur.",
        ["Zolar"],
        [],
    )

    assert reading.named is True
    assert reading.position == 1
    assert reading.rivals == ("Zolar",)


def test_a_company_the_answer_does_not_contain_is_not_counted(mandate):
    """The reading model lists what it read; a name that is not in the answer is
    a name it did not read, and the answer beside the figure has to support it."""
    reading = visibility.read_answer(
        mandate,
        _answer("auswahl_gemini"),
        [*_READING_GEMINI["unternehmen"], "Wegatech"],
        [],
    )

    assert "Wegatech" not in reading.companies


def test_sources_are_only_the_ones_the_answer_states(mandate):
    reading = visibility.read_answer(
        mandate,
        _answer("auswahl_claude"),
        _READING_CLAUDE["unternehmen"],
        ["Verbraucherzentrale", "Stiftung Warentest"],
    )

    assert reading.sources == ("Verbraucherzentrale",)


def test_an_answer_citing_nothing_yields_an_empty_source_list(mandate):
    """Not a guess reconstructed from the text: "we do not know what it read" and
    "it read these four things" are different facts."""
    reading = visibility.read_answer(
        mandate,
        _answer("kategorie_ohne_quelle"),
        _READING_KATEGORIE["unternehmen"],
        _READING_KATEGORIE["quellen"],
    )

    assert reading.sources == ()
    assert reading.named is True
    assert reading.position == 1


def test_a_stated_source_is_not_found_inside_a_longer_word(mandate):
    """A publisher's name is matched as a word. "FAZ" inside "Fazit" is not a
    citation, and the source list is the one figure whose whole job is to resolve
    to something a person can find in the text underneath it."""
    reading = visibility.read_answer(
        mandate,
        "Enpal wird häufig genannt. Fazit: der Markt ist unübersichtlich.",
        [],
        ["FAZ"],
    )

    assert reading.sources == ()


def test_a_short_publisher_name_the_answer_does_name_still_counts(mandate):
    """The other half of the same rule: matching on words rather than substrings
    must not cost a three-letter publisher its citation."""
    reading = visibility.read_answer(
        mandate,
        "Enpal wird häufig genannt; die FAZ hat darüber berichtet.",
        [],
        ["FAZ"],
    )

    assert reading.sources == ("FAZ",)


def test_a_source_written_as_a_url_still_counts_as_stated(mandate):
    reading = visibility.read_answer(
        mandate,
        "Details stehen bei pv-magazine.de und bei niemandem sonst.",
        [],
        ["https://www.pv-magazine.de/"],
    )

    assert reading.sources == ("https://www.pv-magazine.de/",)


# --- The measurement --------------------------------------------------------------


def _measure(session, mandate, *, ask, now=_NOW):
    return visibility.measure(
        session,
        mandate,
        ask=ask,
        invoke=_reader(
            {
                "1Komma5° verkauft": _READING_CLAUDE,
                "überwiegend von regionalen": _READING_GEMINI,
                "bekanntesten Anbieter": _READING_KATEGORIE,
            }
        ),
        now=now,
    )


def _two_questions(session, mandate) -> None:
    visibility.accept(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL, now=_NOW)
    visibility.accept(session, mandate, _KATEGORIE, VisibilityBand.KATEGORIE, now=_NOW)


def test_one_measurement_writes_one_row_per_question_and_provider(session, mandate):
    _two_questions(session, mandate)
    answers = {
        _AUSWAHL: _answer("auswahl_claude"),
        _KATEGORIE: _answer("kategorie_ohne_quelle"),
    }

    run = _measure(
        session,
        mandate,
        ask={
            visibility.PROVIDER_CLAUDE: _asker(answers),
            visibility.PROVIDER_GEMINI: _asker(
                {
                    _AUSWAHL: _answer("auswahl_gemini"),
                    _KATEGORIE: answers[_KATEGORIE],
                }
            ),
        },
    )

    assert run is not None
    assert len(run.answers) == 4
    assert {(row.question.text, row.provider) for row in run.answers} == {
        (_AUSWAHL, "claude"),
        (_AUSWAHL, "gemini"),
        (_KATEGORIE, "claude"),
        (_KATEGORIE, "gemini"),
    }
    assert run.providers_failed == []


def test_a_row_carries_the_answer_verbatim(session, mandate):
    visibility.accept(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL, now=_NOW)
    captured = _answer("auswahl_claude")

    run = _measure(
        session,
        mandate,
        ask={visibility.PROVIDER_CLAUDE: _asker({_AUSWAHL: captured})},
    )

    stored = run.answers[0]
    assert stored.answer == captured
    assert stored.named is True
    assert stored.position == 2
    assert stored.rivals == ["1Komma5°", "Zolar"]
    assert stored.sources == ["Verbraucherzentrale", "Finanztip"]


def test_a_mandate_with_no_accepted_question_is_skipped_and_spends_no_call(
    session, mandate
):
    run = visibility.measure(
        session,
        mandate,
        ask={visibility.PROVIDER_CLAUDE: _never_called},
        invoke=_never_called,
        now=_NOW,
    )

    assert run is None


def test_the_feature_switch_stops_a_measurement_before_any_call(
    session, mandate, monkeypatch
):
    _two_questions(session, mandate)
    monkeypatch.setattr(config, "VISIBILITY_ENABLED", False)

    run = visibility.measure(
        session,
        mandate,
        ask={visibility.PROVIDER_CLAUDE: _never_called},
        invoke=_never_called,
        now=_NOW,
    )

    assert run is None


def test_a_provider_that_errors_is_recorded_as_failed_and_never_as_not_named(
    session, mandate
):
    """The one wrong number this feature could produce. A missing answer must not
    reach the page as an answer that did not name the mandate."""
    _two_questions(session, mandate)

    def _broken(question: str) -> str:
        raise BackendError("claude -p exited 1: usage limit reached")

    run = _measure(
        session,
        mandate,
        ask={
            visibility.PROVIDER_CLAUDE: _broken,
            visibility.PROVIDER_GEMINI: _asker(
                {
                    _AUSWAHL: _answer("auswahl_gemini"),
                    _KATEGORIE: _answer("kategorie_ohne_quelle"),
                }
            ),
        },
    )

    assert run.providers_failed == ["claude"]
    assert {row.provider for row in run.answers} == {"gemini"}
    assert not any(row.provider == "claude" for row in run.answers)


def test_a_failing_provider_is_dropped_after_two_strikes_not_asked_every_question(
    session, mandate
):
    """An exhausted subscription fails identically twenty-four times, so it must
    not be asked twenty-four times. Two strikes rather than one, because the next
    measurement is a week away and a single 503 must not cost the provider all
    of it."""
    for index in range(5):
        visibility.accept(
            session, mandate, f"Frage {index}?", VisibilityBand.KATEGORIE, now=_NOW
        )
    asked: list[str] = []

    def _broken(question: str) -> str:
        asked.append(question)
        raise BackendError("claude -p exited 1: usage limit reached")

    _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _broken})

    assert asked == ["Frage 0?", "Frage 1?"]


def test_one_transient_failure_does_not_cost_a_provider_the_rest_of_the_run(
    session, mandate
):
    """One timeout on a long answer used to retire the provider for the whole
    week. The questions after it are still put to it, and the run keeps what it
    answered."""
    _two_questions(session, mandate)
    seen: list[str] = []

    def _flaky(question: str) -> str:
        seen.append(question)
        if len(seen) == 1:
            raise BackendError("Gemini unreachable: timed out")
        return _answer("kategorie_ohne_quelle")

    run = _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _flaky})

    assert seen == [_AUSWAHL, _KATEGORIE]
    assert [row.question.text for row in run.answers] == [_KATEGORIE]
    assert run.providers_failed == ["claude"]


def test_a_reading_failure_leaves_the_provider_unflagged_and_still_asked(
    session, mandate
):
    """The provider answered and the reader could not parse it. That cell gets no
    row — but the provider is not in ``providers_failed``, because it answered:
    that column says "this one could not answer", and one unreadable cell must
    not flag the ones that were measured as an outage. It is not dropped from the
    run either: the reader is a separate call."""
    _two_questions(session, mandate)
    readings = iter(["not json at all", json.dumps(_READING_KATEGORIE)])

    run = visibility.measure(
        session,
        mandate,
        ask={
            visibility.PROVIDER_CLAUDE: _asker(
                {
                    _AUSWAHL: _answer("auswahl_claude"),
                    _KATEGORIE: _answer("kategorie_ohne_quelle"),
                }
            )
        },
        invoke=lambda prompt, **_: next(readings),
        now=_NOW,
    )

    assert run.providers_failed == []
    assert [row.question.text for row in run.answers] == [_KATEGORIE]


def test_a_run_in_which_every_provider_failed_does_not_hold_the_window(
    session, mandate
):
    """A broken key on Monday must not take the rest of the week with it. The
    attempt is stored, so the page can say both providers were down — but a run
    that measured nothing is not the measurement the window is counted from."""
    _two_questions(session, mandate)

    def _broken(question: str) -> str:
        raise BackendError("claude -p exited 1: usage limit reached")

    barren = _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _broken})

    assert barren is not None
    assert list(barren.answers) == []
    assert barren.providers_failed == ["claude"]
    assert visibility.due(session, mandate, now=_NOW) is True

    again = _measure(
        session,
        mandate,
        ask={
            visibility.PROVIDER_CLAUDE: _asker(
                {
                    _AUSWAHL: _answer("auswahl_claude"),
                    _KATEGORIE: _answer("kategorie_ohne_quelle"),
                }
            )
        },
    )

    assert again.id != barren.id
    assert len(again.answers) == 2


def test_a_second_measurement_inside_the_window_returns_the_stored_run(session, mandate):
    _two_questions(session, mandate)
    answers = {
        _AUSWAHL: _answer("auswahl_claude"),
        _KATEGORIE: _answer("kategorie_ohne_quelle"),
    }
    first = _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _asker(answers)})

    again = visibility.measure(
        session,
        mandate,
        ask={visibility.PROVIDER_CLAUDE: _never_called},
        invoke=_never_called,
        now=_NOW + dt.timedelta(days=6, hours=23),
    )

    assert again is not None
    assert again.id == first.id
    assert len(again.answers) == 2


def test_a_measurement_after_the_window_runs_again(session, mandate):
    _two_questions(session, mandate)
    answers = {
        _AUSWAHL: _answer("auswahl_claude"),
        _KATEGORIE: _answer("kategorie_ohne_quelle"),
    }
    first = _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _asker(answers)})

    later = _measure(
        session,
        mandate,
        ask={visibility.PROVIDER_CLAUDE: _asker(answers)},
        now=_NOW + dt.timedelta(days=7),
    )

    assert later.id != first.id
    assert visibility.latest_run(session, mandate).id == later.id


def test_claude_alone_is_asked_when_no_gemini_key_is_configured(monkeypatch):
    """DEC-2 asks the two assistants that are already connected. A provider the
    deployment never set up must not be asked and then recorded as failing every
    week, which reads as an outage rather than as a choice nobody made."""
    monkeypatch.setattr(config, "gemini_configured", lambda: False)

    assert list(visibility.askers()) == [visibility.PROVIDER_CLAUDE]


def test_both_assistants_are_asked_when_gemini_has_a_key(monkeypatch):
    monkeypatch.setattr(config, "gemini_configured", lambda: True)

    assert list(visibility.askers()) == [
        visibility.PROVIDER_CLAUDE,
        visibility.PROVIDER_GEMINI,
    ]


def test_a_provider_call_that_fails_raises_an_analyzer_error(monkeypatch):
    """The whole failed-provider path catches ``AnalyzerError`` and nothing else.
    A provider raising anything outside that hierarchy would abort the run rather
    than be recorded, and every stored row already collected would go with it."""
    monkeypatch.setattr(config, "gemini_configured", lambda: True)

    def _broken_cli(prompt: str, **_) -> str:
        raise BackendError("claude -p exited 1: usage limit reached")

    def _broken_gemini(prompt: str, **_) -> str:
        raise BackendError("Gemini unreachable: timed out")

    monkeypatch.setattr(visibility, "invoke_claude_cli", _broken_cli)
    monkeypatch.setattr(gemini, "generate", _broken_gemini)

    for put in visibility.askers().values():
        with pytest.raises(visibility.AnalyzerError):
            put("Welche Anbieter für Solaranlagen gibt es?")


def test_a_window_of_zero_days_measures_on_every_request(session, mandate, monkeypatch):
    """Documented in ``config`` as "no window": a legitimate setting for an
    operator working a single mandate by hand, and a bad one for a portfolio."""
    monkeypatch.setattr(config, "VISIBILITY_EVERY_DAYS", 0)
    _two_questions(session, mandate)
    answers = {
        _AUSWAHL: _answer("auswahl_claude"),
        _KATEGORIE: _answer("kategorie_ohne_quelle"),
    }
    first = _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _asker(answers)})

    again = _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _asker(answers)})

    assert again.id != first.id
    assert visibility.due(session, mandate, now=_NOW) is True


def test_the_feature_switch_makes_no_mandate_due(session, mandate, monkeypatch):
    """The sweep asks ``due`` before it asks anything else, so the switch has to
    be answered here as well as inside ``measure``."""
    _two_questions(session, mandate)
    monkeypatch.setattr(config, "VISIBILITY_ENABLED", False)

    assert visibility.due(session, mandate, now=_NOW) is False


def test_a_naive_now_is_read_as_utc_rather_than_raising(session, mandate):
    """``ran_at`` always comes back timezone-aware. A naive ``now`` subtracted
    against it raises TypeError instead of answering the question, so it is read
    as UTC — the same reading the column type gives one on the way in."""
    _two_questions(session, mandate)
    answers = {
        _AUSWAHL: _answer("auswahl_claude"),
        _KATEGORIE: _answer("kategorie_ohne_quelle"),
    }
    _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _asker(answers)})

    assert visibility.due(session, mandate, now=_NOW.replace(tzinfo=None)) is False
    assert (
        visibility.due(
            session,
            mandate,
            now=(_NOW + dt.timedelta(days=8)).replace(tzinfo=None),
        )
        is True
    )


def test_a_mandate_is_due_when_it_has_never_been_measured(session, mandate):
    _two_questions(session, mandate)

    assert visibility.due(session, mandate, now=_NOW) is True


def test_a_mandate_with_no_question_is_never_due(session, mandate):
    """Otherwise the sweep picks it up every morning and can never serve it."""
    assert visibility.due(session, mandate, now=_NOW) is False


# --- The schema the hand-authored migration owns ----------------------------------


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """A database built by Alembic rather than by ``Base.metadata.create_all``.

    Every other test in this file runs on the metadata, so a migration that left
    a constraint out would pass all of them. This is the only place the schema
    that actually reaches production is exercised.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


def _one_cell(engine) -> tuple[int, int]:
    """A mandate, one accepted question and one run, on the migrated schema."""
    with sessionmaker(bind=engine, expire_on_commit=False)() as open_session:
        client = Client(name="Enpal")
        open_session.add(client)
        open_session.flush()
        question = VisibilityQuestion(
            client_id=client.id, text=_AUSWAHL, band=VisibilityBand.AUSWAHL
        )
        run = VisibilityRun(
            client_id=client.id, providers_asked=["claude"], providers_failed=[]
        )
        open_session.add_all([question, run])
        open_session.commit()
        return question.id, run.id


_INSERT_ANSWER = sql_text(
    "INSERT INTO visibility_answers "
    "(run_id, question_id, provider, answer, named, position) "
    "VALUES (:run_id, :question_id, :provider, :answer, :named, :position)"
)


def _insert_answer(engine, **values) -> None:
    with engine.begin() as connection:
        connection.execute(_INSERT_ANSWER, values)


def test_the_migration_creates_the_three_visibility_tables(migrated):
    """``Base.metadata.create_all`` builds the schema every test above runs on;
    Alembic is what builds the one in production. They have to agree."""
    tables = set(inspect(migrated).get_table_names())
    assert {
        "visibility_questions",
        "visibility_runs",
        "visibility_answers",
    } <= tables
    columns = {c["name"] for c in inspect(migrated).get_columns("visibility_answers")}
    assert {"answer", "named", "position", "rivals", "sources"} <= columns


def test_the_migrated_schema_refuses_a_rank_beside_an_answer_that_did_not_name_it(
    migrated,
):
    """The CHECK the model docstring calls a schema guarantee rather than an
    invariant three readers have to remember. If the migration dropped it, the
    guarantee would hold in every test above and nowhere in production."""
    question_id, run_id = _one_cell(migrated)

    with pytest.raises(IntegrityError):
        _insert_answer(
            migrated,
            run_id=run_id,
            question_id=question_id,
            provider="claude",
            answer="Enpal kommt darin nicht vor.",
            named=0,
            position=3,
        )


def test_the_migrated_schema_refuses_a_rank_below_one(migrated):
    """"Position 0" reads as a bug on a page that prints the number."""
    question_id, run_id = _one_cell(migrated)

    with pytest.raises(IntegrityError):
        _insert_answer(
            migrated,
            run_id=run_id,
            question_id=question_id,
            provider="claude",
            answer="Enpal wird zuerst genannt.",
            named=1,
            position=0,
        )


def test_the_migrated_schema_refuses_a_second_row_for_the_same_cell(migrated):
    """One measurement writes one row per (question, provider). Two would count
    the same answer twice in every share computed off the run."""
    question_id, run_id = _one_cell(migrated)
    cell = {
        "run_id": run_id,
        "question_id": question_id,
        "provider": "claude",
        "answer": "Enpal wird zuerst genannt.",
        "named": 1,
        "position": 1,
    }
    _insert_answer(migrated, **cell)

    with pytest.raises(IntegrityError):
        _insert_answer(migrated, **cell)


def test_the_migrated_schema_refuses_the_same_wording_twice_for_one_mandate(
    migrated,
):
    """The UNIQUE ``accept`` leans on for its idempotency: two identical
    questions are one question counted twice in a share."""
    _one_cell(migrated)

    with sessionmaker(bind=migrated, expire_on_commit=False)() as open_session:
        client = open_session.scalars(select(Client)).one()
        open_session.add(
            VisibilityQuestion(
                client_id=client.id, text=_AUSWAHL, band=VisibilityBand.KATEGORIE
            )
        )

        with pytest.raises(IntegrityError):
            open_session.commit()
