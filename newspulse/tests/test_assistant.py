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


# --- Voice controls ------------------------------------------------------------
#
# The behaviour lives in the browser (Web Speech API), so what is testable here is
# the contract the markup carries: both controls exist on every page, both start
# hidden until the script confirms the API, and the page says out loud that the
# recognition is not ours. That last one is the point of these tests — it is a
# data-protection statement, and it must not quietly disappear in a refactor.


def _page(path: str = "/", lang: str | None = None) -> str:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient

    from newspulse import i18n
    from newspulse.models import Base
    from newspulse.web.app import create_app, get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    client = TestClient(app)
    if lang:
        client.cookies.set(i18n.COOKIE_NAME, lang)
    return client.get(path).text


def test_the_voice_controls_ship_on_every_page():
    """The drawer lives in the shared layout, so voice follows it everywhere."""
    for path in ("/", "/clients", "/archive", "/settings"):
        body = _page(path)
        assert 'id="drawer-mic"' in body, path
        assert 'id="drawer-speak"' in body, path


def test_both_controls_start_hidden_so_no_dead_button_is_ever_shown():
    """Web Speech is Chrome/Safari only and needs a secure context. The script
    unhides each control after it confirms the API, so a browser without it shows
    nothing rather than a button that does nothing."""
    body = _page()
    mic = body.split('id="drawer-mic"', 1)[1].split(">", 1)[0]
    speak = body.split('id="drawer-speak"', 1)[1].split(">", 1)[0]
    assert "hidden" in mic
    assert "hidden" in speak
    # And the feature detection that unhides them.
    assert "webkitSpeechRecognition" in body
    assert "speechSynthesis" in body


def test_the_page_says_the_recognition_is_not_ours():
    """A spoken question names mandates and strategy, and the audio leaves the
    machine — Chrome to Google, Safari to Apple. Stating that is not optional.

    The status line is matched on its umlaut-free tail: it reaches the page through
    ``| tojson``, which escapes non-ASCII (``\\u00f6``), so the German spelling
    would never match the rendered source.
    """
    body = _page()
    assert "die Spracherkennung läuft im Browser, nicht in NewsPulse" in body
    assert "der Browser, nicht NewsPulse" in body


def test_reading_answers_aloud_is_off_until_it_is_switched_on():
    """A tool that starts talking while a client is on the phone gets switched off.

    The stored preference is read, never defaulted to on: only an explicit "1"
    enables it.
    """
    body = _page()
    assert 'localStorage.getItem(SPEAK_KEY) === "1"' in body
    assert 'aria-pressed="false"' in body


def test_the_recognizer_gets_a_full_locale_not_the_app_code():
    """A recognizer handed "de" falls back to its default locale, which for a
    German question means listening in English."""
    body = _page()
    assert '"en-GB" : "de-DE"' in body


def test_the_voice_labels_translate():
    body = _page(lang="en")
    assert "Speak your question" in body
    assert "Read answers aloud" in body
    assert "Frage sprechen" not in body


def test_escape_ends_voice_without_checking_whether_the_drawer_is_open():
    """Regression: the guard skipped exactly the case it was written for.

    The drawer's own Escape handler is registered first and hides the panel, so an
    `if (!drawer.hidden)` guard on the voice teardown is always false by the time it
    runs — Escape would close the drawer and leave the answer being read aloud and
    the microphone open.
    """
    body = _page()
    handler = body.split('if (e.key === "Escape") endVoice()', 1)
    assert len(handler) == 2, "the unconditional Escape teardown is gone"
    assert 'e.key === "Escape" && !drawer.hidden) endVoice' not in body
