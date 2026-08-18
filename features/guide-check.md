# The guide check: does this text obey the client's own rules?

prefix: GDC

**Type:** feature
**Complexity:** 2
**Estimated Duration:** ~2 days
**Risk:** low
**Scope:** newspulse, quality
**Test Strategy:** unit tests with an injected `generate`, driving the checker over a client that has a guide, one that has none, and one whose guide contains an explicit no-go the draft violates; a fixture asserting the guide text actually reaches the prompt, because the failure this feature guards against is silent omission; FastAPI `TestClient` coverage of the three rendered states on the letter card. No test reaches a model.

## Context

PR-07 is the engine that reads a text before a human does. It is built and it
works: `outreach.crosscheck()` sends every letter to a second provider and reports
invention, overclaiming, ingratiation, marketing language and machine tells. The
product definition names one thing it does not do: "Es fehlt die Prüfung gegen die
Kundenrichtlinien als eigener Schritt."

That gap is sharper than it sounds. `guide.for_prompt()` already feeds the
communications guide into the *writing* prompt, so the model that writes the letter
knows the client's register and its no-gos. The model that checks the letter does
not. The check is therefore blind to the single class of error that costs an agency
a mandate: a text that is factually clean, well written, and says the one thing this
client has said it never says.

A guide breach is also different in kind from the errors the crosscheck already
finds. Invention and overclaiming are judgements about the world, and the checker
weighs them. A no-go is not a judgement: the client wrote it down. So it is reported
separately, quoted against the line of the guide it breaks, and it is never averaged
into a general verdict.

The check stays advisory, grade A, exactly as PR-07 is specified. It objects; the
human releases.

## Summary

Today the second model reads every letter for invention, overclaiming and marketing
language, and has no idea what the client said it never says. The guide sits in the
writing prompt and not in the checking one, so the one mistake that ends a mandate
is the one mistake nothing looks for.

After this build, the check reads the letter against the client's own guide as a
separate pass with its own verdict. A breach is quoted twice: the sentence in the
draft, and the line of the guide it collides with, so the objection can be judged in
a second rather than taken on faith. A client with no guide gets told that the check
could not run, rather than a clean bill of health, because "no objections" and
"nothing to object with" must never look the same on screen.

## Decisions

### DEC-1: How does a guide breach relate to the existing crosscheck verdict?  [options]
**Status:** open
**Recommend:** B
**Locks as:** the shape of the check in GDC-01 and what the letter card shows
- **A · Ein Prüfergebnis** The guide becomes another part of the existing crosscheck prompt, and a breach arrives as one more line in the list of objections. Cheapest by far, one model call, nothing new on the card. It also lets a no-go be weighed against a tone remark by a model, and a client's written rule is not the same kind of thing as a stylistic preference.
- **B · Zwei Prüfungen, zwei Urteile** The guide check is its own call with its own prompt and its own verdict, rendered as its own block under the crosscheck. A breach names the draft sentence and the guide line together. Twice the model cost per letter and a second failure mode to handle, in exchange for a rule breach that can never be diluted into a style note, and for a check that can say "kein Guide hinterlegt" instead of nothing.
- **C · Erst mechanisch, dann das Modell** Explicit no-gos are extracted from the guide once and matched against the draft as strings first; the model is only asked about the rest. Catches a literal forbidden word with certainty and no model call, but a no-go is almost never a word, it is a claim, and a matcher that finds "billig" will miss "das günstigste Angebot am Markt".

## Stories

### GDC-01: Check the draft against the guide, as its own pass

The second model gets a second job. Given the letter and the client's guide, it
answers one question: does this text break a rule this client wrote down.

The prompt receives the guide verbatim, the draft's subject and body, and nothing
else. Not the article, not the profile, not the angle. The check is about a text
against a rule, and every extra fact is another thing the model can reason its way
around when it should be reading a rule literally.

A client with no stored guide is not checked. That is a distinct state and it is
returned as one, because the failure this feature exists to prevent is a text that
reads as approved when nothing looked at it.

**Changes:**
- New `prompts/guide_check.txt` in the same shape as `prompts/crosscheck.txt`: the guide, the draft, and an instruction to answer only with breaches and to quote both sides of each
- Add `check_guide()` to `src/newspulse/guide.py`, returning a typed verdict and the model name, with an injected `generate` like every other generator in this codebase
- Add `GuideVerdict` to `src/newspulse/schemas.py` alongside `MessageReview`: an ok flag, a list of breaches, each carrying the draft sentence and the guide line
- Call it from `_run_outreach` in `src/newspulse/web/routes/advisory.py`, after the crosscheck, so a failure in either leaves the other's result intact

**Acceptance:**
- The stored guide text reaches the prompt verbatim; a test asserts the guide's own words appear in the rendered prompt, since silent omission is the failure being guarded against.
- A draft that breaks a written no-go produces a breach naming both the draft sentence and the guide line it collides with.
- A draft that obeys the guide produces an ok verdict with an empty breach list, distinct from the no-guide case.
- A client with no stored guide returns the not-checked state without calling the model at all.
- A model call that fails or returns unparseable output leaves the letter stored and readable, records no verdict, and logs at ERROR; it never blocks the draft from appearing.
- The check reads only the guide and the draft: the prompt contains no article text, no client profile and no angle.

**Files:** `newspulse/src/newspulse/prompts/guide_check.txt` (new), `newspulse/src/newspulse/guide.py`, `newspulse/src/newspulse/schemas.py`, `newspulse/src/newspulse/web/routes/advisory.py`, `newspulse/tests/test_guide_check.py` (new)

**Smoke:** `pytest tests/test_guide_check.py tests/test_guide.py` passes

### GDC-02: Store the verdict and show it under the letter
**Depends on:** GDC-01

The verdict is stored beside the crosscheck's, on the same row, and rendered as its
own block with the same three-state discipline the crosscheck already follows: a
clean check, objections, and never checked at all, none of which may look like
either of the others.

**Changes:**
- Add `guide_review`, `guide_reviewed_by` and `guide_ok` to `Outreach` in `src/newspulse/models.py`, mirroring the existing `review` / `reviewed_by` / `review_ok` trio
- Alembic revision `0021_guide_check` following the outreach ledger revisions
- Persist the verdict in `outreach.store()` in `src/newspulse/outreach.py`, and clear it on redraft the way the crosscheck fields are cleared
- Render the guide block under the crosscheck in `advice.html`, with the breach pairs quoted
- German strings into `i18n._EN`

**Acceptance:**
- A letter with a clean guide check shows a block saying so, visually distinct from the crosscheck's block, so two green boxes are not read as one.
- A letter with breaches lists each one as a pair: the draft's sentence and the guide's line, in that order.
- A letter for a client with no guide shows the not-checked state with a link to that client's guide page, and it is styled as a warning rather than as an approval.
- Redrafting clears the stored guide verdict, so a new text never inherits the previous one's clean check.
- Existing letters written before this migration render the not-checked state and no error.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0021_guide_check.py` (new), `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/web/templates/advice.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_guide_check.py`

**Smoke:** `pytest tests/test_guide_check.py tests/test_outreach.py tests/test_migration.py` passes

## Deferred

- **The same check on every other format.** Once PR-05 produces press releases,
  statements and Q&As, each of them needs this pass too. The checker is written
  against a text and a guide rather than against a letter, so extending it is
  wiring rather than new work.
- **Learning from overridden objections.** A consultant who releases a letter that
  the guide check objected to is telling the system something. Recording that is
  the first real signal for L8, and it belongs with the ledger the outreach spec
  builds.
- **Per-client severity.** Some guide lines are absolute and some are preferences,
  and today the guide is one block of prose that cannot say which is which.
  Structuring it is a change to the guide, not to the check.
