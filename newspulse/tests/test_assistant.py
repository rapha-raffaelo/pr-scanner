"""Captain Comms: context building and conversation replay.

``claude -p`` is stateless per call, so continuity is the browser replaying the
transcript. These tests pin the parsing of that replay — the failure that matters
is a malformed payload taking the answer down with it.
"""

from __future__ import annotations

import json

from newspulse.web.routes.assistant import (
    _MAX_HISTORY_TURNS,
    _build_prompt,
    _parse_history,
    _render_history,
)


def test_history_round_trips():
    raw = json.dumps([
        {"role": "user", "text": "Was ist die Lage?"},
        {"role": "assistant", "text": "Angespannt."},
    ])
    assert _parse_history(raw) == [
        ("user", "Was ist die Lage?"),
        ("assistant", "Angespannt."),
    ]


def test_a_malformed_payload_degrades_to_a_one_shot_answer():
    """Losing continuity is a smaller harm than losing the answer."""
    for bad in ("not json", "{}", "[1,2,3]", '[{"role":"user"}]', None, ""):
        assert _parse_history(bad) == []


def test_history_is_capped_and_keeps_the_most_recent_turns():
    """An unbounded transcript would crowd out the coverage that grounds it."""
    turns = [{"role": "user", "text": f"Frage {i}"} for i in range(40)]
    parsed = _parse_history(json.dumps(turns))
    assert len(parsed) == _MAX_HISTORY_TURNS * 2
    assert parsed[-1] == ("user", "Frage 39")


def test_trimming_a_long_transcript_keeps_the_latest_exchange():
    huge = "x" * 3_000
    turns = [
        {"role": "user", "text": huge},
        {"role": "assistant", "text": huge},
        {"role": "user", "text": "Die neueste Frage"},
    ]
    rendered = _render_history(_parse_history(json.dumps(turns)))
    assert "Die neueste Frage" in rendered


def test_the_prompt_carries_the_strategy_frame_and_the_coverage():
    prompt = _build_prompt("Was tun?", "Zalando", "[0] Eine Meldung", [])
    assert "Captain Comms" in prompt
    assert "Zalando" in prompt
    assert "[0] Eine Meldung" in prompt
    assert "Was tun?" in prompt
    # No prior turns means no empty conversation section.
    assert "BISHERIGES GESPRÄCH" not in prompt


def test_the_prompt_includes_prior_turns_when_there_are_any():
    prompt = _build_prompt(
        "Und dann?", "Zalando", "[0] Eine Meldung",
        [("user", "Was ist los?"), ("assistant", "Eine Krise.")],
    )
    assert "BISHERIGES GESPRÄCH" in prompt
    assert "Eine Krise." in prompt


# --- Language ------------------------------------------------------------------


def test_the_frame_follows_the_reader_s_language():
    from newspulse.web.routes.assistant import _FRAME_DE, _FRAME_EN

    de = _build_prompt("Was tun?", "Zalando", "[0] Meldung", [], "de")
    en = _build_prompt("What now?", "Zalando", "[0] Meldung", [], "en")
    assert _FRAME_DE in de and _FRAME_EN not in de
    assert _FRAME_EN in en and _FRAME_DE not in en


def test_section_headings_follow_the_language_too():
    """They are part of the prompt the model reads, so leaving them German would
    quietly pull the answer back towards German."""
    en = _build_prompt("What now?", "Zalando", "[0] Meldung",
                       [("user", "Earlier")], "en")
    assert "CONTEXT (" in en and "QUESTION" in en and "CONVERSATION SO FAR" in en
    assert "KONTEXT" not in en and "FRAGE" not in en


def test_the_english_frame_says_the_coverage_stays_german():
    """Handed German source and asked for English, a model otherwise tends to
    quote in one language and write in the other."""
    from newspulse.web.routes.assistant import _FRAME_EN

    assert "German press" in _FRAME_EN


def test_an_unknown_language_falls_back_to_the_german_frame():
    from newspulse.web.routes.assistant import _FRAME_DE

    assert _FRAME_DE in _build_prompt("?", "X", "y", [], "fr")


# --- Mandate picker ------------------------------------------------------------


def test_the_picker_lists_mandates_only(tmp_path):
    """You do not ask for recommendations for a company you do not represent."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from newspulse.models import Base, Client
    from newspulse.web.app import create_app, get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Client(name="Alpha AG"))
        s.add(Client(name="Rivale AG", is_competitor=True))
        s.add(Client(name="Alt AG", active=False))
        s.commit()

    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    names = [c["name"] for c in TestClient(app).get("/api/assistant/clients").json()]
    assert names == ["Alpha AG"]
    assert "Rivale AG" not in names   # a benchmark, not a mandate
    assert "Alt AG" not in names      # deactivated
