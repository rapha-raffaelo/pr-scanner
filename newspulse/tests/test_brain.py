"""The tests that keep the brain load-bearing rather than decorative.

Three kinds of thing live here, and they answer three different questions.

The *structural* tests answer "is the layer still the only source of standards?"
They are the mechanism DEC-2 option B buys: a prompt that restates a standard
inline fails the suite, so the standards cannot quietly drift back out into the
prompts over a quarter the way they did the first time.

The *golden* tests answer "what did that edit actually change?" They render every
prompt against the shipped blocks, so editing one block turns into a reviewable
diff across exactly the prompts that include it. Regenerate them deliberately::

    NEWSPULSE_REGOLD=1 uv run pytest tests/test_brain.py

The *unit* tests answer "does resolution behave?" and take their block set as an
argument, so none of them touch the filesystem or a database.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from newspulse import brain, prose

PROMPTS = Path(brain.__file__).parent / "prompts"
GOLDEN = Path(__file__).parent / "fixtures" / "prompts"

#: A block set with no relationship to the shipped one, so a test using it fails
#: if resolution ever quietly falls back to the real files.
FIXTURE_BLOCKS = {
    "alpha": "ERSTER BLOCK\n\nDer erste Maßstab.",
    "beta": "ZWEITER BLOCK\n\nDer zweite Maßstab.",
}


def _prompt_files() -> list[Path]:
    return sorted(PROMPTS.glob("*.txt"))


def _flat(text: str) -> str:
    """Whitespace-insensitive and case-insensitive, because the prompts are hard
    wrapped and a restated standard would otherwise hide behind a line break.
    That is not hypothetical: it is how the one real restatement in this
    migration escaped the first scan."""
    return re.sub(r"\s+", " ", text).strip().lower()


# --------------------------------------------------------------------------
# Resolution: pure, explicit about its source, loud about what it cannot find.
# --------------------------------------------------------------------------


def test_every_shipped_block_resolves_to_text():
    for key, text in brain.blocks().items():
        assert text.strip(), f"block {key!r} is empty"
        assert brain.block(key) == text


def test_block_keys_are_stable_snake_case():
    """The key is the contract a prompt and, from BRN-02, a database row hold."""
    for key in brain.blocks():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key), key


def test_block_resolves_against_an_injected_source_without_touching_disk():
    assert brain.block("alpha", FIXTURE_BLOCKS) == FIXTURE_BLOCKS["alpha"]


def test_unknown_block_raises_rather_than_returning_empty():
    with pytest.raises(brain.UnknownBlock) as excinfo:
        brain.block("nonexistent", FIXTURE_BLOCKS)
    # The message names what *is* available, because the usual cause is a typo.
    assert "alpha" in str(excinfo.value)


def test_compose_expands_an_include_from_the_given_source():
    composed = brain.compose("Vorher\n\n{{brain:alpha}}\n\nNachher\n", FIXTURE_BLOCKS)
    assert FIXTURE_BLOCKS["alpha"] in composed
    assert "Vorher" in composed and "Nachher" in composed


def test_compose_expands_every_occurrence_of_a_repeated_include():
    composed = brain.compose("{{brain:beta}}\n\n{{brain:beta}}", FIXTURE_BLOCKS)
    assert composed.count("ZWEITER BLOCK") == 2


def test_compose_strips_the_declaration_header_from_the_rendered_prompt():
    """The header addresses the person editing the file, not the model."""
    composed = brain.compose("#blocks: alpha\n\nAufgabe.\n", FIXTURE_BLOCKS)
    assert "#blocks" not in composed
    assert "Aufgabe." in composed


def test_compose_fails_loudly_at_render_for_an_unknown_block():
    """The quiet alternative is a prompt that silently drops the standard it
    declared and produces text that looks fine."""
    with pytest.raises(brain.UnknownBlock):
        brain.compose("{{brain:ghost}}", FIXTURE_BLOCKS)


def test_compose_leaves_template_placeholders_untouched():
    """`$name` belongs to the caller's string.Template substitution, which runs
    after composition. Eating one here would be invisible until a render fails."""
    composed = brain.compose("$client_profile\n{{brain:alpha}}\n$days", FIXTURE_BLOCKS)
    assert "$client_profile" in composed and "$days" in composed


def test_compose_is_pure_for_the_same_text_and_source():
    text = "#blocks: alpha\n{{brain:alpha}}"
    assert brain.compose(text, FIXTURE_BLOCKS) == brain.compose(text, FIXTURE_BLOCKS)


def test_declared_and_included_read_the_two_markers_apart():
    text = "#blocks: alpha, beta\n\n{{brain:alpha}}"
    assert brain.declared(text) == ("alpha", "beta")
    assert brain.included(text) == ("alpha",)


def test_has_declaration_separates_an_empty_list_from_a_missing_header():
    """"This prompt carries no standards" and "someone forgot the header" are
    different claims, and only the second is a mistake."""
    assert brain.has_declaration("#blocks:\n\nAufgabe.")
    assert brain.declared("#blocks:\n\nAufgabe.") == ()
    assert not brain.has_declaration("Aufgabe.")


# --------------------------------------------------------------------------
# Version: changes when a standard changes, and not otherwise.
# --------------------------------------------------------------------------


def test_version_is_stable_for_an_unchanged_block_set():
    assert brain.version(FIXTURE_BLOCKS) == brain.version(dict(FIXTURE_BLOCKS))


def test_version_changes_when_a_block_text_changes():
    edited = {**FIXTURE_BLOCKS, "alpha": "ERSTER BLOCK\n\nEin anderer Maßstab."}
    assert brain.version(edited) != brain.version(FIXTURE_BLOCKS)


def test_version_changes_when_a_block_is_added():
    added = {**FIXTURE_BLOCKS, "gamma": "DRITTER BLOCK"}
    assert brain.version(added) != brain.version(FIXTURE_BLOCKS)


def test_version_does_not_collide_when_text_moves_between_blocks():
    """Concatenating key and text without separators would make {"a": "xy"} and
    {"ax": "y"} hash alike, which is exactly the edit a consultant makes."""
    assert brain.version({"a": "xy"}) != brain.version({"ax": "y"})


# --------------------------------------------------------------------------
# Structural: DEC-2 option B, enforced by a test rather than by convention.
# --------------------------------------------------------------------------

#: What each prompt must still carry, read off the ten prompt files as they stood
#: before this migration. This is the "no standard was lost in the move" list: if
#: an include is deleted, the prompt silently stops carrying a standard it used
#: to state inline, and only this map notices.
CARRIED_BEFORE = {
    "advisory.txt": {"evidence", "refusal", "no_invention"},
    "analysis.txt": {"no_invention"},
    "angle.txt": {"position", "journalistic_value", "refusal", "house_style",
                  "evidence", "no_invention"},
    "coach.txt": {"evidence", "refusal", "no_invention"},
    "crosscheck.txt": {"no_invention", "position", "house_style", "evidence"},
    "guide.txt": {"no_invention"},
    "industry.txt": {"no_invention"},
    "outreach.txt": {"journalistic_value", "position", "house_style",
                     "evidence", "no_invention"},
    "rivals.txt": {"no_invention", "refusal"},
    "themes.txt": {"journalistic_value"},
}

#: Phrases that only appear in a prompt if somebody wrote a standard out again
#: instead of including it. Deliberately whole rule clauses rather than single
#: words: `crosscheck.txt` has to be able to *name* the thing it checks for
#: ("4. WERBESPRACHE") without that counting as restating the rule. A label
#: points at a standard; a clause restates one.
RESTATEMENT_TELLS = {
    "house_style": ["Keine Superlative", "keine Werbesprache", "Keine Gedankenstriche",
                    "Erkennungszeichen maschinell", "keine Floskeln", "Selbstlob"],
    "position": ["beobachten die Entwicklung aufmerksam", "eigene Übertreibung",
                 "in vier Wochen widerlegt", "angehängte Meinung",
                 "Der Text steht auf der zweiten"],
    "journalistic_value": ["zweihundert PR-Anschreiben", "entscheidet in zehn Sekunden",
                           "Serienbrief mit eingesetztem Namen",
                           "Anbiederung ersetzt kein Angebot"],
    "evidence": ["nie die vollständigen Artikel", "Schlagzeilen und kurze Feed-Anrisse",
                 "ohne Beleg ist wertlos", "statt einer erfundenen Zahl",
                 "viele beliebige"],
    "no_invention": ["Raten ist Erfinden", "Namensgleichheit",
                     "Nichts aus der Branche herleiten", "Was nicht dasteht, fehlt eben",
                     "Nie erfunden werden"],
    "refusal": ["leere Antwort die richtige", "mehr Zeit als eine leere Spalte",
                "nach dem dritten Mal ignoriert", "keine Fehlleistung",
                "erfinde keinen Anlass"],
}


def test_all_ten_prompts_are_accounted_for():
    """A new prompt has to be added to the map above deliberately, so it cannot
    join the codebase without anyone deciding which standards govern it."""
    assert {p.name for p in _prompt_files()} == set(CARRIED_BEFORE)


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_declares_its_blocks(path: Path):
    assert brain.has_declaration(path.read_text("utf-8")), (
        f"{path.name} has no '#blocks:' header"
    )


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_declaration_matches_what_it_includes(path: Path):
    """The header is what a person reads to know which standards apply. If it can
    drift from the includes it is worse than nothing, so it cannot drift."""
    raw = path.read_text("utf-8")
    assert set(brain.declared(raw)) == set(brain.included(raw)), (
        f"{path.name}: declared {brain.declared(raw)} but includes {brain.included(raw)}"
    )


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_composes_against_the_shipped_blocks(path: Path):
    composed = brain.compose(path.read_text("utf-8"))
    assert "{{brain:" not in composed
    assert composed.strip()


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_still_carries_every_standard_it_carried_before(path: Path):
    included = set(brain.included(path.read_text("utf-8")))
    missing = CARRIED_BEFORE[path.name] - included
    assert not missing, f"{path.name} no longer carries: {sorted(missing)}"


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_does_not_restate_a_standard_inline(path: Path):
    """DEC-2 option B. The layer is only load-bearing while this passes."""
    raw = _flat(path.read_text("utf-8"))
    restated = [
        (key, tell)
        for key, tells in RESTATEMENT_TELLS.items()
        for tell in tells
        if _flat(tell) in raw
    ]
    assert not restated, (
        f"{path.name} restates a standard inline instead of including it: {restated}"
    )


def test_the_restatement_rule_would_actually_catch_a_restatement():
    """A guard that cannot fail is a guard that is not there. Every tell must
    really occur in the block it belongs to, or this suite is passing on
    phrases nobody would ever write."""
    shipped = brain.blocks()
    for key, tells in RESTATEMENT_TELLS.items():
        block_text = _flat(shipped[key])
        for tell in tells:
            assert _flat(tell) in block_text, f"{key!r} no longer says {tell!r}"


def test_every_block_is_included_by_at_least_one_prompt():
    """An orphaned block is a standard nobody reads, which reads as doctrine and
    behaves as decoration."""
    used = {key for p in _prompt_files() for key in brain.included(p.read_text("utf-8"))}
    assert set(brain.blocks()) - used == set()


def test_the_dash_rule_stays_in_code_and_not_only_in_a_block():
    """`prose.plain()` is the enforcement and `house_style` is the ask. The ask
    is worth something and it is not sufficient: a model complies for two
    paragraphs and then relapses, and nobody reads the third one closely enough.
    If this ever becomes only a block, the rule has quietly stopped holding."""
    assert prose.plain("Ein Satz — mit Gedankenstrich.") == "Ein Satz, mit Gedankenstrich."
    assert "Gedankenstrich" in brain.block("house_style")


# --------------------------------------------------------------------------
# Golden files: an edit to a block is a reviewable diff, not a surprise.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_renders_to_its_golden_file(path: Path):
    composed = brain.compose(path.read_text("utf-8"))
    golden = GOLDEN / path.name
    if os.environ.get("NEWSPULSE_REGOLD"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(composed, "utf-8")
    assert golden.exists(), (
        f"no golden file for {path.name}; run NEWSPULSE_REGOLD=1 pytest tests/test_brain.py"
    )
    assert composed == golden.read_text("utf-8"), (
        f"{path.name} renders differently than its golden file. If a block changed "
        f"on purpose, regenerate with NEWSPULSE_REGOLD=1 and review the diff."
    )


def test_editing_a_block_moves_exactly_the_prompts_that_include_it():
    """The claim the golden files exist to support: change one standard, and the
    prompts that carry it all move, and the others do not."""
    edited = {**brain.blocks(), "refusal": "GEÄNDERTER MASSSTAB\n\nEin neuer Satz."}
    moved, still = set(), set()
    for path in _prompt_files():
        raw = path.read_text("utf-8")
        (moved if brain.compose(raw, edited) != brain.compose(raw) else still).add(path.name)

    includes_refusal = {
        p.name for p in _prompt_files() if "refusal" in brain.included(p.read_text("utf-8"))
    }
    assert moved == includes_refusal
    assert still == {p.name for p in _prompt_files()} - includes_refusal
    assert moved, "no prompt includes 'refusal', so this test proves nothing"
