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
from string import Template

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
