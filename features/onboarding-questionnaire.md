# The twenty questions: what no research can find out

prefix: ONB

**Type:** feature
**Complexity:** 4
**Estimated Duration:** ~1 week
**Risk:** medium
**Scope:** newspulse, onboarding
**Test Strategy:** unit tests over the answer store, the completeness rule and the guide draft with an injected `generate`, using the existing in-memory SQLite `factory` fixture; a test that an answer is never written into `client_facts` or `comms_guide` without an explicit accept, since silent adoption is the failure being guarded against; `TestClient` coverage of the questionnaire page including partial completion, skip and resume; the two dozen question texts are data and get a fixture test that every one of them declares which target it feeds. No test reaches a model.

## Context

CS-01 turns a won client into a working account, and most of it runs today. Creating
a mandate already triggers industry classification, an archive backfill, theme
assignment and a first draft, all automatically. The product definition names what
is not there: "Es fehlt der Fragebogen: die zwanzig Fragen an den Kunden, aus denen
Guide, No-Gos und Sprecher entstehen."

That gap is upstream of nearly everything else in the tool. `profile.research()`
reads the open web and fills fourteen fields well. `guide.distill()` reads an
uploaded brand book and proposes a guide. Both are good, and both are limited in
the same way: they can only find what the client has already published. A company's
website never says which sentence would end the relationship if it appeared in
print, which competitor claim is a lie the client can disprove, or which topic the
legal department has ruled out until the case closes. Those answers exist in one
place, the kick-off conversation, and today they evaporate when the call ends.

Everything downstream inherits the gap. The guide check specified in `guide-check.md`
has nothing to check against for a client whose guide was never written. The angle
prompt guesses a register. The comparison set for share of voice is inferred from an
industry rather than named by the client. And PR-05's future formats will each
guess separately.

So the questionnaire is not a form. It is the input layer for the whole PR system,
and its output is three concrete things: facts on the profile, no-gos in the guide,
and named spokespeople. Each answer declares which of those it feeds, and no answer
is ever adopted without a human saying yes, because an onboarding that silently
writes a client's own words into a guide is how a wrong sentence becomes policy.

## Summary

Today a new mandate is set up from what the web says about it. The things that only
the client knows, which sentence must never be printed, who may be quoted on what,
which competitor claim is provably false, live in the notes of a kick-off call and
nowhere in the tool. Every text the system writes afterwards guesses at them.

After this build, a mandate carries a questionnaire that can be worked through in
one sitting or over a week, saving as it goes and leaving unanswered questions
visibly open rather than filled in with a guess. Every question says what it feeds:
this one becomes a profile field, this one becomes a no-go that every future text is
checked against, this one names the comparison set. When enough of it is answered,
the answers are turned into a draft guide the consultant edits and releases. Nothing
is adopted automatically, and the mandate page shows how much of its own foundation
is still missing, so a thinly set-up client is visible instead of merely quiet.

## Decisions

### DEC-1: Who answers the twenty questions, and where?  [mock]
**Status:** open
**Recommend:** A
**Locks as:** the built surface, matched against the chosen mock
- **A · Im Werkzeug, vom Berater** A sectioned form on the mandate, filled by the consultant during or right after the kick-off call. The answers already exist in that conversation and the consultant is the one who heard them, so this is the shortest path from a real answer to a stored one. It also needs no public URL and no new authentication surface. The cost is that it is the consultant's transcription rather than the client's own words, and a question he cannot answer stays open until he asks.
  `features/mocks/onboarding-form.html`
- **B · Ein Link an den Mandanten** The client gets a personal link and answers in their own words, at their own pace. Closest to what the product definition describes, "die zwanzig Fragen an den Kunden", and it produces better raw material because a no-go written by the client is the client's own sentence. It also means RauteOS gains its first surface reachable without the shared login, which is a real security step: an unguessable token, an expiry, and a page that must never leak anything about the mandate beyond the questions.
  `features/mocks/onboarding-client-link.html`
- **C · Als Gespräch** Captain Comms asks the questions one at a time, follows up where an answer is thin, and fills the profile visibly beside the conversation. Far less daunting than twenty text areas, it can probe ("den Doktortitel habe ich von der Website, stimmt der?"), and it reuses the assistant that already exists and already streams. It is also the hardest to resume cleanly, the hardest to see at a glance what is still missing, and a model deciding when an answer is good enough is a new kind of judgement in a tool that has been careful about where those live.
  `features/mocks/onboarding-conversation.html`

### DEC-2: What happens to an answer that contradicts what was researched?  [options]
**Status:** open
**Recommend:** A
**Locks as:** the adoption rule in ONB-02
- **A · Die Antwort gewinnt, sichtbar** An answer from the client or the consultant replaces the researched value on accept, and the old value stays visible as what the web said. The person who knows the company outranks the page about it, and the disagreement stays legible instead of being erased. Needs the profile to hold a superseded value, which it does not today.
- **B · Als Vorschlag in dieselbe Prüfliste** The answer arrives as an ordinary proposal beside the ones the refresh produces, and is accepted the same way. One code path, one mental model, nothing new in the schema. It also treats an answer from the CEO as one more suggestion to be triaged, which is not what it is.
- **C · Beides behalten, nichts entscheiden** Profile fields grow a second slot: what the client says and what the web says, side by side, permanently. Nothing is ever lost and every generated text can be told which to prefer. It also doubles the shape of every field and pushes the decision onto whoever reads the profile next, which is the decision the profile exists to have already made.

## Stories

### ONB-01: The questionnaire, answerable in pieces
**Decisions:** DEC-1

The questions themselves and the place they get answered. Twenty of them across
five sections, written so that a PR consultant would actually ask them out loud,
and each one declaring which downstream target it feeds.

The questions are data, not template markup, for the same reason `profile.FIELDS`
is data: they will be edited by someone reading the answers they produce, and a
question that has to be found in a Jinja file will not be edited.

Answers save as they are given. A questionnaire that must be completed in one pass
would be abandoned in the first meeting that overran, and an unanswered question is
a useful state: it says the foundation is thin here.

**Changes:**
- New `src/newspulse/onboarding.py`: the question set as data (key, section, text, help, input kind, and the target it feeds), plus `answers()`, `save_answer()`, `skip()` and `completeness()`
- New `OnboardingAnswer` model in `src/newspulse/models.py` (`client_id`, `key`, `value`, `answered_at`, `answered_by`, `skipped`), unique on `(client_id, key)`
- Alembic revision `0024_onboarding_answers`
- New route module `src/newspulse/web/routes/onboarding.py` with the page and the per-answer save, registered in `create_app()` the way the other routers are
- New `onboarding.html` matching the locked mock, and a Kickoff entry in `_client_tabs.html`
- German strings into `i18n._EN`

**Acceptance:**
- All twenty questions render in their sections, each showing what it feeds in words, and each is answerable independently.
- An answer is stored as it is entered and survives a reload; leaving the page mid-question loses at most that one unsubmitted field.
- A skipped question is stored as skipped, which is distinct from unanswered, and both are distinct from answered.
- The progress figure counts answered plus skipped against the total, and the page states how many remain rather than only showing a bar.
- Re-answering a question overwrites that answer and updates its timestamp, and does not create a second row.
- Every question declares a target, and a fixture test fails if a question is added without one.
- Nothing in this story writes to `client_facts`, `comms_guide` or `Client`; a test asserts those are byte-identical before and after a full questionnaire.
- The page matches the locked mock for DEC-1.

**Files:** `newspulse/src/newspulse/onboarding.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0024_onboarding_answers.py` (new), `newspulse/src/newspulse/web/routes/onboarding.py` (new), `newspulse/src/newspulse/web/templates/onboarding.html` (new), `newspulse/src/newspulse/web/app.py`, `newspulse/src/newspulse/web/templates/_client_tabs.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_onboarding_questions.py` (new)

**Smoke:** `pytest tests/test_onboarding_questions.py tests/test_onboarding.py tests/test_migration.py` passes

### ONB-02: Turn the answers into a profile, no-gos and a guide
**Depends on:** ONB-01
**Decisions:** DEC-2

The conversion. Answers that map to a profile field become proposals on that field;
answers about what must never be said become a draft guide; the named competitors
become the comparison set. Each is offered, none is applied.

The guide draft reuses `guide.distill()`, which already exists for uploaded brand
books and already ends in a proposal the consultant edits. Feeding it the answers
instead of a PDF keeps one path to a guide rather than two, and keeps the rule that
uploaded or dictated material is never applied directly.

**Changes:**
- Add `to_proposals()` and `to_guide_draft()` to `src/newspulse/onboarding.py`, the second calling `guide.distill()` with the answers as its source
- Record the answers as a `GuideSource` so the guide's provenance says it came from the kick-off, alongside uploaded documents
- Apply the DEC-2 rule where an answer contradicts a stored `ClientFact`, including whatever schema that rule needs
- Link the named competitors into the comparison set through the existing rivals path in `src/newspulse/rivals.py`, proposing rather than linking
- Show a completeness line on `client_profile.html` and on the client list: how much of the foundation this mandate actually has
- German strings into `i18n._EN`

**Acceptance:**
- Answers mapped to profile fields appear as proposals with the questionnaire named as their source, not as a URL.
- Generating the guide produces a draft the consultant can edit before it is saved, and the guide is not written until he saves it.
- The generated guide contains every no-go answer, verbatim rather than paraphrased, because a rule that has been reworded is a different rule.
- Named competitors are offered for the comparison set and are never linked automatically.
- A contradiction with an existing fact behaves exactly as DEC-2 locks it, and the profile shows both values with their provenance while the disagreement stands.
- The guide can be generated from a partly answered questionnaire, and the draft says which sections had no answers rather than inventing content for them.
- The completeness line appears on the profile and the client list, and a mandate with no questionnaire at all says so plainly.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/onboarding.py`, `newspulse/src/newspulse/guide.py`, `newspulse/src/newspulse/rivals.py`, `newspulse/src/newspulse/profile.py`, `newspulse/src/newspulse/web/templates/client_profile.html`, `newspulse/src/newspulse/web/templates/clients.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_onboarding_questions.py`

**Smoke:** `pytest tests/test_onboarding_questions.py tests/test_guide.py tests/test_profile.py` passes

## Deferred

- **The client answers directly (DEC-1 option B).** The client's own words are
  better raw material than the consultant's transcription, and it needs RauteOS's
  first surface reachable without the shared login. That is a security decision
  with an expiry, a token and a page that must leak nothing, and it belongs with
  CS-03's client portal rather than bolted on here.
- **Asking again.** A no-go from two years ago may not be one any more, and a
  spokesperson may have left. Re-asking a subset on a rhythm is the questionnaire's
  version of what `profile-refresh.md` does for researched facts.
- **Reading the kick-off recording.** Most agencies record or transcribe the call.
  Filling the questionnaire from a transcript would remove the transcription step
  entirely, and it needs a stance on storing client call recordings first.
- **Questions per industry.** A pharma mandate and a construction mandate do not
  need the same twenty questions. A per-industry overlay on the base set is the
  obvious refinement once the base set has been used on a dozen mandates.
