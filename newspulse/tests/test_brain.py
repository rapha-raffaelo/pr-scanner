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

The *store* tests at the bottom are the ones that do use a database, because the
thing they are about is an event: who changed a standard, when, and which version
that produced. They drive ``brain.edit``/``brain.revert`` directly and the three
settings routes through ``TestClient``, against an in-memory SQLite database.
Nothing here reaches a model.
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
import re
from pathlib import Path
from string import Template

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import brain, i18n, prose
from newspulse.models import Base, BrainOverride
from newspulse.web.app import create_app, get_db

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


def test_unknown_block_is_not_a_keyerror_a_render_handler_could_swallow():
    """`Template.substitute` raises KeyError for a missing placeholder, so a
    caller that wraps a render in `except KeyError` would catch an unresolved
    block too and compose without the standard: the exact silence this raises
    to prevent. `except LookupError` still catches it deliberately."""
    with pytest.raises(brain.UnknownBlock) as excinfo:
        brain.block("nonexistent", FIXTURE_BLOCKS)
    assert not isinstance(excinfo.value, KeyError)
    assert isinstance(excinfo.value, LookupError)


def test_shipped_blocks_cannot_be_mutated_through_the_cache():
    """`shipped()` is cached, so it hands out one object for the process. BRN-02
    layers database overrides in front of it, and `merged = shipped();
    merged.update(rows)` would make one client's edits every client's standards
    until restart."""
    with pytest.raises(TypeError):
        brain.shipped()["alpha"] = "injected"  # type: ignore[index]
    assert "alpha" not in brain.blocks()


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


@pytest.mark.parametrize(
    "marker",
    ["{{brain:Alpha}}", "{{brain:al-pha}}", "{{brain:}}", "{{brain}}", "{{ brain alpha }}"],
)
def test_compose_raises_for_a_mistyped_marker_instead_of_shipping_it(marker: str):
    """The hole the first version left open. A marker the pattern did not match
    was not an unknown block, it was not a marker at all, so it survived
    composition and went to the model as the literal string `{{brain:Alpha}}`:
    the standard silently absent and the braces in the prompt. Every spelling
    close enough to be a typo has to land inside the capture and raise."""
    with pytest.raises(brain.UnknownBlock):
        brain.compose(f"Aufgabe.\n{marker}\n", FIXTURE_BLOCKS)


def test_compose_expands_an_include_a_block_itself_contains():
    """Composition is closed over block text: a `{{brain:…}}` or a `#blocks:`
    line inside a block obeys the same two rules as one inside a prompt. Moot
    while blocks are repo files, and live from BRN-02, which is the same reason
    `$` is escaped."""
    source = {"outer": "#blocks: inner\nAUSSEN\n{{brain:inner}}", "inner": "INNEN"}
    composed = brain.compose("{{brain:outer}}", source)
    assert composed.strip() == "AUSSEN\nINNEN"


def test_compose_stops_rather_than_expanding_two_blocks_into_each_other():
    """A cycle is reachable as soon as block text is a field somebody types in,
    and without the cap it is an out-of-memory kill that looks like a slow render."""
    with pytest.raises(brain.BlockCycle):
        brain.compose("{{brain:links}}", {"links": "{{brain:rechts}}", "rechts": "{{brain:links}}"})


def test_compose_leaves_template_placeholders_untouched():
    """`$name` belongs to the caller's string.Template substitution, which runs
    after composition. Eating one here would be invisible until a render fails."""
    composed = brain.compose("$client_profile\n{{brain:alpha}}\n$days", FIXTURE_BLOCKS)
    assert "$client_profile" in composed and "$days" in composed


def test_compose_escapes_a_dollar_sign_inside_block_text():
    """Composition runs before the caller's `Template.substitute`. A `$` a
    consultant types into a block from BRN-02's settings screen would otherwise
    become a live placeholder and raise from `substitute` at render time, in a
    call site that has no idea a block was involved."""
    composed = brain.compose("$client_profile\n{{brain:preis}}", {"preis": "ab $500"})
    assert Template(composed).substitute(client_profile="ACME").strip() == (
        "ACME\nab $500"
    )


def test_compose_is_pure_for_the_same_text_and_source():
    text = "#blocks: alpha\n{{brain:alpha}}"
    assert brain.compose(text, FIXTURE_BLOCKS) == brain.compose(text, FIXTURE_BLOCKS)


def test_declared_and_included_read_the_two_markers_apart():
    text = "#blocks: alpha, beta\n\n{{brain:alpha}}"
    assert brain.declared(text) == ("alpha", "beta")
    assert brain.included(text) == ("alpha",)


def test_declared_reads_every_header_and_not_only_the_first():
    """`compose` strips every `#blocks:` line. If `declared` read only the first,
    a second header would vanish from the rendered prompt while the keys it names
    went unreported, and the test below that holds the declaration to the
    includes would be comparing against half a declaration."""
    text = "#blocks: alpha\n\nAufgabe.\n\n#blocks: beta\n"
    assert brain.declared(text) == ("alpha", "beta")
    assert "#blocks" not in brain.compose(text, FIXTURE_BLOCKS)


def test_has_declaration_separates_an_empty_list_from_a_missing_header():
    """"This prompt carries no standards" and "someone forgot the header" are
    different claims, and only the second is a mistake."""
    assert brain.has_declaration("#blocks:\n\nAufgabe.")
    assert brain.declared("#blocks:\n\nAufgabe.") == ()
    assert not brain.has_declaration("Aufgabe.")


# --------------------------------------------------------------------------
# The override chain: shipped is the default, the tool holds the disagreement.
# --------------------------------------------------------------------------


def test_an_override_wins_over_the_shipped_text_for_the_same_key():
    resolved = brain.resolved({"refusal": "EIN ANDERER MASSSTAB"})
    assert resolved["refusal"] == "EIN ANDERER MASSSTAB"
    # And nothing else moved with it.
    assert resolved["house_style"] == brain.shipped()["house_style"]


def test_a_block_with_no_override_is_still_the_shipped_text():
    assert brain.resolved({}) == dict(brain.shipped())


def test_resolving_does_not_mutate_the_shipped_set():
    """`shipped()` is cached and hands out one object for the process, so a
    resolution that updated it in place would make one edit everyone's
    standards until restart."""
    brain.resolved({"refusal": "ÜBERSCHRIEBEN"})
    assert brain.shipped()["refusal"] != "ÜBERSCHRIEBEN"


def test_an_override_for_a_block_that_no_longer_ships_is_orphaned_not_dropped():
    """A block renamed in the repository leaves its override behind. Dropping it
    would make a live edit look like one that was never made."""
    overrides = {"refusal": "x", "alter_name": "y"}
    assert brain.orphaned(overrides) == ("alter_name",)
    assert brain.resolved(overrides)["alter_name"] == "y"


def test_composition_reads_the_installed_override_source(monkeypatch):
    """The seam the whole story hangs on: a prompt that names no source composes
    against what the tool holds today, not against the files on disk."""
    monkeypatch.setattr(brain, "_override_source", lambda: {"refusal": "SCHWEIGEN."})
    assert brain.block("refusal") == "SCHWEIGEN."
    assert "SCHWEIGEN." in brain.compose("{{brain:refusal}}")


# --------------------------------------------------------------------------
# Structural: DEC-2 option B, enforced by a test rather than by convention.
# --------------------------------------------------------------------------

#: What each prompt must still carry, re-derived clause by clause from the ten
#: prompt files as they stood at 5862614, the commit before they were rewritten
#: (``git show 5862614:src/newspulse/prompts/<name>``). This is the "no standard
#: was lost in the move" list: if an include is deleted, the prompt silently
#: stops carrying a standard it used to state inline, and only this map notices.
#:
#: A key is listed only where the original demonstrably stated that standard,
#: not where the standard would have been reasonable to hold. Reading it the
#: generous way is what let the first version of this map list four keys for
#: crosscheck.txt when the original carried six: two standards could have been
#: deleted from it and the suite would have stayed green.
CARRIED_BEFORE = {
    # "Eine Maßnahme ohne Beleg ist wertlos"; "nur Schlagzeilen und kurze
    # Zusammenfassungen"; "Lieber drei gute als sechs beliebige" / "erfinde keine
    # Betriebsamkeit"; "Empfiehl auch das Unterlassen".
    "advisory.txt": {"evidence", "refusal"},
    # "eine bloße Namensgleichheit ... NICHT als relevant".
    "analysis.txt": {"no_invention"},
    # All six: the pauschale/belastbare split, "kein Anlass", "erfinde keinen
    # Anlass", the dash and superlative rules, "nur Schlagzeilen und kurze
    # Feed-Anrisse", "kein Datum, keine Zahl, keinen Namen".
    "angle.txt": {"position", "journalistic_value", "refusal", "house_style",
                  "evidence", "no_invention"},
    # "Ein Befund ohne Beleg gehört nicht in den Bericht"; "leere Liste ... keine
    # Fehlleistung".
    "coach.txt": {"evidence", "refusal"},
    # All six, one per check plus the frame: ERFUNDENES, "NIE die vollständigen
    # Artikel", the overclaim check, "zweihundert PR-Anschreiben" / "Serienbrief
    # mit eingesetztem Namen", WERBESPRACHE and MASCHINENSPUR, "Eine Prüfung, die
    # immer etwas findet" -- that last one and *only* that one, which is why it is
    # `false_alarm` here and not `refusal`. See CONTRADICTIONS: `refusal` also
    # licenses the empty answer, and MessageReview reads an empty answer as
    # send=True, so the one prompt that must never return nothing was the one
    # prompt being told that returning nothing is right.
    "crosscheck.txt": {"no_invention", "position", "journalistic_value",
                       "house_style", "evidence", "false_alarm"},
    # "Nichts ergänzen, nichts aus der Branche herleiten ... Was nicht dasteht,
    # fehlt eben."
    "guide.txt": {"no_invention"},
    # "Keine Selbstbeschreibung des Unternehmens und kein Produktname. Der
    # Begriff muss auch in Meldungen vorkommen, in denen dieses Unternehmen nicht
    # auftaucht." One clause, which is `press_relevance` and not the whole of
    # `journalistic_value`: this prompt emits a search term, not a pitch. The
    # original carried no invention rule either: it instructs the model to derive
    # the industry from name and website, which is what no_invention forbids. See
    # test_industry_does_not_forbid_the_inference_it_asks_for.
    "industry.txt": {"press_relevance"},
    # "an einen Menschen ... nicht an einen Verteiler"; "die pauschale Lesart ist
    # die Falle"; the dash and Werbeton rules; "Du siehst nur Schlagzeilen";
    # "ohne erfundene Personennamen oder Kontaktdaten".
    "outreach.txt": {"journalistic_value", "position", "house_style",
                     "evidence", "no_invention"},
    # "Erfinde keinen Namen und rate nicht"; "leere Liste ... keine Fehlleistung".
    "rivals.txt": {"no_invention", "refusal"},
    # "Keine Selbstbeschreibung ... daraus entsteht kein Impuls, sondern
    # Eigenwerbung." Same clause, same reason as industry.txt.
    "themes.txt": {"press_relevance"},
}

#: Standards a prompt did *not* carry before and now does. Every one is a
#: decision, so every one is written down: the point of a shared layer is that
#: including a block is cheap, and cheap is how ten prompts quietly grow a
#: standard nobody chose for them. Together with CARRIED_BEFORE this accounts for
#: every include in every prompt, so an addition cannot arrive as a side effect.
ADDED_IN_MIGRATION = {
    # advisory writes drafts that go out as they stand, to a Redaktion or as a
    # Sprachregelung. Both standards govern sendable text and the original
    # relied on the model not needing to be told.
    "advisory.txt": {"no_invention", "house_style"},
    # coach quotes coverage back at the consultant. The original forbade
    # unsupported claims but never named invented quotes as the failure.
    "coach.txt": {"no_invention"},
    "analysis.txt": set(),
    "angle.txt": set(),
    "crosscheck.txt": set(),
    "guide.txt": set(),
    "industry.txt": set(),
    "outreach.txt": set(),
    "rivals.txt": set(),
    "themes.txt": set(),
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
                "keine Fehlleistung", "erfinde keinen Anlass"],
    "false_alarm": ["nach dem dritten Mal ignoriert", "Ist etwas in Ordnung, sag das"],
    "press_relevance": ["ohnehin nicht schreibt", "Eigenwerbung, kein Impuls"],
}

#: The other half of DEC-2's guard, and the half RESTATEMENT_TELLS structurally
#: cannot hold: a standard written out again *in different words*. A paraphrase
#: appears in no block by definition, so
#: `test_the_restatement_rule_would_actually_catch_a_restatement` cannot validate
#: these the way it validates the map above, and the list is a judgement call
#: rather than a derivation from the blocks.
#:
#: It catches the short forms somebody reaches for when they have forgotten the
#: block exists. It does not catch a careful paraphrase, and no phrase list will:
#: what actually holds AC #4 for a rewording is CARRIED_BEFORE plus
#: ADDED_IN_MIGRATION plus CONTRADICTIONS, which account for every clause a
#: prompt composes rather than for the words it avoids.
PARAPHRASE_TELLS = {
    "house_style": ["vermeide Superlative", "verzichte auf Superlative",
                    "keine Marketingsprache"],
    "no_invention": ["nichts erfinden", "erfinde nichts", "nicht erfinden"],
    "refusal": ["lieber nichts als"],
}

#: Clauses that must not reach a given prompt's model, checked against the
#: *composed* text rather than the include list. The per-block accounting above
#: asks "was this key carried before?" and cannot see that a block states four
#: things where the prompt only ever needed one: that is how industry.txt and
#: themes.txt, which emit a durable search term, came to compose "Eine Struktur,
#: die seit Jahren so ist, ist kein Anlass" and two hundred PR letters' worth of
#: advice about writing to a person. Every entry here is a defect that shipped
#: once. The fix was to split the block; this is what stops the split being
#: quietly undone by an include that looks harmless in a diff.
CONTRADICTIONS = {
    # Both prompts emit a search term a news filter runs against. A Branchenbegriff
    # is a long-standing structure by construction, and neither prompt composes a
    # letter to anybody.
    "industry.txt": ["Eine Struktur, die seit Jahren so ist, ist kein Anlass",
                     "zweihundert PR-Anschreiben", "an einen Menschen gerichtet"],
    "themes.txt": ["Eine Struktur, die seit Jahren so ist, ist kein Anlass",
                   "zweihundert PR-Anschreiben", "an einen Menschen gerichtet"],
    # The safety gate, and the one prompt whose empty answer is not a result:
    # MessageReview defaults to send=True, so "nichts zurückgeben" reads as an
    # unconditional approval of a letter nobody reviewed.
    "crosscheck.txt": ["die leere Antwort die richtige", "Nichts zu liefern ist ein Ergebnis"],
}


def test_all_ten_prompts_are_accounted_for():
    """A new prompt has to be added to the maps above deliberately, so it cannot
    join the codebase without anyone deciding which standards govern it."""
    names = {p.name for p in _prompt_files()}
    assert names == set(CARRIED_BEFORE)
    assert names == set(ADDED_IN_MIGRATION)


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
    # Tuples, not sets: the header claims an order ("in the order written") and a
    # set comparison let three prompts list their standards in an order the
    # rendered prompt does not use, which is a header that reads as documentation
    # and is not.
    assert brain.declared(raw) == brain.included(raw), (
        f"{path.name}: declared {brain.declared(raw)} but includes {brain.included(raw)}"
    )


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_composes_against_the_shipped_blocks(path: Path):
    composed = brain.compose(path.read_text("utf-8"))
    # Not `"{{brain:" not in composed`: that spelling is the one an unresolved
    # marker is least likely to have, and it would have missed `{{Brain:evidence}}`.
    assert not re.search(r"\{\{[^}]*brain", composed, re.IGNORECASE)
    assert composed.strip()


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_still_carries_every_standard_it_carried_before(path: Path):
    included = set(brain.included(path.read_text("utf-8")))
    missing = CARRIED_BEFORE[path.name] - included
    assert not missing, f"{path.name} no longer carries: {sorted(missing)}"


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_includes_nothing_it_was_not_given_on_purpose(path: Path):
    """The other half of the map, and the one the golden files cannot supply:
    they were captured after the migration, so they notice future change and
    prove nothing about what the move added. Including a block costs one line,
    which is exactly how a prompt grows a standard nobody chose for it, and how
    the analyzer's per-batch prompt would quietly take on advice about weighing
    few solid claims over many arbitrary ones."""
    included = set(brain.included(path.read_text("utf-8")))
    unaccounted = included - CARRIED_BEFORE[path.name] - ADDED_IN_MIGRATION[path.name]
    assert not unaccounted, (
        f"{path.name} includes {sorted(unaccounted)}, which it did not carry before. "
        f"If that is deliberate, add it to ADDED_IN_MIGRATION with the reason."
    )


def test_industry_does_not_forbid_the_inference_it_asks_for():
    """industry.txt tells the model to derive an unknown client's field from its
    name and website, and industry.propose() has no fallback for an empty list:
    the search degrades to nothing for exactly the small, unknown clients that
    bullet was written for. no_invention says "Nichts aus der Branche herleiten"
    and "Raten ist Erfinden", so composing the two puts a contradiction in front
    of the model on the onboarding path."""
    raw = (PROMPTS / "industry.txt").read_text("utf-8")
    assert "no_invention" not in brain.included(raw)
    assert _flat("leite die Branche aus Name und Website ab") in _flat(raw)


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_does_not_restate_a_standard_inline(path: Path):
    """DEC-2 option B. The layer is only load-bearing while this passes."""
    raw = _flat(path.read_text("utf-8"))
    restated = [
        (key, tell)
        for tells_by_key in (RESTATEMENT_TELLS, PARAPHRASE_TELLS)
        for key, tells in tells_by_key.items()
        for tell in tells
        if _flat(tell) in raw
    ]
    assert not restated, (
        f"{path.name} restates a standard inline instead of including it: {restated}"
    )


def test_the_restatement_rule_would_actually_catch_a_restatement():
    """A guard that cannot fail is a guard that is not there. Every tell must
    really occur in the block it belongs to, or this suite is passing on
    phrases nobody would ever write.

    PARAPHRASE_TELLS is deliberately not checked here: a paraphrase is by
    definition not in the block, so the same proof is not available for it."""
    shipped = brain.blocks()
    for key, tells in RESTATEMENT_TELLS.items():
        block_text = _flat(shipped[key])
        for tell in tells:
            assert _flat(tell) in block_text, f"{key!r} no longer says {tell!r}"


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_composed_prompt_carries_no_clause_its_own_task_contradicts(path: Path):
    """The clause-level half of DEC-2, and the one the include list cannot give.
    A prompt does not compose a block, it composes every sentence in it, and a
    block that is right for four prompts can be wrong for the fifth in a way no
    per-key accounting can see."""
    composed = _flat(brain.compose(path.read_text("utf-8")))
    everything = _flat("\n".join(brain.blocks().values()))
    for clause in CONTRADICTIONS.get(path.name, []):
        # A reworded block would otherwise retire this guard without anyone
        # deciding to, and it would keep passing while it did.
        assert _flat(clause) in everything, f"no block says {clause!r} any more"
        assert _flat(clause) not in composed, (
            f"{path.name} composes {clause!r}, which its own task contradicts"
        )


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
        # Not falling through to the assert below: it would compare the file to
        # what it was just handed and pass by construction. If the variable ever
        # leaks into a CI environment, a green run has to mean the goldens were
        # checked, not rewritten.
        pytest.skip("golden regenerated; re-run without NEWSPULSE_REGOLD to verify")
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


# --------------------------------------------------------------------------
# The store: a change to a standard is an event with a time, an author and a
# number, and the shipped text is what a revert brings back.
# --------------------------------------------------------------------------

#: A fixed clock, so a version's timestamp is asserted rather than approximated.
FIXED_CLOCK = dt.datetime(2026, 9, 3, 9, 30, tzinfo=dt.UTC)

#: A block that ships, used wherever a test needs a real key. Read off the
#: shipped set rather than typed, so renaming a block file fails these loudly
#: instead of leaving them testing a key nobody has.
A_BLOCK = "refusal"


@pytest.fixture
def factory():
    """A sessionmaker on a fresh in-memory database with the schema built.

    StaticPool keeps every session on one connection, so a POST's write is
    visible to the GET that follows it.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def client(factory):
    """A TestClient whose routes read and write the fixture database."""
    app = create_app()

    def _override_get_db():
        open_session = factory()
        try:
            yield open_session
        finally:
            open_session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def test_a_fresh_install_has_no_overrides_and_version_zero(session):
    """Nothing has been changed, which is a true statement about the standards
    and a different one from "we do not know", which is what BRN-03 renders for
    a text stored before there was anything to stamp."""
    assert brain.stored(session) == {}
    assert brain.version(session) == 0


def test_editing_a_block_stores_an_override_and_bumps_the_version_once(session):
    change = brain.edit(session, A_BLOCK, "Schweigen ist meistens richtig.")

    assert change is not None
    assert brain.stored(session) == {A_BLOCK: "Schweigen ist meistens richtig."}
    assert brain.version(session) == 1
    assert change.version == 1


def test_an_edit_takes_effect_on_the_next_composition_without_a_restart(
    session, monkeypatch
):
    """The claim the panel makes when it says an edit is live. Nothing is
    reloaded, no process is restarted: the next prompt that composes the block
    reads what was just typed."""
    monkeypatch.setattr(brain, "_override_source", lambda: brain.stored(session))
    before = brain.compose("{{brain:%s}}" % A_BLOCK)

    brain.edit(session, A_BLOCK, "NEUER MASSSTAB\n\nEin Satz.")

    after = brain.compose("{{brain:%s}}" % A_BLOCK)
    assert "NEUER MASSSTAB" in after
    assert after != before


#: Every module that turns a shipped prompt file into a template, paired with a
#: block that prompt includes. The point of the list is that it is exhaustive: an
#: edit is only "live" if it reaches the prompt a generator actually sends, and a
#: loader that memoises its template is invisible to any test that asserts
#: against ``brain.compose`` directly.
PROMPT_LOADERS = [
    ("advisor", "no_invention"),
    ("analyzer", "no_invention"),
    ("angles", "position"),
    ("coach", "refusal"),
    ("guide", "no_invention"),
    ("industry", "press_relevance"),
    ("outreach", "journalistic_value"),
    ("rivals", "refusal"),
    ("themes", "press_relevance"),
]


@pytest.mark.parametrize(("module_name", "key"), PROMPT_LOADERS)
def test_an_edit_reaches_every_prompt_a_generator_sends(
    session, monkeypatch, module_name: str, key: str
):
    """AC 2 held where it is written about, not one layer below it.

    ``analyzer._prompt_template`` was ``@lru_cache(maxsize=1)``, which was correct
    while the blocks only changed with a deployment. Runs happen in threads inside
    the long-lived web process, so once a block became editable that cache meant
    an edited standard governed nine prompts immediately and article analysis —
    the highest-volume path there is — only after a container restart. Asserting
    against ``brain.compose`` cannot see that; this composes through the loader.
    """
    module = importlib.import_module(f"newspulse.{module_name}")
    monkeypatch.setattr(brain, "_override_source", lambda: brain.stored(session))
    before = module._prompt_template().template

    brain.edit(session, key, f"NEUER MASSSTAB FUER {key.upper()}\n\nEin Satz.")

    after = module._prompt_template().template
    assert f"NEUER MASSSTAB FUER {key.upper()}" in after
    assert after != before


def test_each_change_gets_its_own_version_across_blocks(session):
    """The counter is portfolio-wide, not per block: one integer has to name what
    the whole house believed at a moment."""
    first = brain.edit(session, "refusal", "Erst.")
    second = brain.edit(session, "house_style", "Zweitens.")

    assert (first.version, second.version) == (1, 2)
    assert brain.version(session) == 2


def test_an_edit_records_when_it_happened_and_who_made_it(session):
    change = brain.edit(
        session, A_BLOCK, "Ein Satz.", edited_by="lucas", now=FIXED_CLOCK
    )

    assert change.edited_at == FIXED_CLOCK
    assert change.edited_by == "lucas"


def test_an_unattributed_edit_says_a_person_made_it_rather_than_naming_nobody(session):
    """The dashboard has one shared credential and no user table. "mensch" is
    what that honestly is; a blank author would read as a change the machine
    made to its own standards."""
    change = brain.edit(session, A_BLOCK, "Ein Satz.")
    assert change.edited_by == brain.editor()
    assert change.edited_by.strip()


def test_re_saving_the_same_text_is_not_a_new_version(session):
    """A version nobody can point at a difference for is worse than no version:
    BRN-03 stamps texts with these numbers, and a stamp has to mean something
    changed."""
    brain.edit(session, A_BLOCK, "Ein Satz.")
    again = brain.edit(session, A_BLOCK, "Ein Satz.")

    assert again is None
    assert brain.version(session) == 1


def test_saving_the_shipped_text_unchanged_stores_no_override(session):
    """Opening a block, changing nothing and pressing save is not a decision to
    override it."""
    assert brain.edit(session, A_BLOCK, brain.shipped()[A_BLOCK]) is None
    assert brain.stored(session) == {}


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "\t \r\n "])
def test_an_empty_or_whitespace_only_block_is_refused(session, empty: str):
    """A prompt composing an empty standard drops it in silence and goes on
    producing text that looks fine, so the refusal is at the edit rather than at
    the render."""
    with pytest.raises(ValueError):
        brain.edit(session, A_BLOCK, empty)

    assert brain.stored(session) == {}
    assert brain.version(session) == 0


def test_editing_a_block_that_does_not_exist_raises_rather_than_creating_one(session):
    """The key is in the URL. Without this a typo would conjure a block no
    prompt reads and no panel would ever explain."""
    with pytest.raises(brain.UnknownBlock):
        brain.edit(session, "gibt_es_nicht", "Ein Satz.")
    assert brain.stored(session) == {}


def test_a_textarea_submission_does_not_carry_windows_line_endings_into_a_prompt(
    session,
):
    """Browsers submit \\r\\n from a textarea and the blocks use \\n. Invisible in
    the interface, and a diff in every golden file the block touches."""
    brain.edit(session, A_BLOCK, "Erste Zeile.\r\nZweite Zeile.\r\n")
    assert brain.stored(session)[A_BLOCK] == "Erste Zeile.\nZweite Zeile."


def test_reverting_restores_the_shipped_text_exactly(session):
    brain.edit(session, A_BLOCK, "Etwas anderes.")
    brain.revert(session, A_BLOCK)

    assert brain.stored(session) == {}
    assert brain.resolved(brain.stored(session))[A_BLOCK] == brain.shipped()[A_BLOCK]


def test_reverting_is_itself_a_recorded_change_rather_than_a_disappearance(session):
    """The override row stays and the revert is a row of its own, so the history
    reads "changed, then changed back" and not "nothing ever happened here"."""
    brain.edit(session, A_BLOCK, "Etwas anderes.", now=FIXED_CLOCK)
    undo = brain.revert(session, A_BLOCK, now=FIXED_CLOCK + dt.timedelta(hours=1))

    assert undo is not None
    assert undo.version == 2
    assert brain.version(session) == 2
    assert [row.version for row in brain.history(session, A_BLOCK)] == [2, 1]


def test_reverting_a_block_that_was_never_overridden_is_not_a_version(session):
    """A second click on a page held open in another tab must not spend a
    version on a no-op."""
    assert brain.revert(session, A_BLOCK) is None
    assert brain.version(session) == 0


def test_the_history_carries_the_previous_wording_and_not_only_its_date(session):
    """A date says a change happened and nothing about what the house believed
    afterwards, which is the only question a history is opened for."""
    brain.edit(session, A_BLOCK, "Erste Fassung.", edited_by="lucas", now=FIXED_CLOCK)
    brain.edit(session, A_BLOCK, "Zweite Fassung.", edited_by="raphael")

    entries = brain.history(session, A_BLOCK)
    assert [row.text for row in entries] == ["Zweite Fassung.", "Erste Fassung."]
    assert [row.edited_by for row in entries] == ["raphael", "lucas"]
    assert entries[-1].edited_at == FIXED_CLOCK


def test_a_history_is_per_block_and_not_the_whole_house(session):
    brain.edit(session, "refusal", "Nur hier.")
    brain.edit(session, "house_style", "Nur dort.")

    assert [row.key for row in brain.history(session, "refusal")] == ["refusal"]


def test_the_newest_row_wins_when_a_block_has_been_edited_twice(session):
    brain.edit(session, A_BLOCK, "Alt.")
    brain.edit(session, A_BLOCK, "Neu.")

    assert brain.stored(session)[A_BLOCK] == "Neu."


def test_a_version_from_a_restored_table_cannot_collide_with_one_that_has_a_text(
    session,
):
    """The counter and the rows are written in one commit, so they can only
    disagree if the table was restored from a dump without the settings row.
    Taking the maximum means the next change gets a fresh number rather than a
    second text for a version somebody has already been shown."""
    session.add(
        BrainOverride(
            key=A_BLOCK, text="Aus einem Dump.", edited_at=FIXED_CLOCK,
            edited_by="lucas", version=7,
        )
    )
    session.commit()

    assert brain.version(session) == 7
    assert brain.edit(session, A_BLOCK, "Danach.").version == 8


# --------------------------------------------------------------------------
# The panel: every block readable, its provenance stated, its history reachable.
# --------------------------------------------------------------------------


def _panel(body: str) -> str:
    """The brain section of the settings page, between its own markers."""
    return body.split("<!-- ── Brain")[1].split("<!-- ── /Brain")[0]


def test_the_panel_lists_every_block_with_its_current_text(client):
    body = _panel(client.get("/settings").text)

    for key, text in brain.shipped().items():
        assert key in body, key
        # The first line of each block, which is its heading in the file: enough
        # to prove the text is on the page rather than only its name.
        assert text.splitlines()[0] in body, key


def test_the_panel_says_a_block_is_the_shipped_default_when_nothing_was_changed(client):
    body = _panel(client.get("/settings").text)
    assert "Vorgabe" in body
    assert "Überschrieben" not in body
    assert "Noch nie geändert" in body


def test_the_panel_says_a_block_is_an_override_once_it_has_been_edited(
    factory, client
):
    with factory() as session:
        brain.edit(session, A_BLOCK, "Ein anderer Maßstab.", edited_by="lucas")

    body = _panel(client.get("/settings").text)
    assert "Überschrieben" in body
    assert "Ein anderer Maßstab." in body
    assert "lucas" in body


def test_editing_through_the_page_stores_the_override(factory, client):
    resp = client.post(
        f"/settings/brain/{A_BLOCK}",
        data={"text": "Schweigen ist meistens richtig."},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/settings/brain/{A_BLOCK}"
    with factory() as session:
        assert brain.stored(session) == {A_BLOCK: "Schweigen ist meistens richtig."}
        assert brain.version(session) == 1


def test_an_empty_edit_is_refused_on_the_page_with_the_reason(factory, client):
    resp = client.post(f"/settings/brain/{A_BLOCK}", data={"text": "   "})

    assert resp.status_code == 200
    assert "leer" in _panel(resp.text)
    with factory() as session:
        assert brain.stored(session) == {}


def test_reverting_through_the_page_restores_the_shipped_text(factory, client):
    with factory() as session:
        brain.edit(session, A_BLOCK, "Etwas anderes.")

    resp = client.post(f"/settings/brain/{A_BLOCK}/revert", follow_redirects=False)

    assert resp.status_code == 303
    with factory() as session:
        assert brain.stored(session) == {}
        assert brain.version(session) == 2


def test_the_block_page_shows_every_version_with_its_wording(factory, client):
    with factory() as session:
        brain.edit(session, A_BLOCK, "Erste Fassung.", edited_by="lucas")
        brain.edit(session, A_BLOCK, "Zweite Fassung.", edited_by="raphael")

    body = _panel(client.get(f"/settings/brain/{A_BLOCK}").text)

    assert "Fassung 1" in body and "Fassung 2" in body
    assert "Erste Fassung." in body and "Zweite Fassung." in body
    assert "lucas" in body and "raphael" in body


def test_a_reverted_version_shows_the_wording_it_restored(factory, client):
    with factory() as session:
        brain.edit(session, A_BLOCK, "Etwas anderes.")
        brain.revert(session, A_BLOCK)

    body = _panel(client.get(f"/settings/brain/{A_BLOCK}").text)

    assert "Auf die Vorgabe zurückgesetzt" in body
    # Both wordings are readable: the one that was overridden and the one that
    # came back.
    assert "Etwas anderes." in body
    assert brain.shipped()[A_BLOCK].splitlines()[0] in body


def test_an_override_whose_block_no_longer_ships_is_shown_as_orphaned(factory, client):
    """A renamed block must not leave a live override nobody can find."""
    with factory() as session:
        session.add(
            BrainOverride(
                key="alter_name", text="Gilt noch.", edited_at=FIXED_CLOCK,
                edited_by="lucas", version=1,
            )
        )
        session.commit()

    body = _panel(client.get("/settings").text)
    assert "alter_name" in body
    assert "Verwaist" in body
    assert "Gilt noch." in body


def test_an_orphaned_override_can_be_reverted_away(factory, client):
    with factory() as session:
        session.add(
            BrainOverride(
                key="alter_name", text="Gilt noch.", edited_at=FIXED_CLOCK,
                edited_by="lucas", version=1,
            )
        )
        session.commit()

    resp = client.post("/settings/brain/alter_name/revert", follow_redirects=False)

    assert resp.status_code == 303
    # Nowhere left to land: the block has no shipped default to show.
    assert resp.headers["location"] == "/settings#brain"
    with factory() as session:
        assert brain.stored(session) == {}


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_block_key_that_exists_nowhere_is_a_404(client, method: str):
    """The key comes from the URL. Without this a typo renders an editor for a
    block that does not exist, over a save button that would refuse it."""
    call = getattr(client, method)
    kwargs = {"data": {"text": "Ein Satz."}} if method == "post" else {}
    assert call("/settings/brain/gibt_es_nicht", **kwargs).status_code == 404


def test_the_panel_names_the_version_the_standards_are_at(factory, client):
    with factory() as session:
        brain.edit(session, A_BLOCK, "Ein anderer Maßstab.")

    assert "Fassung 1" in _panel(client.get("/settings").text)


def test_every_german_string_in_the_brain_panel_has_an_english_entry():
    """A half-translated panel reads as broken in a way a fully German one does
    not, and the strings that get forgotten are always the new ones."""
    markup = _panel(
        (Path(brain.__file__).parent / "web/templates/settings.html").read_text("utf-8")
    )
    called = set(re.findall(r"""t\(\s*['"](.+?)['"]\s*\)""", markup, re.DOTALL))
    missing = sorted(called - set(i18n.known_keys()))
    assert not missing, f"no English for: {missing}"


def test_the_german_the_panel_renders_as_a_value_is_translated_too():
    """The hole the test above cannot see.

    It scans for ``t('…')`` call sites, so it is blind to a German sentence that
    reaches the page as a variable — which is exactly how the refusal for an empty
    block arrives. It shipped as a bare ``{{ brain_error }}`` and rendered German
    inside an otherwise English panel.
    """
    assert brain.EMPTY_BLOCK_MESSAGE in i18n.known_keys()
    assert i18n.translate(brain.EMPTY_BLOCK_MESSAGE, "en") != brain.EMPTY_BLOCK_MESSAGE


def test_a_refused_edit_reads_in_english_on_an_english_page(client):
    """The whole point of the entry above, driven through the route."""
    client.cookies.set(i18n.COOKIE_NAME, "en")
    response = client.post(f"/settings/brain/{A_BLOCK}", data={"text": "   "})

    assert response.status_code == 200
    assert i18n.translate(brain.EMPTY_BLOCK_MESSAGE, "en") in _panel(response.text)
    assert brain.EMPTY_BLOCK_MESSAGE not in _panel(response.text)


def test_the_recorded_author_is_translated_rather_than_shown_as_a_german_noun():
    """`edited_by` is rendered through the same lookup as the chrome around it,
    so the fallback author does not sit in German in an English panel."""
    assert i18n.translate(brain.editor(), "en") == "human"
