# The client report: what the work was worth, with the evidence attached

prefix: RPT

**Type:** feature
**Complexity:** 4
**Estimated Duration:** ~1 week
**Risk:** medium
**Scope:** newspulse, reporting
**Test Strategy:** unit tests over the metric functions against a seeded archive with a known answer, so every figure in a report is checked against a number computed by hand in the fixture rather than against itself; a test that every claim in a generated report resolves to at least one stored article or ledger row, and that a claim whose evidence is deleted stops rendering; golden-file tests on the rendered report for one seeded month so wording drift is reviewable; `TestClient` coverage of generating, editing, dropping a finding and releasing. The interpretation is generated with an injected `generate`, so no test reaches a model.

## Context

CS-06 makes the value of the work visible: "Report mit Kennzahlen, Belegen und
Einordnung". The product definition is unusually terse about the current state,
and correct: "Ein Excel-Export je Mandant existiert. Ein Report ist er nicht."

`reporting.py` already does the hard half. `share_of_voice()` computes each
monitored company's part of the month against a comparison set that is a real
`Client` carrying `is_competitor`, so competitor coverage is matched, analysed and
archived exactly like a mandate's. `client_workbook()` writes coverage, voice, gaps
and impulses into four sheets. The numbers are sound and they are traceable.

What is missing is everything that makes a number mean something. A spreadsheet
with 24 rows says a mandate was mentioned 24 times. It does not say that the
increase came from one theme rather than from more activity, that three of those
mentions were caused by letters this agency sent, or that two negative pieces are
local today and will not stay local. That is the judgement a client pays for, and
it is currently retyped every month from a dashboard into a document.

Two things have changed that make this buildable now rather than aspirational.
The outreach ledger connects a piece of coverage to the pitch that produced it, so
"drei der 24 Beiträge gehen auf eigene Ansprache zurück" is a query rather than a
memory. And the guide gives the report something to be measured against, so
whether the message landed is answerable rather than felt.

The constraint that shapes the build is the same one that shapes the rest of
RauteOS: every number is traceable to the coverage under it, and nothing is
estimated. Reach, impressions and advertising equivalence are the standard filler
of PR reporting and RauteOS holds none of that data. A report that quietly
estimates them would be the first thing in this system that a client could catch
out, so a figure the tool cannot source is not printed with a caveat, it is not
printed.

Grade F, and the strictest reading of it: this is the artefact that goes to the
client under the agency's name.

## Summary

Today the monthly report is an Excel export plus an evening of retyping. The
numbers in the tool are right and they arrive as four spreadsheet tabs, so the
part a client actually pays for, what the numbers mean and what to do next, is
written from scratch every month against a dashboard.

After this build, RauteOS reads the month and proposes findings: a claim in one
sentence, what follows from it, and underneath it the specific articles and
outreach records the claim rests on. The consultant keeps, edits or drops each
one, and what survives becomes a dated document with the figures, the evidence and
the interpretation in it. Every figure traces to coverage that can be opened.
Anything the tool cannot source, reach above all, is absent rather than estimated,
and a claim whose evidence is thin is offered as thin rather than rounded up.
Nothing reaches a client without a release.

## Decisions

### DEC-1: What is a report, as an object?  [mock]
**Status:** open
**Recommend:** C
**Locks as:** the built surface, matched against the chosen mock
- **A · Ein Dokument** A fixed set of sections rendered to a printable page: figures, charts, tables, and comment boxes the consultant writes. Closest to what actually gets sent, it exports cleanly, and a client recognises it immediately. It also means the tool lays out the paper and the consultant still supplies every sentence of judgement, which is the expensive part of the evening.
  `features/mocks/report-document.html`
- **B · Eine lebende Seite** A page in the tool that is always current, with one filter row over charts and tiles, plus an export at the bottom. Cheapest to build on `reporting.py` as it stands, useful daily rather than monthly, and the natural precursor to the client portal in CS-03. It is also not a report: it has no date, no argument and no release, and the retyping evening survives it.
  `features/mocks/report-dashboard.html`
- **C · Ein belegter Befund** RauteOS proposes findings, each a claim with its consequence and the articles and outreach records underneath it. The consultant keeps, edits or drops each, and the survivors become the document. It does the part that costs the evening, it makes every claim checkable in the second before it is approved, and it fails visibly rather than silently when the month has nothing to say. It is the most model-dependent of the three, and a bad finding is a wrong sentence about a client's month, which is why every one arrives with its evidence attached and none is generated without any.
  `features/mocks/report-findings.html`

### DEC-2: What counts as a figure the report may print?  [options]
**Status:** open
**Recommend:** A
**Locks as:** the metric set in RPT-01 and what may never appear
- **A · Nur was aus dem Archiv und dem Ledger kommt** Coverage counts, share of voice against the named comparison set, tonality, lead media, and outreach attribution. Every figure resolves to rows a reader can open. Reach, impressions and advertising value are absent, and a client used to seeing them will ask where they are, which is a conversation worth having once rather than a number worth faking monthly.
- **B · Dazu geschätzte Reichweite mit ausgewiesener Methode** Outlet reach from a maintained table, multiplied out, clearly labelled as an estimate. It answers the question clients ask and it puts a number in the agency's own report that the agency cannot defend from its own data, which is a different kind of exposure than not having it.
- **C · Geschätztes offen lassen, vom Berater füllbar** The report has slots for figures RauteOS cannot source, filled by hand from whatever the agency subscribes to, and marked as externally sourced. Honest about provenance and it makes the report depend on a monthly manual step, which is the step this feature exists to remove.

### DEC-3: How often does a report happen?  [options]
**Status:** open
**Recommend:** B
**Locks as:** the trigger in RPT-03
- **A · Auf Knopfdruck** The consultant asks for a report for a period. Simple, predictable, nothing runs unasked. The report also then happens when someone remembers it, which for a jour fixe on the third of the month means the second of the month, at night.
- **B · Zum Stichtag vorbereitet, nie verschickt** On the first of each month RauteOS drafts last month's report for every active mandate and leaves it waiting. The work is done before it is needed and nothing goes anywhere without a release, which matches how every other generator in this tool already behaves. It costs one model call per mandate per month, and drafts nobody opens.
- **C · An den Vertrag gebunden** Reporting follows the reporting rhythm in the contract, monthly for one mandate, quarterly for another. Correct, and it needs OPS-04 Contract & Scope, which is open, so the rhythm would have to be typed per mandate and would drift from the contract it claims to follow.

## Stories

### RPT-01: The figures a report is allowed to make
**Decisions:** DEC-2

The measurement layer, extending `reporting.py` rather than replacing it.
`share_of_voice()` stays as it is; what is added is everything a claim needs and
the rule about what may not be claimed.

Every metric returns its figure together with the rows it was computed from, not
just the number. That is what makes the rest of the feature possible: a finding
cites evidence because the metric handed the evidence over, rather than because a
model was asked to remember where a number came from.

**Changes:**
- Add to `src/newspulse/reporting.py`: `period_metrics()` returning coverage count, share of voice, tonality split, lead-media count and their comparison against the previous period, each carrying the analyses behind it
- Add `attributed_coverage()` joining released letters from the outreach ledger to coverage by the same journalist within the attribution window, so "aus eigener Ansprache" is computed rather than asserted
- Add `message_pull_through()`: how often the guide's own key messages appear in the coverage, using the same matching `coach.py` already applies
- A single `MetricValue` shape carrying figure, previous figure, direction, and the ids of the rows behind it
- A guard listing the figures RauteOS may not produce, so a future prompt cannot introduce reach by wording

**Acceptance:**
- Every metric returns its figure with the ids of the analyses or outreach rows it was computed from, and a test asserts recomputing from those ids alone reproduces the figure.
- Share of voice uses the mandate's own stored comparison set and never a set inferred from industry; a mandate with no comparison set gets no share of voice figure and says why.
- Attributed coverage counts a piece only where a released letter to that journalist precedes it within the window, and a piece matching two letters is counted once.
- The previous-period comparison uses the same length of period, so a partial month is never compared against a full one.
- A period with no coverage returns empty metrics rather than zeros, because zero coverage and no data are different statements about a month.
- No metric function can return reach, impressions or advertising value, and a test asserts the forbidden list is empty of results.
- Existing `share_of_voice()` and `client_workbook()` behaviour is unchanged; their tests pass untouched.

**Files:** `newspulse/src/newspulse/reporting.py`, `newspulse/src/newspulse/outreach.py`, `newspulse/src/newspulse/coach.py`, `newspulse/tests/test_reporting.py`, `newspulse/tests/test_report_metrics.py` (new)

**Smoke:** `pytest tests/test_report_metrics.py tests/test_reporting.py` passes

### RPT-02: Findings, each carrying its evidence
**Depends on:** RPT-01
**Decisions:** DEC-1

The judgement layer. Given a month of metrics and coverage, propose the handful of
things worth saying, each as a claim, a consequence and the rows underneath it.

The model is given the metrics and the headlines and is asked for claims that the
evidence supports. It is not given the freedom to compute: a finding may only cite
figures that came from RPT-01, and a finding whose evidence list is empty is
discarded before a human ever sees it. That inversion is the whole safety argument
of this story, and it is why the evidence is attached by the code rather than
quoted by the model.

**Changes:**
- New `src/newspulse/report.py`: `findings()` with an injected `generate`, `store()`, and the discard rule for unevidenced claims
- New `Report` and `ReportFinding` models in `src/newspulse/models.py` (`client_id`, `period_start`, `period_end`, `state`, `generated_at`, `released_at`, `released_by`; and per finding `kind`, `claim`, `consequence`, `evidence_ids`, `kept`, `edited_at`)
- Alembic revision `0028_reports`
- New `prompts/report_findings.txt`, composing the brain blocks so the standard for what is worth saying is the house standard
- Structural validation that every returned finding names figures from the metric set and nothing else

**Acceptance:**
- Every stored finding carries at least one evidence id; a finding the model returns without evidence is discarded and the discard is logged, not shown.
- A finding may only cite figures produced by RPT-01; a claim naming a figure outside that set is rejected rather than rendered.
- Deleting or dismissing an article that a finding cites causes that finding to render as weakened with the remaining evidence, rather than silently keeping a claim whose ground moved.
- A month with no coverage produces no findings and a stated reason, following the precedent that silence is an acceptable answer in this codebase.
- Findings are typed, so a risk finding and a visibility finding are distinguishable without reading them.
- The same period generated twice replaces the draft rather than creating a second report, and a released report is never replaced.
- The prompt composes the shared standards rather than restating them.

**Files:** `newspulse/src/newspulse/report.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0028_reports.py` (new), `newspulse/src/newspulse/prompts/report_findings.txt` (new), `newspulse/tests/test_report.py` (new)

**Smoke:** `pytest tests/test_report.py tests/test_report_metrics.py tests/test_migration.py` passes

### RPT-03: Read it, cut it, release it, send it as a document
**Depends on:** RPT-02
**Decisions:** DEC-1, DEC-3

The surface and the artefact. Findings reviewed and cut down, the survivors
rendered as a dated document, released once, and exported.

The release is the same act the outreach ledger defines, and it means the same
thing: a human read this and put the agency's name on it. A released report is
frozen, because a document a client has been sent must still say next quarter what
it said when it was sent.

**Changes:**
- New route module `src/newspulse/web/routes/report.py`: the review surface, per-finding edit, keep and drop, generate, release, and the rendered document
- New templates for the review surface and the document, matching the locked mock
- A Berichte entry in `_client_tabs.html`
- Charts as inline SVG with no external dependency, following the existing offline rule; the mandate highlighted and the comparison set neutral, every tonality segment carrying its own number so no figure depends on hue alone, and a table view of every chart
- The scheduled draft per DEC-3, from `src/newspulse/job.py` or the scheduler, guarded so a failure never fails the sweep
- German strings into `i18n._EN`

**Acceptance:**
- Each finding can be edited, kept or dropped, and a dropped finding stays visible as dropped with its reason rather than disappearing.
- The document renders only kept findings, with their evidence, the period, the comparison set and the generation date on it.
- Releasing freezes the report: findings, figures and evidence stay as released even when the underlying coverage is later re-triaged, and a test asserts a released report renders identically after the archive changes.
- A released report cannot be regenerated or edited, matching the letter's immutability rule.
- Every chart has a table view reachable from the page, every tonality segment carries its own figure, and no value can be read only from a colour.
- Charts render offline with no external request, following the existing vendored-assets rule.
- The export carries the same content as the on-screen document, and a figure that is on one is on the other.
- The surface matches the locked mock for DEC-1.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/web/routes/report.py` (new), `newspulse/src/newspulse/web/templates/report_review.html` (new), `newspulse/src/newspulse/web/templates/report_document.html` (new), `newspulse/src/newspulse/web/templates/_client_tabs.html`, `newspulse/src/newspulse/web/app.py`, `newspulse/src/newspulse/job.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_report_view.py` (new)

**Smoke:** `pytest tests/test_report_view.py tests/test_report.py tests/test_job.py` passes

## Deferred

- **PDF with the agency's letterhead.** The document renders and prints; a real
  branded PDF is what an agency sends, and it is the same question `asset-formats.md`
  defers about exporting a press release. Both should be answered once.
- **The report in the client portal (CS-03).** A released report is exactly the
  artefact a client portal would show, and the portal is the security decision the
  onboarding link also runs into.
- **Reach, if it is ever wanted (DEC-2 options B and C).** Left out on purpose. If
  the agency decides an estimate is better than an absence, the metric layer has a
  place for it and the report has a way to mark provenance.
- **Comparing two periods side by side.** The report compares against the previous
  period as a direction. A quarterly review that reads three months against each
  other is a different document and deserves its own shape.
- **Learning which findings survive.** A consultant who drops the same kind of
  finding every month is telling the system something. That is L8's signal and it
  needs a few months of reports to exist first.
