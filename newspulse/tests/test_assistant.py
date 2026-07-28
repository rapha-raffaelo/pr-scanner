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
