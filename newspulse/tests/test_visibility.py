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
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, visibility
from newspulse.analyzer import BackendError
from newspulse.models import (
    Base,
    Client,
    VisibilityAnswer,
    VisibilityBand,
    VisibilityQuestion,
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


def test_a_failing_provider_is_asked_once_rather_than_once_per_question(session, mandate):
    """An exhausted subscription fails identically twenty-four times. Retiring it
    after the first failure keeps a bad morning from costing a full set."""
    _two_questions(session, mandate)
    asked: list[str] = []

    def _broken(question: str) -> str:
        asked.append(question)
        raise BackendError("claude -p exited 1: usage limit reached")

    _measure(session, mandate, ask={visibility.PROVIDER_CLAUDE: _broken})

    assert asked == [_AUSWAHL]


def test_a_reading_failure_marks_the_provider_failed_without_retiring_it(session, mandate):
    """The provider answered and the reader could not parse it. Same consequence
    for the page — that cell was not measured — but the reader is a different
    call, so the next question is still put to the provider."""
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

    assert run.providers_failed == ["claude"]
    assert [row.question.text for row in run.answers] == [_KATEGORIE]


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


def test_a_mandate_is_due_when_it_has_never_been_measured(session, mandate):
    _two_questions(session, mandate)

    assert visibility.due(session, mandate, now=_NOW) is True


def test_a_mandate_with_no_question_is_never_due(session, mandate):
    """Otherwise the sweep picks it up every morning and can never serve it."""
    assert visibility.due(session, mandate, now=_NOW) is False


# --- The schema the hand-authored migration owns ----------------------------------


def test_the_migration_creates_the_three_visibility_tables(tmp_path, monkeypatch):
    """``Base.metadata.create_all`` builds the schema every test above runs on;
    Alembic is what builds the one in production. They have to agree."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "visibility_questions",
            "visibility_runs",
            "visibility_answers",
        } <= tables
        columns = {c["name"] for c in inspect(engine).get_columns("visibility_answers")}
        assert {"answer", "named", "position", "rivals", "sources"} <= columns
    finally:
        engine.dispose()
