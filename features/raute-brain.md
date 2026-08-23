# Raute Brain: one place that says what good looks like

prefix: BRN

**Type:** feature
**Complexity:** 4
**Estimated Duration:** ~1 week
**Risk:** medium
**Scope:** newspulse, brain
**Test Strategy:** a structural test over every prompt file asserting it composes the blocks it declares and restates none of them inline, which is the mechanism that keeps the layer load-bearing rather than decorative; unit tests over block resolution, the override chain and version stamping with an injected clock; golden-file tests that render each prompt with a fixed block set so a change to a standard shows up as a reviewable diff across every prompt it touches; `TestClient` coverage of editing a block, reverting it, and reading the version history. No test reaches a model.

## Context

L1 is the first layer in the architecture and the one every engine is supposed to
read from: "Definiert, wie RAUTE denkt: Qualitätsstandards, PR-Philosophie,
Positionierungslogik, journalistische Kriterien, Bewertungsmaßstäbe, Tonalität,
Arbeitsprinzipien. Jede Engine liest daraus, keine definiert eigene Maßstäbe."

Today the second half of that sentence is false, and measurably so. The standards
exist and they are good, and they are scattered across ten prompt files in
`src/newspulse/prompts/`. `outreach.txt` knows the house style. `angle.txt` knows
what makes a thesis worth sending and that silence is an acceptable answer.
`crosscheck.txt` knows what counts as overclaiming. `coach.txt` knows how a claim
is judged against coverage. Each of them learned it separately, each states it in
its own words, and no two agree exactly.

The cost is not visible until something changes. Decide that a thesis now needs two
independent pieces of evidence rather than one, and that judgement has to be found
and rewritten in the prompt that writes theses, the prompt that checks them, the
prompt that reviews the month, and the prompt that briefs the assistant. Miss one
and the tool holds two standards at once and reports both as correct. The one house
rule that is enforced properly, no dashes, is enforced properly precisely because
it lives in code as `prose.plain()` rather than as a sentence repeated in prompts.

This matters now rather than later because six engines are about to be built on top
of it. `asset-formats.md` adds six formats, each with a prompt. `guide-check.md`
adds a checker. Each will restate the house standards in its own words unless there
is somewhere to read them from, and the product definition already names the failure
mode: "eine Engine, die eine Schicht umgeht, ist kein Fortschritt, sondern eine
Ausnahme, die später jemand zurückbauen muss."

So this is deliberately a small feature with a large blast radius. It adds no
capability a user can point at. It makes one sentence in the architecture true.

## Summary

Today the agency's standards live in ten prompt files that each learned them
separately. What makes a thesis worth sending is stated four times in four
wordings, so changing it means finding four places and getting all four right.

After this build there is one set of named blocks, in the agency's own words, and
every prompt composes from them instead of restating them. Change what a thesis
must do in one place and every engine that writes, checks or reviews one changes
with it, visibly, as a diff a person can read before it takes effect. The blocks
are editable in the tool, because what good PR looks like is the agency's judgement
and not the developer's, and every edit is versioned with who made it and when.
Every text the system generates records which version of the standards it was
written under, so a text from March can be read against what the house believed in
March rather than against what it believes today.

## Decisions

### DEC-1: Where do the standards live?  [options]
**Status:** open
**Recommend:** C
**Locks as:** the storage and edit path in BRN-01 and BRN-02
- **A · Als Dateien im Repository** Blocks are files, edited by whoever edits code, versioned in git. Free history, free review, free rollback, and diffs that show exactly what changed. It also means the agency's editorial judgement can only be changed by a deployment, which puts a developer between a consultant and a sentence about tone that a consultant is better qualified to write.
- **B · In der Datenbank, im Werkzeug bearbeitbar** Blocks live in a table with a version history and are edited in settings. The layer belongs to the people who own the judgement, and a change takes effect immediately. It also means a fresh install has an empty brain, git holds no record of how the standards evolved, and a bad edit is live until someone notices.
- **C · Dateien als Vorgabe, Überschreibung im Werkzeug** The repository ships the blocks, so a fresh install thinks correctly on day one and git keeps the lineage. Any block can be overridden in the tool, the override is versioned and revertable to the shipped text, and the settings page always shows whether a block is the default or an override. Two sources to reason about, in exchange for both properties that matter.

### DEC-2: How strictly must a prompt use the blocks?  [options]
**Status:** open
**Recommend:** B
**Locks as:** the composition rule and the test that enforces it in BRN-01
- **A · Blöcke als Bausteine, Rest frei** A prompt includes the blocks it wants and may add whatever else it needs. Easiest migration, nothing breaks, every existing prompt keeps working. It is also exactly today's situation with a helper attached, and the standards will drift back out into the prompts within a quarter.
- **B · Blöcke sind die einzige Quelle für Maßstäbe** A prompt declares which blocks it composes, and a structural test fails if a prompt restates a standard inline instead of including it. The layer becomes load-bearing rather than advisory, and the test is what keeps it that way after everyone has forgotten this decision. It costs a real migration of ten prompt files and a rule about what counts as a standard versus a task instruction.
- **C · Blöcke als Kontext, ohne Zwang** Blocks are prepended to every prompt automatically and prompts are not touched at all. No migration, immediate consistency of a sort. It also makes every prompt longer and vaguer, and a prompt that contradicts a prepended block is a contradiction the model resolves however it likes.

### DEC-3: What is recorded about which standards a text was written under?  [options]
**Status:** open
**Recommend:** A
**Locks as:** the stamping in BRN-03
- **A · Eine Version je Text** Every generated angle, letter and asset stores the brain version in force when it was written. Cheap, one integer, and it is the first real entry L9 can point at: this text was written under these standards. It does not tell you what changed between two versions without going to the history.
- **B · Version plus die verwendeten Blöcke** Each text stores the version and which blocks composed its prompt. Answers "why does this text say that" precisely, and it is the shape L8 would want for learning which standards actually produce good output. More storage and a join to read.
- **C · Nichts aufzeichnen** The brain is current, texts are current, nothing is stamped. Simplest, and it means a letter from March cannot be judged against what the house believed in March, which is the question that comes up in exactly the conversation where it matters.

## Stories

### BRN-01: The blocks, and prompts that compose from them
**Decisions:** DEC-1, DEC-2

The layer itself. A set of named blocks holding what the house believes, and the
ten existing prompts rewritten to include them instead of restating them.

The blocks are the extraction of what is already in the prompts, not new doctrine.
The work is reading ten files, finding the four wordings of the same rule, deciding
which one is right, and writing it once. That judgement call is the feature.

Starting set, each named and each traceable to prompts it currently lives in:
house style and tonality, what a position must do and what a non-position is,
journalistic criteria for what is worth a journalist's time, evidence and how a
claim may be supported, refusal principles and when silence is the right answer,
and the rules on what may never be invented.

**Changes:**
- New `src/newspulse/brain/` holding the blocks as text files, one per block, each with a stable key
- New `src/newspulse/brain.py`: `block()`, `blocks()`, `compose()` and `version()`, resolving a block through whatever chain DEC-1 locks
- Rewrite the prompt files in `src/newspulse/prompts/` to declare and include blocks rather than restate standards
- A structural test that every prompt declares its blocks, that composition resolves, and, under DEC-2 option B, that no prompt restates a block's content inline
- Golden-file rendering tests so a change to a block shows as a reviewable diff across every prompt it touches

**Acceptance:**
- Every block has a stable key and resolves to text; a prompt referencing an unknown block fails loudly at render rather than silently composing without it.
- All ten existing prompts compose successfully and their rendered output still contains every standard they carried before, asserted against the golden files captured from the current prompts.
- Changing one block changes every prompt that includes it, and the golden-file diff shows exactly which prompts moved.
- The composition rule from DEC-2 is enforced by a test rather than by convention; under the recommended option, a prompt that restates a standard inline fails the suite.
- Block resolution is pure and takes its source explicitly, so a test can compose against a fixed block set without touching the filesystem or the database.
- `prose.plain()` stays in code and is not moved into a block, because a rule that must hold on output cannot be a sentence a model is asked to follow.
- No behavioural change is expected from this story: the analyzer, angle and outreach tests pass unchanged.

**Files:** `newspulse/src/newspulse/brain.py` (new), `newspulse/src/newspulse/brain/` (new block files), `newspulse/src/newspulse/prompts/` (ten files rewritten), `newspulse/tests/test_brain.py` (new), `newspulse/tests/fixtures/prompts/` (new golden files)

**Smoke:** `pytest tests/test_brain.py tests/test_angles.py tests/test_outreach.py tests/test_analyzer.py` passes

### BRN-02: Reading and editing the brain
**Depends on:** BRN-01
**Decisions:** DEC-1

The surface, in settings, beside the other things that configure how the tool
behaves. Each block readable in full, editable, with its history and a way back to
the shipped text.

Deliberately not a free-text box holding everything. Blocks are separate because
the point of the layer is that a change to tonality is a different act from a
change to what counts as evidence, and one textarea makes them the same act.

**Changes:**
- Add a `BrainOverride` model to `src/newspulse/models.py` (`key`, `text`, `edited_at`, `edited_by`, `version`) and a portfolio-wide `brain_version` counter
- Alembic revision `0026_brain_overrides`
- A brain panel in `settings.html`: each block with its text, whether it is the shipped default or an override, when it was last changed, and a revert
- Routes for edit, revert and history in `src/newspulse/web/routes/settings.py`
- German strings into `i18n._EN`

**Acceptance:**
- Every block is listed with its current text and says plainly whether it is the shipped default or an override.
- Editing a block stores an override, bumps the brain version once for the change, and takes effect on the next generated text without a restart.
- Reverting restores the shipped text exactly and bumps the version again, so a revert is itself a recorded change rather than a disappearance.
- The history for a block shows every version with when it changed and who changed it, and previous texts are readable rather than only their dates.
- An empty or whitespace-only block is refused, because a prompt composing an empty standard silently drops it.
- A block that exists as an override but no longer ships as a default is shown as orphaned rather than hidden, so a renamed block cannot leave a live override nobody can find.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0026_brain_overrides.py` (new), `newspulse/src/newspulse/brain.py`, `newspulse/src/newspulse/web/routes/settings.py`, `newspulse/src/newspulse/web/templates/settings.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_brain.py`

**Smoke:** `pytest tests/test_brain.py tests/test_settings.py tests/test_migration.py` passes

### BRN-03: Every text says which standards it was written under
**Depends on:** BRN-02
**Decisions:** DEC-3

The stamp. What the ledger does for the human act, this does for the machine act:
a text records the standards in force when it was made.

This is the first thing in the tool that L8 could learn from and the first thing
L9 can point at for a generated artefact. It is one column and a render line, and
it is only worth anything if it is on every generator rather than on the convenient
ones, so it goes on all of them at once.

**Changes:**
- Add `brain_version` to `Angle`, `Outreach` and, once `asset-formats.md` lands, `Asset` in `src/newspulse/models.py`
- Alembic revision `0027_brain_version`
- Stamp it at store time in `src/newspulse/angles.py`, `src/newspulse/outreach.py` and the asset writer
- Show it on the generated text as a quiet line, linking to that version in the brain history
- German strings into `i18n._EN`

**Acceptance:**
- Every newly stored angle, letter and asset carries the brain version that was in force when it was generated, read at generation rather than at save.
- Rows created before this migration carry no version and render as "unbekannt" rather than as version zero, which would be a claim about standards that were never recorded.
- The version shown on a text links to that version in the block history, so the standards it was written under are readable and not merely numbered.
- A brain edit between generation and storage does not change the stamp: the version is captured with the prompt.
- The stamp is on every generator; a test enumerates the generators and fails if one stores without a version.

**Files:** `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0027_brain_version.py` (new), `newspulse/src/newspulse/angles.py`, `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/web/templates/advice.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_brain.py`

**Smoke:** `pytest tests/test_brain.py tests/test_angles.py tests/test_outreach.py tests/test_migration.py` passes

## Deferred

- **Per-mandate deviation from a house standard.** A client whose register genuinely
  differs from the house tone is handled by the guide today, which is the right
  place. If a mandate ever needs to override a brain block rather than add to it,
  that is a real conflict and needs a rule, not a text field.
- **Learning which standards work (L8).** Once texts carry a version and the ledger
  carries outcomes, the answer rate of letters written under version 8 versus
  version 12 is a query. That is the first honest evaluation signal this system can
  produce, and it needs both halves to exist first.
- **The brain as the assistant's own ground.** Captain Comms currently reasons from
  its own prompt. Composing it from the same blocks would make the advice and the
  drafts argue from one position, which is the whole point of the layer.
- **Diffing two versions side by side.** The history stores texts; reading what
  changed between version 6 and version 9 is currently a manual comparison.
