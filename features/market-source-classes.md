# Studies, regulation and dates: the market signals a news feed never carries

prefix: SRC

**Type:** feature
**Complexity:** 4
**Estimated Duration:** ~1 week
**Risk:** medium
**Scope:** newspulse, market
**Test Strategy:** unit tests over captured fixture payloads for each new source class, kept in `tests/fixtures/` the way the RSS fixtures already are, covering parsing, deduplication against the news radar, and the dated-in-the-future case that news items never have; a deterministic end-to-end pass over fixture sources asserting a second run creates zero duplicates; `TestClient` tests for the market view rendering each class separately and rendering nothing when a class is empty. No test performs a network call.

## Context

PR-02 watches markets, competitors, topics, studies, regulation and trends, and
turns them into communication opportunities. Half of that is built. The topic
radar runs per mandate, bound to the client's field, and `themes.py` measures which
theme wordings actually return German press coverage instead of trusting what an
operator typed. The product definition names what is missing: "Es fehlen Studien,
Regulierungskalender und Veranstaltungen als eigene Quellenklassen."

They are missing because they are not news, and everything in the tool is built for
news. A news item has happened; it has a headline, a publication and a date in the
past, and its value decays within days. The three missing classes each break that
shape in a way that matters:

- **A study** is not an event but a body of evidence, and its value to a PR
  consultant is that it can be cited for months. It is also the single strongest
  raw material for a positioning: PR-03's whole job is a thesis that can be
  backed, and a study is a backing.
- **Regulation** is dated in the *future*. A consultation closing in six weeks, a
  law taking effect next quarter, a reporting duty starting in January: the entire
  value is in the lead time, and a feed that only reports what already happened
  delivers it on the day it is too late to say anything.
- **An event** is a date and a stage. It is the one class that answers "where can
  this client be heard" rather than "what was said about this client", and it is
  the only one with a deadline attached, because a call for speakers closes.

Treating them as three more RSS feeds would lose exactly what makes them worth
having. A regulatory item filed under "what happened today" and ranked by
importance next to a product launch is an item nobody will act on in time. So each
class gets its own shape, its own place on the market page, and its own sense of
when it matters.

The engine stays grade A: it watches and reports, it decides nothing.

## Summary

Today the market radar reads news, which means it sees a market only after the
market has spoken. A study that would back a client's thesis for the next six
months, a consultation that closes in five weeks, a conference whose call for
speakers shuts on Friday: none of it exists in the tool, and all of it is where a
PR consultant's lead time comes from.

After this build, the market view carries four kinds of signal rather than one.
Studies are listed with who published them and what they measured, and can be
attached to a positioning as its evidence. Regulation is a forward calendar: what
is coming, when it lands, and how many weeks are left. Events show what is being
staged in this client's field, with the speaker deadline if there is one. Each
class says where it came from and each is kept out of the client's own coverage,
so the separation the tool already enforces between "news about my client" and
"news about my client's world" now holds for three more kinds of world.

## Decisions

### DEC-1: Where do studies, regulation and events come from?  [options]
**Status:** open
**Recommend:** B
**Locks as:** the fetchers built in SRC-01
- **A · Kuratierte Quellen je Klasse** A maintained list per class: statistics offices, the big institutes and consultancies for studies; the EU and federal publication feeds for regulation; the trade fair and association calendars for events. Highest precision, everything is attributable, and nothing arrives that nobody chose. It is also a list that has to be kept, and a mandate in a field nobody curated for gets nothing.
- **B · Kuratierte Basis plus gezielte Suche im Feld des Mandanten** The curated list carries the sources that apply to everyone, and on top of it the existing search runs per mandate with class-specific query shapes, using the industry term the tool already measures. Covers a field nobody anticipated, keeps the reliable sources reliable, and the search half will surface things that are not really studies, so each item says which half it came from.
- **C · Nur Suche** No curated list at all; every class is a query shape over the search the tool already uses. Nothing to maintain and it works for any field on day one. It also means the regulatory calendar is assembled from whatever a search engine felt like returning, which is not a calendar.

### DEC-2: How long does a market signal stay on the page?  [options]
**Status:** open
**Recommend:** C
**Locks as:** the retention and ranking rule in SRC-02
- **A · Wie Nachrichten: nach Datum, und dann weg** Everything ages out on the same 90-day window the coverage uses. One rule for the whole tool, nothing new to explain. It also throws away a study six weeks after it became citable, which is roughly when a consultant would want it.
- **B · Je Klasse eine eigene Frist** Studies live a year, regulation lives until its date has passed, events disappear the day after they happen. Matches how each thing actually behaves. Three rules instead of one, and a fourth the day a fifth class arrives.
- **C · Nach Verwendbarkeit statt nach Alter** Each class carries the date that makes it actionable, its own or its deadline, and the page ranks by how close that date is rather than by how recently it was found. A study with no date sorts by publication; regulation and events sort by what is next. Old things fall off the page because nothing is coming, not because a timer expired, and an item that becomes relevant again is not gone.

## Stories

### SRC-01: Fetch the three classes, and keep them apart from the news
**Decisions:** DEC-1

The ingest side. Three fetchers, one shape, stored in a way that keeps a study
from ever appearing where a press clipping belongs.

Deliberately a separate table rather than a flag on `articles`. An article is
what a feed syndicated about a company, it obeys the no-body-text rule for
Leistungsschutzrecht reasons, and every query in the tool that touches coverage
assumes that shape. A regulatory date is not a clipping and putting it in that
table would make every one of those queries wrong in a way nobody would notice
until a client report counted a consultation as coverage.

**Changes:**
- New `MarketSignal` model in `src/newspulse/models.py`: `client_id`, `kind` (a `StrEnum` of `studie`, `regulierung`, `veranstaltung`), `title`, `publisher`, `url` unique per client, `found_at`, `published_at`, `effective_at` (the date it lands or opens), `deadline_at`, `summary`, `origin` (curated or search)
- Alembic revision `0023_market_signals`
- New `src/newspulse/market_sources.py`: one fetcher per class behind a common interface, each with an injected `fetch`, plus the curated source list as data rather than code
- Call the fetchers from the daily sweep in `src/newspulse/job.py`, per active client, guarded per class so one failing class never stops the others
- Deduplicate against existing signals and against the client's own coverage, so a study already picked up as a news item does not appear twice

**Acceptance:**
- Each class is fetched, parsed and stored with its own kind, and an item carries the date that makes it actionable rather than only the date it was found.
- A regulatory item with a future effective date is stored with that date in the future; nothing in the pipeline treats a future date as an error or clamps it to now.
- Running the sweep twice over the same fixture sources creates zero duplicate signals, on the URL and on a title match for sources whose URLs change.
- A signal is never written into `articles`, and a test asserts the coverage queries return the same rows before and after a market sweep.
- Signals are scoped to the client they were fetched for, so one mandate's market never appears under another, matching the separation the topic radar already enforces.
- Each stored signal records whether it came from a curated source or from a search, and that provenance is available to the view.
- A class whose source is unreachable logs at ERROR, stores nothing for that class, and leaves the other two classes and the news sweep unaffected.

**Files:** `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0023_market_signals.py` (new), `newspulse/src/newspulse/market_sources.py` (new), `newspulse/src/newspulse/job.py`, `newspulse/src/newspulse/feeds.py`, `newspulse/tests/test_market_sources.py` (new), `newspulse/tests/fixtures/` (new fixture payloads)

**Smoke:** `pytest tests/test_market_sources.py tests/test_job.py tests/test_migration.py` passes

### SRC-02: The market view learns to read a calendar
**Depends on:** SRC-01
**Decisions:** DEC-2

`client_market.html` today shows one list. It gains three more, and the regulatory
one is not a list but a calendar: what is coming, in what order, with the weeks
remaining spelled out, because "in 5 Wochen" is a different instruction to a
consultant than a date in a column.

**Changes:**
- Extend `GET /client/{id}/market` in `src/newspulse/web/routes/client.py` to load signals by class alongside the existing radar items
- Rework `client_market.html` into four sections: the existing theme radar, studies, the regulatory calendar and events, each with its own empty state
- Show the remaining time on anything with a future date or a deadline, and mark a deadline inside two weeks
- Add a per-class mute, reusing the pattern `Client.muted_categories` already establishes
- German strings into `i18n._EN`

**Acceptance:**
- The four sections render independently, and a class with no signals shows a specific empty line rather than an empty box or a collapsed section.
- Regulation is ordered by when it lands, soonest first, and each row shows the remaining time in weeks; an item whose date has passed leaves the calendar under the locked rule from DEC-2.
- Events show the event date and, when there is one, the speaker deadline; a deadline within two weeks is visually marked.
- Studies show the publisher and what was measured, and link out to the source.
- Each row says whether it came from a curated source or from a search, so a search-found item is judged as one.
- A muted class disappears from the page for that client and stops being fetched for them on the next sweep.
- A client whose industry term is not usable gets the curated signals and a line explaining why the searched ones are missing, reusing `industry.field_is_usable()` rather than failing silently.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/web/routes/client.py`, `newspulse/src/newspulse/web/templates/client_market.html`, `newspulse/src/newspulse/models.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_market.py`

**Smoke:** `pytest tests/test_market.py tests/test_market_sources.py` passes

## Deferred

- **A study as the evidence under a thesis.** PR-03 asks for a position that can
  be backed, and a study is the strongest backing there is. Attaching one to an
  impulse so the letter can cite it is the obvious next step, and it belongs with
  the positioning work rather than here.
- **The calendar as a reason to write.** A consultation closing in five weeks is a
  deadline the tool could act on: propose a statement now. That is the
  orchestrator's job, L2, and it needs the calendar to exist first.
- **Sources per mandate.** A client with an industry association that publishes
  everything worth knowing should be able to name it. Today the curated list is
  global; making it per mandate is a settings change once the global list has
  proven its shape.
- **Events the client is already speaking at.** The tool sees what is being staged
  and not what the mandate already agreed to. That is calendar data the agency
  holds elsewhere, and importing it is a different conversation.
