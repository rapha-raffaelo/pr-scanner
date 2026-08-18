# Outreach ledger: who we wrote to, and what came back

prefix: OUT

**Type:** feature
**Complexity:** 5
**Estimated Duration:** ~2 weeks
**Risk:** high
**Scope:** newspulse, outreach, gmail
**Test Strategy:** unit tests over the established in-memory SQLite `factory` fixture for the state transitions and the no-overwrite-after-release rule; the Gmail client is injected the way `invoke` / `generate` / `fetch` already are everywhere else in this codebase, so no test reaches Google and the reply matcher runs against captured Gmail API payloads under `tests/fixtures/`; FastAPI `TestClient` tests for the release, outcome, draft and OAuth-callback routes and for the contact history page; the OAuth flow is exercised with a stub token endpoint, including the refused-consent and revoked-token paths; the existing `tests/test_migration.py` alembic-upgrade-head run picks up revisions 0018 to 0020. The real test is a week of live use on one mandate before the second mailbox is connected.

## Context

RauteOS today can find the story, take a position on it, pick the journalists it
belongs to, write each of them a personal letter, and have a second model read
that letter before a human sees it. Then the loop stops. The only control on a
finished letter is a Kopieren button: the consultant pastes it into his own mail
client, sends it, and RauteOS never learns that it happened.

Everything downstream is missing because of that one gap. PR-04 cannot show a
relationship history, because nothing records that a relationship event occurred.
L5 Human Control describes grade F as "ein Mensch liest, ändert, gibt frei", and
today that release leaves no trace, so the load-bearing claim of the product is
undocumented in exactly the place it is exercised most. L9 has no ledger to write
to. PR-08 can count coverage but cannot connect a piece of coverage to the pitch
that produced it, so nobody can say what the outreach was worth.

This feature closes the loop. The human act of releasing a letter becomes a
record, the journalist's reply is read out of the mailbox and recorded against the
letter it answers, and a journalist's whole history becomes readable in the
contact book. Four constraints shape how, and none of them is a preference:

- **The Grundannahme.** "Keine Engine verschickt etwas an einen Journalisten oder
  einen Mandanten." A mailbox connection is the first thing built here that could
  break that sentence, so what the connection is allowed to do is a decision taken
  in the open (DEC-4) rather than a capability that arrives with the library.
- **Google's verification regime.** Gmail read scopes are restricted scopes. An
  app that touches them for users outside its own organisation needs Google
  verification plus a recurring third-party security assessment, and an unverified
  external app in Testing mode hands out refresh tokens that expire after about a
  week, which is fatal for a daily sync. Published as an **Internal** app inside
  RAUTE's own Google Workspace, none of that applies. This spec assumes a
  Workspace account; on a personal `@gmail.com` there is no Internal option and
  DEC-5 has to go a different way.
- **Somebody else's data.** A journalist's reply is personal data belonging to a
  person who never agreed to be in RauteOS. That is why the default read is the
  narrowest one that answers the question (DEC-6) and why every stored reply says
  where it came from.
- **Credentials from the environment.** `config.py` sources every secret from
  `NEWSPULSE_*` env vars and never from the database. A refresh token cannot
  follow that rule, because it is obtained at runtime. It goes to a file on the
  Railway volume beside the database, mode 0600, and never into a table, so that
  neither a database copy nor the Excel export can carry it out of the machine.

It also removes a quiet data-loss bug on the way. `outreach.store()` currently
upserts on `(angle_id, journalist, outlet)`, so redrafting for the same recipient
silently overwrites the previous text. Once a letter has been released that
overwrite would destroy the record of what was actually sent, so a released letter
becomes immutable and a redraft becomes a new row.

## Summary

Today a finished letter ends at Kopieren. Whether it went out, whether the
journalist answered, whether anything was published because of it: none of that is
anywhere in RauteOS, so the consultant carries it in his head and the coverage
numbers cannot be traced back to the pitch that caused them.

After this build, releasing a letter is one click that records who released it and
when, and the letter is then locked so its text stays exactly as it was sent. The
letter goes out through the consultant's own Gmail account, which is also what
makes the rest work: because RauteOS put the message into that thread, the reply
that arrives days later belongs to it beyond doubt, instead of being guessed at
from a subject line. A daily read of the mailbox picks up those replies, files
each one against the letter it answers, and marks the letter as answered or
declined. Nothing else in the mailbox is read.

The contact book becomes the place this is visible. Pick a journalist and read
everything ever sent to them across all mandates, what came back in their own
words, and what was published, each line saying whether it came from the mailbox
or was typed by hand. The pitch list stops suggesting someone who was written to
about the same thing last week.

## Decisions

### DEC-1: Which RauteOS building block should we build first?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** the scope of this spec
- **A · Kontakthistorie und Freigabe (PR-04, L5, L9)** The loop from impulse to journalist ends at Kopieren today, so nothing knows a letter went out. This is the missing spine: it makes the human release a record, gives PR-04 the relationship history it lacks, and writes the first real entries for the audit ledger. Everything else in the roadmap that needs a result reads from here.
- **B · Ein zweites Textformat (PR-05)** Pressemitteilung, Statement, Q&A or Talking Points beside the one format that exists. High visible value, but it makes more unreleased drafts, and the drafts already outrun what anyone can track.
- **C · Der Onboarding-Fragebogen (CS-01)** The twenty questions to a new client that produce the Guide, the no-gos and the spokespeople. Fixes the weakest input into every generated text, but only pays off on the next new mandate.
- **D · Der Mandantenreport (CS-06)** Turn the Excel export into a real report with figures, evidence and context. The most client-facing option, but the strongest figure a report can carry is "this pitch produced this coverage", and that number does not exist until A is built.

### DEC-2: Where does the outreach history live?  [mock]
**Status:** locked
**Chosen:** C
**Recommend:** B
**Locks as:** the built surface, matched against the chosen mock
- **A · Nur am Brief** The state, the release button and the outcome form sit directly on the letter card on the Impulse page, where the letter already is. Cheapest and closest to the moment of acting, but there is no place that answers "what is open with whom" without walking every mandate.
  `features/mocks/outreach-status-on-letter.html`
- **B · Ausgang: eine Seite für alles Rausgegangene** A new entry in the sidebar listing every released letter across all mandates, with filters by state, the count of what has been silent too long, and a click through to one journalist's own history. Answers the follow-up question every morning and still keeps the release control on the letter itself.
  `features/mocks/outreach-outbox.html`
- **C · Verlauf am Kontakt** The contact book becomes the relationship file: pick a journalist and read everything ever sent to them and what came of it, across all mandates. The most natural home for a relationship, but organised by person rather than by what is open, so a forgotten follow-up stays forgotten.
  `features/mocks/outreach-contact-history.html`

### DEC-3: How does an outcome get into the ledger?  [options]
**Status:** locked
**Chosen:** C
**Recommend:** A
**Locks as:** the scope of the outcome capture in OUT-01 and OUT-02
- **A · Von Hand, plus ein Treffervorschlag aus der Berichterstattung** The consultant types what came back. On top of that, when a journalist who was written to publishes about that mandate within thirty days, RauteOS notices it from the byline it already stores and offers it as the result in one click. No credentials, no new data source, and the one outcome that matters most is caught even when nobody remembers to record it.
- **B · Nur von Hand** Every outcome is typed. Simplest and completely predictable, but the result that is easiest to forget is the one that arrives weeks later as a published article.
- **C · Postfach-Anbindung** RauteOS reads the consultant's mailbox and matches replies to letters by sender and subject. The most complete answer, and the only one that catches a reply nobody records. It also needs mail credentials in a tool that today holds none, and it puts client correspondence into the system, which is a different privacy conversation.

### DEC-4: What is the mailbox connection allowed to do?  [mock]
**Status:** locked
**Chosen:** C
**Recommend:** B
**Locks as:** the Gmail scopes requested in OUT-03 and the built letter card in OUT-04
- **A · Nur lesen** RauteOS reads replies and nothing else. Kopieren stays, the consultant writes the mail himself in Gmail. The Grundannahme is untouched and the smallest possible permission is asked for. The cost is that RauteOS did not put the outgoing message into the thread, so matching a reply to a letter falls back to recipient, subject and time window, and will sometimes be wrong, which is why that variant carries a correction control on every reply.
  `features/mocks/gmail-read-only.html`
- **B · Lesen und einen Entwurf in Gmail anlegen** RauteOS writes the letter into Gmail as a draft, addressed and with its subject. The consultant opens Gmail and presses send. Still nothing leaves the house without a human hand on it, so the Grundannahme holds word for word. It also removes the copy-paste step and, more importantly, makes the thread ours: every later reply is matched exactly instead of guessed.
  `features/mocks/gmail-draft.html`
- **C · Lesen und senden** RauteOS sends the mail itself after a confirmation. Fewest clicks and the only variant where the tool controls the whole loop. It also means a text leaves the house because a button in RauteOS was pressed, which is the sentence the product definition currently rules out. Worth choosing deliberately if at all, and not to save a click.
  `features/mocks/gmail-send.html`

### DEC-5: How does RauteOS reach the mailbox?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** the connection mechanism built in OUT-03
- **A · Gmail API über OAuth, als interne App im Google Workspace von RAUTE** Scoped to exactly what DEC-4 allows, revocable from the Google account at any time, no password anywhere, and an Internal app inside your own Workspace needs no Google verification and no annual security assessment. This requires that the mailbox is a Workspace account on a domain you control. If it is a personal `@gmail.com`, this option is not available in practice: an external app would sit in Testing mode, where refresh tokens expire after roughly a week and the daily sync dies every Monday.
- **B · IMAP mit einem App-Passwort** Works on any Gmail account including a personal one, no Google Cloud project, no verification question at all. The price is that an app password is a full-mailbox credential with no scope and no per-action limit: it cannot be restricted to reading, and if RauteOS is compromised the whole mailbox is. Workspace admins can also disable app passwords centrally, so it can stop working without warning.
- **C · Eine eigene Weiterleitungsadresse** The consultant BCCs a dedicated RauteOS address on every letter and forwards replies to it. No credentials, no permissions, nothing to revoke, and it works with any mail provider on earth. It also depends on a human remembering the BCC every single time, which is the same discipline problem this feature exists to solve.

### DEC-6: How much of the mailbox is read?  [options]
**Status:** open
**Recommend:** A
**Locks as:** the Gmail query in OUT-05 and what may be stored
- **A · Nur Verläufe, die zu einem freigegebenen Anschreiben gehören** The sync asks Gmail only for the threads RauteOS itself started, by thread id. Nothing else is fetched, so nothing else can be stored, and the sentence "your mailbox is not read" stays literally true for everything that is not a pitch. Requires DEC-4 option B or C, because without an outgoing message from RauteOS there is no thread id to ask for.
- **B · Alles von Adressen, die im Kontaktbuch stehen** The sync queries by sender address, so a reply is found even when the letter went out by copy-paste. Works with any DEC-4 option. It also pulls in every other exchange with that journalist, including ones that have nothing to do with a pitch, and those land in a client's file.
- **C · Der ganze Posteingang seit dem Verbinden** Every message is read and matched locally. Catches everything, including a reply from an address nobody had recorded. It also means a PR agency's entire correspondence, including client contracts and personal mail, passes through RauteOS, which is a data protection conversation with the agency's own clients rather than a technical choice.

## Stories

### OUT-01: Release a letter, and record what came back

The human act at the end of the pipeline becomes a record. A drafted letter is a
draft until somebody releases it; releasing stamps the time and the person and
freezes the text; what came back is recorded beside it.

States are `entwurf`, `raus`, `antwort`, `absage`, `veroeffentlicht`, as a
`StrEnum` in `models.py` next to the existing `TriageState` and `Category`.
"Ohne Reaktion" is not a stored state: it is derived from `raus` plus age, so the
ledger never claims a fact nobody entered. There are no user accounts in this
tool, so `released_by` follows the `ClientFact.filled_by` precedent and defaults
to `"mensch"`. This story stands on its own and works with no mailbox connected
at all, which is also how it stays testable once one is.

**Changes:**
- Add `OutreachState` `StrEnum` and the `SILENT_AFTER_DAYS = 14` constant to `src/newspulse/models.py`, plus columns on `Outreach`: `contact_id` (FK `contacts.id`, nullable, `ondelete="SET NULL"`), `state`, `released_at`, `released_by`, `outcome_at`, `outcome_note`
- Alembic revision `0018_outreach_ledger` with `down_revision = "0017_client_facts"`, using `render_as_batch` the way the existing revisions do; existing rows become `entwurf`
- Add `release()`, `record_outcome()` and `is_silent()` to `src/newspulse/outreach.py`, and change `store()` so it only overwrites a row that is still a draft
- Add `POST /client/{client_id}/outreach/{outreach_id}/release` and `POST /client/{client_id}/outreach/{outreach_id}/outcome` to `src/newspulse/web/routes/advisory.py`
- Add the state strip (badge, release button, outcome form, release trail) to the letter card in `advice.html`, and the new German strings to `i18n._EN`

**Acceptance:**
- A letter card shows exactly one state of Entwurf, Verschickt, Antwort, Absage or Veröffentlicht, and a released letter with no outcome after 14 days is additionally marked as still.
- Releasing records `released_at`, `released_by` (`"mensch"` when nothing is supplied) and sets the state to `raus`; releasing the same letter twice leaves the first timestamp unchanged.
- Recording an outcome on a letter that was never released is refused, returns 400, and leaves the row untouched.
- Redrafting for the same angle and recipient overwrites the row only while it is still a draft; once released, the redraft is a new row and the released letter keeps its own subject and message verbatim.
- Releasing resolves the recipient through `contacts.find()` and stores `contact_id` when an entry matches, and leaves it null rather than guessing when none does.
- The outcome note is stored as typed and rendered under the badge on the card; recording an outcome sets `outcome_at`.
- Every new German UI string has an English entry in `i18n._EN` and `tests/test_i18n.py` passes unchanged.
- `alembic upgrade head` succeeds on an empty database and on one that already holds outreach rows, and those pre-existing rows read back as drafts.

**Files:** `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0018_outreach_ledger.py` (new), `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/web/routes/advisory.py`, `newspulse/src/newspulse/web/templates/advice.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_outreach_ledger.py` (new)

**Smoke:** `pytest tests/test_outreach.py tests/test_outreach_ledger.py tests/test_migration.py` passes

### OUT-02: Verlauf am Kontakt
**Depends on:** OUT-01
**Decisions:** DEC-2

The contact book becomes the relationship file. It keeps its list and its edit
form and gains a second pane: pick a journalist and read everything ever sent to
them across all mandates, in one timeline, with what came of it.

Deliberately across mandates, not per mandate: a journalist is a relationship the
agency holds, and the question "have we already gone to her with something this
month" cannot be answered from inside one client's workspace. The mandate is
named on every line instead.

**Changes:**
- Extend `GET /contacts` in `src/newspulse/web/routes/contacts.py` with a selected contact (`?id=`), keeping the existing search, add and delete behaviour untouched
- Rebuild `contacts.html` as the two-pane layout from the locked mock: the roster with a per-contact letter count on the left, the file with its header, the four tallies and the timeline on the right
- Add `history_for_contact()` to `src/newspulse/outreach.py`, returning released letters plus their outcomes for one contact, newest first, joined to the mandate
- Mark a `pitch.PitchTarget` that already received a released letter for the same angle within 90 days, with the date of that letter
- German strings into `i18n._EN`

**Acceptance:**
- `/contacts?id=<n>` shows that journalist's timeline: every released letter across all mandates, newest first, each entry naming the date, the mandate, and the letter's subject; drafts never appear.
- The four tallies (Anschreiben, Antworten, Veröffentlicht, ohne Reaktion) count the same rows the timeline shows.
- Every timeline entry says where it came from, and an entry typed by a human is visually distinct from one the system recorded.
- A journalist with no released letters shows the file with an empty timeline and a line saying so, not a blank pane or an error.
- The roster shows a letter count per contact and keeps working with the existing `?search=` filter.
- Deleting a contact leaves their released letters intact and readable, with the recipient still named from the letter's own `journalist` and `outlet` fields.
- A pitch target that already received a released letter for the same angle within 90 days is shown with the date of that letter.
- The page matches the locked mock `features/mocks/outreach-contact-history.html`.

**Files:** `newspulse/src/newspulse/web/routes/contacts.py`, `newspulse/src/newspulse/web/templates/contacts.html`, `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/pitch.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_contact_history.py` (new)

**Smoke:** `pytest tests/test_contacts.py tests/test_contact_history.py tests/test_pitch.py` passes

### OUT-03: Connect a Gmail account

One mailbox, connected once, in settings. The OAuth round trip, the token that
survives a restart, an honest connected state, and a disconnect that actually
revokes rather than just forgetting.

Nothing in this story reads mail yet. It ends at a settings panel that can say
"verbunden als lucas@raute.example" and prove it by fetching the profile. The
scopes requested are exactly what DEC-4 licenses and no more, so choosing "nur
lesen" there means the consent screen Google shows says read-only.

The client id and secret come from `NEWSPULSE_GMAIL_CLIENT_ID` and
`NEWSPULSE_GMAIL_CLIENT_SECRET` the same way `GEMINI_API_KEY` does. The refresh
token cannot: it is obtained at runtime, so it is written to a file beside the
SQLite database on the Railway volume with mode 0600, never to a table.

**Changes:**
- Add the Gmail env vars and a `gmail_configured()` predicate to `src/newspulse/config.py`, deriving the redirect URI from the existing `BASE_URL`
- New `src/newspulse/gmail_link.py`: `authorize_url()`, `exchange()`, `token()` with refresh-on-expiry, `profile()`, `connected()`, `disconnect()`, and the 0600 token file; every network call goes through an injected `fetch` so tests never reach Google
- Add `GET /settings/gmail/start`, `GET /settings/gmail/callback` and `POST /settings/gmail/disconnect` to `src/newspulse/web/routes/settings.py`, with a `state` parameter checked on return
- A connection panel in `settings.html`: not configured, configured but not connected, connected as an address with the granted scopes named, and a disconnect button
- The Google Cloud project and Workspace Internal setup written down in `docs/deployment.md`, including what happens on a non-Workspace account
- German strings into `i18n._EN`

**Acceptance:**
- With no client id configured, the panel says the integration is not set up and links to the deployment note; it never renders a connect button that would fail.
- Starting the flow redirects to Google with exactly the scopes DEC-4 licenses, and a `state` value that is checked on the callback; a callback with a wrong or missing `state` is refused and connects nothing.
- A completed callback stores the refresh token in a file with mode 0600 beside the database, and no token value is ever written to the database or to the log.
- The panel then shows the connected address, read back from the Gmail profile rather than from what the user typed, and the granted scopes in words.
- An expired access token is refreshed transparently on the next call; a refresh that fails because access was revoked at Google puts the panel back to disconnected with a line saying why, rather than raising.
- Disconnect revokes the token at Google, deletes the local file, and leaves every stored letter and reply untouched.
- The user declining consent at Google returns to the panel with a plain German line and nothing stored.
- No test in this story performs a network call.

**Files:** `newspulse/src/newspulse/config.py`, `newspulse/src/newspulse/gmail_link.py` (new), `newspulse/src/newspulse/web/routes/settings.py`, `newspulse/src/newspulse/web/templates/settings.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/docs/deployment.md`, `newspulse/tests/test_gmail_link.py` (new)

**Smoke:** `pytest tests/test_gmail_link.py tests/test_config.py` passes

### OUT-04: The letter goes out through Gmail
**Depends on:** OUT-01, OUT-03

Written against DEC-4 option B: RauteOS puts the released letter into Gmail as a
draft, addressed and with its subject, and the consultant presses send there. If
DEC-4 locks A, this story shrinks to recording the Gmail thread of a letter the
consultant sent himself, found in the Sent folder; if it locks C, the same code
path calls send instead of drafts.create and the confirmation step from that mock
applies.

The point is not the saved copy-paste. It is that the thread becomes ours, which
is what turns OUT-05's reply matching from a guess into a fact, and what lets
DEC-6 option A read nothing but the threads RauteOS started.

**Changes:**
- Add `gmail_draft_id`, `gmail_thread_id` and `gmail_message_id` to `Outreach` in `src/newspulse/models.py`
- Alembic revision `0019_outreach_gmail` with `down_revision = "0018_outreach_ledger"`
- Add `create_draft()` and `thread()` to `src/newspulse/gmail_link.py`, building a correctly encoded RFC 5322 message with a UTF-8 subject
- Add `POST /client/{client_id}/outreach/{outreach_id}/gmail-draft` to `src/newspulse/web/routes/advisory.py`
- The letter card in `advice.html` gains the draft action, the "liegt in Gmail" state, the link into the Gmail thread, and the disabled state with its reason when no address is known
- German strings into `i18n._EN`

**Acceptance:**
- A released letter to a contact with an email address can be pushed to Gmail, and the stored draft id, thread id and message id come back from the API response rather than being constructed locally.
- The card then shows "Liegt in Gmail" with a working link into that thread, and the Kopieren button is gone for that letter.
- A letter whose recipient has no address in the contact book shows the action disabled with the reason named, and no address is ever derived from a name or an outlet pattern.
- Umlauts and the German subject line survive the round trip: a draft fetched back from Gmail has the same subject and body text that was stored, byte for byte after decoding.
- Pushing the same letter twice updates the existing draft rather than creating a second one.
- When the letter is later sent from Gmail, the next sync records `released_at` from the message's own send date if it was not already released by hand.
- With no mailbox connected the action is not offered at all, and the Kopieren path from OUT-01 still works unchanged.
- The card matches the locked mock for DEC-4.

**Files:** `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0019_outreach_gmail.py` (new), `newspulse/src/newspulse/gmail_link.py`, `newspulse/src/newspulse/web/routes/advisory.py`, `newspulse/src/newspulse/web/templates/advice.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_gmail_draft.py` (new)

**Smoke:** `pytest tests/test_gmail_draft.py tests/test_outreach_ledger.py tests/test_migration.py` passes

### OUT-05: Read the replies, and file them against the letter
**Depends on:** OUT-02, OUT-04

The sync. Once a day, with the existing sweep, RauteOS asks Gmail for the threads
belonging to released letters, takes any message in them that is not its own, and
files it as the reply to that letter.

A reply is stored as text and never interpreted into a state on its own: the
system sets Antwort because a human answered, and leaves Absage or Veröffentlicht
to the consultant, because "danke, nichts für uns" and "schicken Sie mehr" are the
same event to a matcher and opposite events to a PR consultant. One letter can
collect several replies, so replies are their own table rather than a column.

**Changes:**
- New `src/newspulse/mailsync.py`: `sync()` over letters with a thread id, skipping messages sent by the connected account, storing one row per reply, idempotent on the Gmail message id
- Add an `OutreachReply` model to `src/newspulse/models.py` (`outreach_id`, `gmail_message_id` unique, `from_name`, `from_email`, `received_at`, `body`, `fetched_at`)
- Alembic revision `0020_outreach_replies` with `down_revision = "0019_outreach_gmail"`
- Call `mailsync.sync()` from the daily run in `src/newspulse/job.py`, after ingest, guarded so a mail failure never fails the sweep
- Set the letter's state to `antwort` on the first reply, unless a human has already recorded an outcome, in `src/newspulse/outreach.py`
- Render replies in the contact timeline in `contacts.html`, each marked as coming from the mailbox
- German strings into `i18n._EN`

**Acceptance:**
- The sync fetches only threads that belong to a released letter; a mailbox holding unrelated mail yields no stored rows, and the Gmail query is asserted in a test.
- A reply from the journalist is stored once; running the sync twice over the same mailbox creates no second row and changes no timestamp.
- Messages sent by the connected account itself are never stored as replies, including the outgoing letter that started the thread.
- The first reply moves the letter to `antwort` and sets `outcome_at` to the message's own received time; a letter where a human already recorded Absage or Veröffentlicht keeps that outcome and still stores the reply.
- The reply appears in the contact's timeline with the sender, the received time, its text, and a marker saying it came from the mailbox.
- A sync that fails because Gmail is unreachable or access was revoked logs at ERROR, leaves every stored row unchanged, and does not fail the daily sweep.
- With no mailbox connected the sync is a no-op and the daily run is unaffected.
- Deleting a letter deletes its replies with it, so no reply text outlives the letter it belonged to.

**Files:** `newspulse/src/newspulse/mailsync.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0020_outreach_replies.py` (new), `newspulse/src/newspulse/job.py`, `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/web/templates/contacts.html`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_mailsync.py` (new)

**Smoke:** `pytest tests/test_mailsync.py tests/test_job.py tests/test_contact_history.py` passes

## Deferred

- **"Was ist offen" über alle Mandate.** DEC-2 locked the history to the contact
  book, which is the right home for a relationship and does not answer the morning
  question of what is waiting on a reply anywhere in the portfolio. The Ausgang
  page is drawn and costs about a day once the ledger exists:
  `features/mocks/outreach-outbox.html`.
- **Coverage as an outcome (DEC-3 option A).** A mailbox never tells you an
  article appeared, so Veröffentlicht stays a hand entry. Matching a released
  letter to a later byline by the same journalist uses data already in the
  database and would close the last gap in the ledger.
- **A second mailbox.** One connected account is specified. Several consultants
  each with their own Gmail means per-user tokens, and this tool has one shared
  login, so it is a change to the auth model rather than to the mail code.
- **Follow-up prompts.** Once "silent for 14 days" is a fact the system knows, it
  can propose a second letter. Worth doing after the first weeks of real data show
  what the useful interval actually is.
- **Reporting on the ledger (CS-06, PR-08).** Pitches sent, answer rate, coverage
  attributed to outreach, per mandate and per period. The ledger is the source it
  needs; the report is its own feature.
- **Retention on stored replies.** A journalist's mail sits in the database
  indefinitely. A deletion rule, by age or on request, belongs in the same
  conversation as the agency's own data protection commitments.
