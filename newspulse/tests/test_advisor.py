"""The advisor (newspulse.advisor).

Driven with an injected ``invoke`` so the whole path — prompt build, parse,
evidence resolution, persistence — is exercised without a subprocess or a model.
The assertions concentrate on the trust boundary: the model's reply is text until
the schema says otherwise, and a citation that points nowhere is not shown.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import advisor
from newspulse.analyzer import ParseError
from newspulse.models import Analysis, Article, Base, Category, Client


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture
def client(session):
    c = Client(name="Alpha AG", industry="Handel")
    session.add(c)
    session.flush()
    for i in range(3):
        art = Article(
            title=f"Alpha schliesst Standort {i}",
            url=f"https://ex.de/{i}",
            source="FAZ",
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
            fetched_at=dt.datetime.now(dt.UTC),
            summary_text="Snippet.",
            language="de",
            title_hash=f"h{i}",
        )
        session.add(art)
        session.flush()
        session.add(
            Analysis(
                article_id=art.id, client_id=c.id, summary="Zusammenfassung.",
                category=Category.KRISE, relevance_score=5,
                importance_score=9 - i, is_alert=True,
            )
        )
    session.commit()
    return c


def _reply(payload) -> str:
    return json.dumps(payload)


def test_advise_returns_a_validated_brief(session, client):
    brief, coverage = advisor.advise(
        session, client,
        invoke=lambda prompt, timeout=None: _reply({
            "situation": "Lage ist angespannt.",
            "suggestions": [{
                "title": "Statement vorbereiten",
                "rationale": "Wegen der Schliessung.",
                "kind": "reaktiv", "urgency": "heute", "evidence": [0],
            }],
        }),
    )
    assert brief.situation == "Lage ist angespannt."
    assert len(brief.suggestions) == 1
    assert brief.suggestions[0].kind == "reaktiv"
    assert len(coverage) == 3


def test_the_coverage_actually_reaches_the_prompt(session, client):
    """A brief built without the coverage in front of the model would be fiction."""
    seen = {}

    def _capture(prompt, timeout=None):
        seen["prompt"] = prompt
        return _reply({"situation": "x", "suggestions": []})

    advisor.advise(session, client, invoke=_capture)
    assert "Alpha AG" in seen["prompt"]
    assert "Alpha schliesst Standort 0" in seen["prompt"]
    assert "[0]" in seen["prompt"]


def test_invented_evidence_ids_are_dropped(session, client):
    """An index pointing at nothing would render as a broken citation and make
    the whole brief look unreliable."""
    brief, _ = advisor.advise(
        session, client,
        invoke=lambda prompt, timeout=None: _reply({
            "situation": "x",
            "suggestions": [{
                "title": "t", "rationale": "r", "kind": "proaktiv",
                "urgency": "laufend", "evidence": [0, 99, -1],
            }],
        }),
    )
    assert brief.suggestions[0].evidence == [0]


def test_non_json_reply_is_a_parse_error_not_a_silent_empty_brief(session, client):
    """"Nothing to advise" and "the advisor failed" must not look alike."""
    with pytest.raises(ParseError):
        advisor.advise(session, client, invoke=lambda p, timeout=None: "Gerne! Hier...")


def test_a_fenced_reply_is_still_read(session, client):
    """A ```json wrapper is a formatting habit, and this call has no retry: losing
    the brief to it would mean failing at exactly the moment someone asked."""
    payload = _reply({
        "situation": "Die Lage ist ruhig.",
        "suggestions": [{
            "title": "t", "rationale": "r", "kind": "proaktiv",
            "urgency": "laufend", "evidence": [0],
        }],
    })
    brief, _ = advisor.advise(
        session, client, invoke=lambda p, timeout=None: f"```json\n{payload}\n```"
    )

    assert brief.situation == "Die Lage ist ruhig."
    assert len(brief.suggestions) == 1


def test_reply_failing_the_schema_is_rejected_whole(session, client):
    """A half-parsed recommendation is worse than none."""
    with pytest.raises(ParseError):
        advisor.advise(
            session, client,
            invoke=lambda p, timeout=None: _reply({
                "situation": "x",
                "suggestions": [{"title": "t", "kind": "unfug", "urgency": "heute"}],
            }),
        )


def test_an_empty_suggestion_list_is_a_valid_answer(session, client):
    """A tool that invents busywork on a quiet week trains its user to ignore it."""
    brief, _ = advisor.advise(
        session, client,
        invoke=lambda p, timeout=None: _reply({"situation": "Ruhig.", "suggestions": []}),
    )
    assert brief.suggestions == []


def test_no_coverage_short_circuits_without_calling_the_model(session):
    """No coverage means nothing to reason about — and no reason to spend a call."""
    c = Client(name="Leer AG")
    session.add(c)
    session.commit()

    def _boom(prompt, timeout=None):
        raise AssertionError("must not be called")

    brief, coverage = advisor.advise(session, c, invoke=_boom)
    assert coverage == []
    assert brief.suggestions == []


def test_store_and_latest_round_trip(session, client):
    brief, coverage = advisor.advise(
        session, client,
        invoke=lambda p, timeout=None: _reply({
            "situation": "Lage.",
            "suggestions": [{"title": "t", "rationale": "r", "kind": "reaktiv",
                             "urgency": "heute", "evidence": [1]}],
        }),
    )
    advisor.store(session, client, brief, coverage, days=30)
    stored = advisor.latest(session, client.id)
    assert stored is not None
    assert stored.situation == "Lage."
    assert stored.article_count == 3
    assert stored.suggestions[0]["title"] == "t"


def test_latest_is_none_before_anything_is_generated(session, client):
    assert advisor.latest(session, client.id) is None


# --- A recommendation is a text, not a briefing line -----------------------------


def test_a_recommendation_carries_the_text_it_recommends(session):
    """"Für mich sind Empfehlungen Beispiel-Pressemeldungen, die man an PR-Berater
    schicken kann." The two halves of the page produced different shapes: the
    positioning was a sendable draft, this one described work and left the
    writing to the reader."""
    from newspulse.schemas import ActionSuggestion

    suggestion = ActionSuggestion(
        title="Sachlich einordnen",
        draft="Zwei Absätze, die so verschickt werden könnten.",
        rationale="Weil die Meldung sonst unwidersprochen bleibt.",
        kind="reaktiv",
        urgency="heute",
        evidence=[0],
    )

    assert suggestion.draft.startswith("Zwei Absätze")


def test_a_recommendation_to_stay_silent_has_no_draft(session):
    """Inventing a text nobody should send would be worse than an empty field —
    the same rule the positioning drafts follow with worth_sending."""
    from newspulse.schemas import ActionSuggestion

    suggestion = ActionSuggestion(
        title="Nicht reagieren",
        rationale="Eine Antwort verlängert die Geschichte.",
        kind="beobachten",
        urgency="laufend",
    )

    assert suggestion.draft == ""


def test_an_older_stored_brief_still_renders(session):
    """Briefs written before the field existed carry no draft, and the page has
    to show them rather than fail on them."""
    from newspulse.schemas import AdvisoryBrief

    brief = AdvisoryBrief.model_validate(
        {
            "situation": "Die Lage.",
            "suggestions": [
                {
                    "title": "Alt",
                    "rationale": "Ohne draft gespeichert.",
                    "kind": "proaktiv",
                    "urgency": "diese_woche",
                    "evidence": [],
                }
            ],
        }
    )

    assert brief.suggestions[0].draft == ""
