# Six more formats: everything an agency actually delivers

prefix: FMT

**Type:** feature
**Complexity:** 5
**Estimated Duration:** ~2 weeks
**Risk:** medium
**Scope:** newspulse, assets
**Test Strategy:** unit tests per format with an injected `invoke`, asserting the structural contract each one owes (a press release has a headline, a dateline, a quote attributed to a named spokesperson and a boilerplate; a Q&A has questions and answers; talking points are bounded in number), driven from stored fixture model output rather than live calls; a shared test that every format is refused rather than invented when its required inputs are missing, since a fabricated quote is the worst failure this feature can produce; `TestClient` coverage of generating, editing, regenerating and releasing one asset; the existing crosscheck and guide-check paths are exercised against each format so no format can ship unchecked.

## Context

PR-05 is the engine that produces what an agency sells: pitches, press releases,
statements, Q&As, guest articles, interview briefings, talking points, social
content. The product definition is blunt about how far it got. "Ein Format ist
gebaut: das personalisierte Anschreiben an eine namentliche Journalistin. Es fehlen
Pressemitteilung, Statement, Q&A, Briefing, Talking Points und Social."

The one built format is built well, and it is built as one thing rather than as an
instance of a kind. `outreach.py` holds the prompt assembly, the model call, the
crosscheck, the storage and the recipient logic in one module, and the `Outreach`
table has a `journalist` and an `outlet` column because a letter has a recipient.
A press release does not. A statement has a speaker instead. Talking points have a
context and a duration. Copying `outreach.py` six times would produce six modules
that drift apart within a month and six places to fix the day the house style
changes.

So the first thing this feature builds is not a format. It is the shape a format
has: what it needs before it may be written, what it must contain, who it is
attributed to, and how it is checked. Each of the six then becomes a definition
against that shape rather than a new pipeline, and the seventh, whenever someone
wants it, is a definition too.

Two rules carry over from the letter and are not negotiable, because both were
learned the expensive way. Nothing is written from article bodies, only from
headlines, feed summaries and what the profile holds, so the Leistungsschutzrecht
constraint holds for every new format automatically. And every format goes through
a second model before a human sees it, plus the guide check from `guide-check.md`,
because a press release with a fabricated quote from a named CEO is a categorically
worse artefact than a pitch with a weak opening line.

The grade stays F, without exception. Six formats means six times as many drafts,
which is exactly why the outreach ledger's release step matters more, not less.

## Summary

Today RauteOS writes one kind of text: a letter to a named journalist. Everything
else an agency delivers, the release, the statement, the Q&A, the briefing, the
talking points, the guest article, is still typed from scratch in Word while the
tool sits on the thesis, the evidence and the guide that the text should be built
from.

After this build, an impulse can become any of seven formats. Each one knows its
own shape: a release carries a headline, a dateline, an attributed quote and a
boilerplate; a statement is three to five quotable sentences from a named speaker;
a Q&A includes the questions nobody wants asked; talking points carry bridges back
to the thesis. Each declares what it needs before it will write, and refuses rather
than inventing when a required piece is missing, so a quote is never attributed to
a spokesperson the profile does not name. Every format is read by a second model
and checked against the client's guide before a human sees it, and nothing leaves
the tool without a release.

## Decisions

### DEC-1: Where do the generated texts live and get worked on?  [mock]
**Status:** open
**Recommend:** C
**Locks as:** the built surface, matched against the chosen mock
- **A · Format am Impuls wählen** The impulse card grows a format chooser, and each generated text appears beneath it as a card in the same family as the letter card. Smallest change to a page people already use, and the text stays next to the position it argues. It also means a mandate with four impulses and twenty texts becomes a very long page, and there is no way to see everything written for a client.
  `features/mocks/assets-format-picker.html`
- **B · Ein eigener Arbeitsplatz für Texte** A new section in the sidebar: every text for a mandate in a list on the left, the document in the middle, the checks and the evidence on the right, editable in place. The right shape for the actual work, since a press release gets edited for twenty minutes rather than read once and copied. It is also the largest build here, a third surface pattern in the app, and it separates the text from the impulse it came from.
  `features/mocks/assets-workspace.html`
- **C · Ein Anlass, ein Paket** The impulse keeps everything, and the formats become tabs on it: which are written, which are approved, which do not exist yet, in one strip. It matches how a communication occasion is actually handled, as a package rather than as separate documents, and "Fehlende schreiben" is one button. Each individual text gets less room than in a workspace, and heavy editing still happens elsewhere.
  `features/mocks/assets-one-brief.html`

### DEC-2: What happens when a format's required input is missing?  [options]
**Status:** open
**Recommend:** A
**Locks as:** the refusal rule in FMT-01
- **A · Verweigern und den Grund nennen** A format declares its requirements; if the profile has no named spokesperson, no statement is written and the button says why with a link to the field. Silence is already an accepted answer in this codebase, `angles.suggest()` returns nothing and records its refusal, so this extends an established posture. It also means a thinly filled profile blocks formats, which is uncomfortable and correct.
- **B · Mit Platzhaltern schreiben** The text is generated with `[Name, Funktion]` where the fact is missing. The consultant gets something to work with immediately, and a placeholder that survives one careless copy becomes a press release with a bracket in it, which has happened at every agency that has tried this.
- **C · Nachfragen statt schreiben** A missing requirement opens the question instead of the draft: "Wer soll zitiert werden?", answered inline, stored to the profile, then the text is written. Best outcome per click and the most work to build, and it needs an answer for the case where the consultant does not know either.

## Stories

### FMT-01: What a format is
**Decisions:** DEC-2

The shape, and the storage. One definition per format holding its key, its German
name, what it requires, what its output must structurally contain, and its prompt.
One writer that takes a definition, an angle and a client and produces a stored
asset. One table for all of them.

Deliberately a new `Asset` table rather than more columns on `Outreach`. A letter's
`journalist` and `outlet` are its recipient, and five of the six new formats have no
recipient at all; widening that table would leave most rows with most columns empty
and every query guessing which shape it was looking at. The existing letter stays
where it is and keeps its ledger, and the two are read together where both belong.

**Changes:**
- New `src/newspulse/assets.py`: a `FormatDef` dataclass, the registry, `requirements_met()`, `write()` with an injected `invoke`, and `store()` applying `prose.plain()` the way `outreach.store()` does
- New `Asset` model in `src/newspulse/models.py`: `client_id`, `angle_id`, `kind`, `title`, `body`, `speaker`, `generated_at`, `edited_at`, plus the review, guide-review and release fields mirroring `Outreach`
- Alembic revision `0025_assets`
- New per-format prompts under `src/newspulse/prompts/`, one file per format, each stating its structure and its refusal condition
- Route the crosscheck from `src/newspulse/outreach.py` and the guide check from `guide.py` through a shared entry point so every format is checked by the same code

**Acceptance:**
- A format is defined by data, and adding a seventh needs a definition plus a prompt file and no change to the writer.
- Each format declares its requirements, and `requirements_met()` reports exactly which are missing for a given client.
- A format whose requirements are not met behaves as DEC-2 locks it; under the recommended rule nothing is written and the reason names the missing field.
- A generated asset is stored with the angle it came from, so every text can be traced to the position it argues and the evidence under it.
- Every format passes through the crosscheck and the guide check before it is readable, and an asset with neither recorded renders as unchecked rather than as clean.
- `prose.plain()` is applied to every stored title and body, so the dash rule holds for six new formats without six new call sites.
- Nothing in the prompt for any format contains article body text; a test asserts the rendered prompt draws only on headlines, feed summaries and profile facts.

**Files:** `newspulse/src/newspulse/assets.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0025_assets.py` (new), `newspulse/src/newspulse/prompts/` (new per-format prompts), `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/guide.py`, `newspulse/tests/test_assets.py` (new)

**Smoke:** `pytest tests/test_assets.py tests/test_outreach.py tests/test_migration.py` passes

### FMT-02: The six formats
**Depends on:** FMT-01

The definitions themselves, and the structural contract each one owes. This is
where the PR craft lives, and each format is only as good as the shape it is held
to, so each one's contract is asserted rather than hoped for.

- **Pressemitteilung** needs a named spokesperson and at least one fact from the
  profile. Headline, dateline, a lead that answers what happened in one sentence,
  body, one attributed quote, boilerplate.
- **Statement** needs a named spokesperson. Three to five sentences, quotable as
  printed, attribution underneath, no preamble.
- **Q&A** needs the client's no-gos, because its value is the questions nobody
  wants asked, and a Q&A that avoids them is decoration. Questions grouped, the
  uncomfortable ones marked.
- **Talking Points** needs the thesis and the not-thesis. Bounded in number, each
  point with a bridge back to the thesis, plus what not to say.
- **Gastbeitrag** needs the thesis and at least two pieces of evidence. Argued,
  around 4000 characters, first person, no news lead.
- **Interview-Briefing** needs the outlet and, where known, the journalist's recent
  headlines. Who is asking, what they wrote lately, what they will probably ask,
  what the client wants to have said regardless.

**Changes:**
- Six `FormatDef` entries in `src/newspulse/assets.py` with their requirements and structural contracts
- Six prompt files under `src/newspulse/prompts/`, each written against the house style rules already stated in `prompts/outreach.txt`
- Structural validators per format in `src/newspulse/assets.py`, run on the model's output before it is stored
- German format names and descriptions into `i18n._EN`

**Acceptance:**
- Each of the six produces its declared structure, and the validator rejects output that does not carry it rather than storing a malformed asset.
- A press release quote is attributed only to a spokesperson named in the profile, and a test with an empty spokesperson field asserts no release is produced at all.
- The Q&A includes at least one question drawn from the client's no-gos, and a client with no guide does not get a Q&A.
- Talking points stay within their declared bound and each carries a bridge; the not-thesis appears as an explicit "nicht sagen" section.
- The guest article carries no news lead and no dateline, so it cannot be mistaken for a release.
- The interview briefing names the outlet and, where bylines exist, the journalist's recent headlines, drawing on the same evidence `pitch.py` already assembles.
- Model output that cannot be parsed into the format's structure is retried once and then refused with a stored reason, following the existing analyzer retry precedent.

**Files:** `newspulse/src/newspulse/assets.py`, `newspulse/src/newspulse/prompts/` (six files), `newspulse/src/newspulse/pitch.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_assets.py`

**Smoke:** `pytest tests/test_assets.py tests/test_guide_check.py` passes

### FMT-03: Writing, editing and releasing a text
**Depends on:** FMT-02
**Decisions:** DEC-1

The surface. Choosing a format, generating, editing what came back, regenerating,
and releasing. Written against the locked mock.

Editing matters more here than it did for the letter. A pitch is read once and
copied; a press release is worked on. So an edited asset stores the edit, marks
itself as edited by a human, and is checked again before it can be released,
because a text a human changed after the check was cleared is a text nothing has
read.

**Changes:**
- New routes for generate, edit, regenerate and release, in `src/newspulse/web/routes/advisory.py` or a new module as the locked mock requires
- The surface itself, per the locked mock, with the per-format state visible: not written, draft, checked, released
- Reuse the release semantics from the outreach ledger so an asset and a letter mean the same thing by "freigegeben"
- Generation runs in a background thread with the existing lock and polling pattern from `_run_outreach`, since six formats in sequence is a long wait
- German strings into `i18n._EN`

**Acceptance:**
- Each format can be generated individually, and the surface shows for every format whether it exists, is a draft, is checked, or is released.
- A generated text can be edited in place, the edit is stored, and the asset is marked as edited by a human with a timestamp.
- An edited asset is re-checked before it can be released, and the release control is unavailable until that check has run.
- Regenerating replaces a draft and refuses to replace a released asset, matching the letter's immutability rule.
- Generation runs in the background with the existing lock, the page reports progress, and a second request while one is running is refused rather than queued.
- A generation that fails leaves any previously stored asset for that format intact and shows the reason.
- The surface matches the locked mock for DEC-1.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/web/routes/advisory.py`, `newspulse/src/newspulse/web/templates/advice.html`, `newspulse/src/newspulse/assets.py`, `newspulse/src/newspulse/web/app.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_assets_view.py` (new)

**Smoke:** `pytest tests/test_assets_view.py tests/test_assets.py` passes

## Deferred

- **Social.** Named in the product definition and left out here on purpose: it is
  the one format whose shape depends on a channel the profile does not record, and
  its house style is a different question from the other six. It becomes a
  definition like the rest once a channel field exists.
- **Formats from a crisis rather than an impulse.** PR-06 needs statements and
  Q&As under time pressure and from a different trigger. The format machinery is
  built to be called from anywhere, so that is wiring, but the trigger and the
  grade are PR-06's decision.
- **Client-specific templates.** A boilerplate is a fixed text per mandate and is
  currently regenerated every time. Storing it as a fact and pasting it in is
  cheaper, more correct, and belongs with the profile.
- **Export as a document.** The consultant copies text out today. A DOCX or PDF
  with the client's letterhead is what an agency actually sends, and it is the same
  question the client report asks, so both should answer it once.
