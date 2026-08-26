# KI-Sichtbarkeit: was ein Assistent antwortet, wenn jemand nach diesem Markt fragt

prefix: KIS

**Type:** feature
**Complexity:** 4
**Estimated Duration:** ~1 week
**Risk:** medium
**Scope:** newspulse, visibility
**Test Strategy:** unit tests over captured model answers kept as fixtures in `tests/fixtures/`, the way the RSS payloads already are, covering the naming check, the position rank, the competitor intersection, the source extraction and the failed-provider case; a deterministic end-to-end pass over fixture answers asserting a second measurement inside the window spends no call; `TestClient` tests for the page rendering the standing, the movement, the not-yet-measured state and the no-questions state. No test performs a network call and no test invokes a model.

## Context

RauteOS knows precisely who wrote about a mandate. It has no idea what a machine
says about it, and that is now half the question a client asks.

The tool measures one channel: German media, swept daily, matched, judged, counted
in share of voice. The channel it does not measure is the one that has quietly
become the first stop for the buyer of a solar system, a compliance tool or a
managed service. Somebody asks an assistant "welche Anbieter gibt es", and the
answer names three companies. Whether the mandate is one of them is a
communications fact of the same order as a piece in the Handelsblatt, and today
nobody in this tool can state it.

The gap is total. There is no prompt tracking, no probe, no share-of-answer
figure, nothing. Meanwhile everything needed to build it is already here: the
mandate profile says what the company does, `themes.py` already measures which
wordings return real German press coverage instead of trusting what an operator
typed, `rivals.py` maintains the comparison set that share of voice runs on, and
two model providers are wired and paid for.

Three properties make this different from an SEO ranking product, and the design
follows all three:

- **An answer is a sample, not a rank.** The same question asked twice returns
  different words. So a single measurement is never a verdict: every figure is
  shown against its own history, and a change counts as a change only when it
  survives two measurements.
- **The interesting questions never name the client.** "Was macht Enpal" is
  trivia. "Welche Anbieter für Solaranlagen mit Speicher gibt es" is the question
  a purchase starts with, and it is the one the mandate is either in or not. So
  the question set is banded by distance from the brand, and only the brand band
  may name it.
- **This measures the models we actually ask.** Not "die KI". Two named
  assistants, on a stated date, with the answer kept verbatim so a claim made to
  a client can be checked rather than trusted.

Posture unchanged: it measures and reports, it decides nothing, it optimises
nothing and it sends nothing.

## Summary

Today a client can ask "werden wir eigentlich genannt, wenn jemand ChatGPT nach
unserem Markt fragt", and the honest answer is that nobody here knows. The tool
counts articles; it has never once looked at what a machine answers.

After this build, every mandate carries a set of German questions a buyer would
actually ask, most of which never name the company. Those questions are put to
two assistants once a week, and the page shows how many of them named the
mandate, on which position, which competitors were named instead, and which
sources the models leaned on. Each answer is kept word for word with its date, so
a figure that goes into a client report can be opened and read rather than
believed. Movement is reported against the previous measurement, so a consultant
sees what changed rather than a number without a memory.

## Decisions

### DEC-1: Welche der Lücken bauen wir zuerst?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** the scope of this spec
- **A · KI-Sichtbarkeit** Die einzige ganze Kategorie, die vollständig fehlt, und die einzige, die ein Mandant von sich aus anspricht. Baut auf Profil, Wettbewerbern und zwei bereits angebundenen Modellen auf, braucht keine neue Abhängigkeit und verletzt die Kein-Scraping-Regel nicht.
- **B · Der Prüfstand für fremde Texte** Die zweimodellige Textprüfung existiert schon, aber nur für Texte, die das Tool selbst erzeugt hat. Eine Pressemitteilung, die der Mandant schickt, kann heute niemand einwerfen. Kleiner und billiger als A, aber eine Erweiterung statt einer neuen Fläche.
- **C · Eingehende Presseanfragen** Post läuft heute nur hinaus: das Tool liest Antworten auf eigene Briefe und sonst nichts. Eine Journalistenanfrage mit Deadline ist unsichtbar. Hoher Alltagswert, verlangt aber Zugriff auf ein Postfach statt auf einzelne Threads, was die bewusst enge Gmail-Anbindung aufmacht.
- **D · Der Verteiler lernt Passung** Die Empfängerliste ordnet heute nach drei Stufen, bewertet aber nicht, ob ein Journalist zu diesem einen Text passt, und schützt nicht davor, zwei Leute derselben Redaktion anzuschreiben. Klare Verbesserung, aber eine Verfeinerung von etwas, das funktioniert.

### DEC-2: Welche Assistenten werden gefragt?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** which providers a measurement asks, and what the page may claim
- **A · Die zwei, die schon angebunden sind** Claude über das Abo und Gemini über den vorhandenen Schlüssel. Keine neuen Zugänge, keine neuen Kosten, und die Seite sagt beide beim Namen statt von "der KI" zu sprechen. Deckt zwei der drei Assistenten ab, die in Deutschland tatsächlich benutzt werden.
- **B · Zusätzlich ChatGPT und Perplexity über eigene Schlüssel** Näher an dem, was ein Mandant meint. Kostet zwei weitere Zugänge und laufendes Geld pro Messung, und Perplexity sucht bei jeder Frage live, misst also etwas anderes als ein Modell, das aus dem Gedächtnis antwortet. Beides lässt sich später als weiterer Anbieter nachziehen, ohne die Tabellen zu ändern.

### DEC-3: Wie liest sich die Seite?  [mock]
**Status:** locked
**Chosen:** C
**Recommend:** C
**Locks as:** the layout the build matches
- **A · Tafel** Fragen als Zeilen, Modelle als Spalten, in jeder Zelle genannt oder nicht. Dicht und vergleichend, zeigt achtzehn Fragen auf einen Blick. Verlangt aber, dass man die Zahl selbst zusammenrechnet.
  `features/mocks/visibility-matrix.html`
- **B · Antworten im Wortlaut** Eine Karte je Frage mit dem echten Antworttext, der Nennung hervorgehoben und den Wettbewerbern daneben. Am nächsten an der Belegregel des Hauses, aber die Kennzahl, nach der der Mandant fragt, steht weit oben und klein.
  `features/mocks/visibility-answers.html`
- **C · Stand und Bewegung** Links wo das Mandat steht und wer den Markt besetzt, rechts was sich seit der letzten Messung verändert hat und was daraus folgt. Führt mit der Zahl, die im Mandantengespräch fällt, und begründet sie; der Wortlaut jeder Antwort bleibt einen Klick entfernt.
  `features/mocks/visibility-standing.html`

## Stories

### KIS-01: Der Fragensatz und die Messung
**Decisions:** DEC-2

Two things live here: the question set a mandate is measured on, and the pass that
puts it to the providers and reads the answers back.

The set is proposed, never imposed. `rivals.py` already set the rule and it holds
for the same reason: a wrong question silently changes a number the agency reports
to a client, so nothing is stored until a human accepts it. The proposal is built
from the mandate profile and the measured industry term, and it is banded by
distance from the brand, because a set full of questions naming the client
measures nothing at all.

Reading an answer is the other half, and it is deliberately unglamorous. Which
companies are named, in what order, which sources the model states. The competitor
list is intersected with the mandate's stored competitors so that an unrelated
firm counts as market rather than as a rival, and the answer is kept verbatim so
every figure on the page resolves to something a person can read.

**Acceptance:**
  - A proposed question carries one of four bands (`marke`, `auswahl`, `kategorie`, `problem`); a proposal without a recognised band is dropped rather than filed under a default.
  - Outside the `marke` band, a proposed question containing the client name or any stored alias is rejected at generation, because a question that names the client cannot measure whether the client is found.
  - Nothing is stored until accepted: a mandate with no accepted question has an empty set and is skipped by the measurement entirely, with no model call spent.
  - One measurement writes one row per (question, provider) carrying the answer verbatim, whether the client was named, its position among the companies named in that answer (1-based, null when not named), the competitors named, and the sources the model stated.
  - A company counts as named when its name or one of its stored aliases appears in the answer; position is the rank of its first appearance among all companies named there.
  - Sources are recorded only where the model states them, never derived from the text; a model citing nothing yields an empty list rather than a guess.
  - A provider that errors is recorded as a failed provider on that run, never as "not named": a missing answer and a negative answer are different facts and the stored rows must distinguish them.
  - At most one measurement per mandate per `NEWSPULSE_VISIBILITY_EVERY_DAYS` (default 7); a second request inside the window returns the stored run and spends no call. `NEWSPULSE_VISIBILITY=0` switches the whole feature off, and an accepted set is capped at 24 questions.

**Files:** `newspulse/src/newspulse/visibility.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0036_visibility.py` (new), `newspulse/src/newspulse/prompts/visibility_panel.txt` (new), `newspulse/src/newspulse/prompts/visibility_read.txt` (new), `newspulse/src/newspulse/config.py`, `newspulse/tests/test_visibility.py` (new), `newspulse/tests/test_brain.py`, `newspulse/tests/fixtures/prompts/` (new golden files for the two prompts)

**Changes:**
- Add `VisibilityQuestion` (client, text, band, accepted flag, created/accepted timestamps), `VisibilityRun` (client, run timestamp, providers asked, providers failed) and `VisibilityAnswer` (run, question, provider, answer text, named flag, position, rivals named, sources) to `models.py`, with the Alembic migration.
- Add `propose()` in `visibility.py`, building the banded question set from the profile and the measured industry term via `prompts/visibility_panel.txt`, dropping unbanded and client-naming proposals.
- Add `measure()`, asking every accepted question of every configured provider and reading each answer through `prompts/visibility_read.txt`, recording a provider failure as a failure rather than a negative.
- Add `NEWSPULSE_VISIBILITY` and `NEWSPULSE_VISIBILITY_EVERY_DAYS` to `config.py`, and register the two new prompts in the brain accounting test with their golden fixtures.

**Smoke:** `uv run pytest tests/test_visibility.py tests/test_brain.py` passes

### KIS-02: Die Seite, und die wöchentliche Messung im Lauf
**Depends on:** KIS-01
**Decisions:** DEC-3

The tab that makes the measurement readable, and the hook that keeps it current
without anybody remembering to press a button.

The page leads with the figure a consultant repeats in a client meeting, and then
earns it: who else occupies the market, what moved since last week, and which
sources the models are leaning on. Movement is the part a weekly measurement
exists for, so unchanged questions are counted rather than listed, and a mandate
with only one measurement so far is told plainly that there is nothing to compare
against yet instead of being shown a flat line.

The measurement rides the daily sweep for mandates that are due, and a failure
there is logged without marking the run degraded, exactly as the positioning
drafts already behave: a missed measurement is not a broken morning.

**Acceptance:**
  - A `KI-Sichtbarkeit` tab appears in the client tab strip at `/client/{id}/ki`, and the page matches the locked mock in DEC-3.
  - The page states the share of accepted questions that named the mandate, the ranking of companies named across the set with the mandate marked, and the stated sources with their counts.
  - Movement lists only questions whose result changed against the previous run, each naming what changed and on which provider; unchanged questions are counted, not listed.
  - A mandate whose set is empty shows the proposal flow instead of an empty chart: the proposed questions with an accept control each, and nothing stored until one is accepted.
  - A question a provider failed on renders as "nicht gemessen" and names the provider, never as "nicht genannt".
  - A mandate with exactly one measurement shows the standing and says in one line that there is nothing to compare against yet, rather than drawing a trend.
  - Benchmarks (`is_competitor`) get no page of their own; they appear only as named companies inside a mandate's ranking, the same exclusion the sidebar and the portfolio already apply.
  - The weekly measurement runs inside the daily sweep for mandates that are due, and a failure is logged without marking the sweep degraded; every visible string exists in German and English through `i18n.py`, none hard-coded in the template.

**Files:** `newspulse/src/newspulse/web/routes/visibility_view.py` (new), `newspulse/src/newspulse/web/templates/client_visibility.html` (new), `newspulse/src/newspulse/web/templates/_client_tabs.html`, `newspulse/src/newspulse/web/app.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/src/newspulse/job.py`, `newspulse/src/newspulse/web/static/app.css`, `newspulse/tests/test_visibility_view.py` (new)

**Changes:**
- Add `visibility_view.py` with the `GET /client/{id}/ki` page plus the accept and reject posts for a proposed question, and register the router in `web/app.py`.
- Add `client_visibility.html` rendering standing, market occupancy, sources, movement and the proposal state, following the locked mock.
- Add the tab to `_client_tabs.html` and the German and English strings to `i18n.py`.
- Call the due-mandate measurement from the sweep in `job.py`, folding a failure into the run row without degrading it.

**Smoke:** `uv run pytest tests/test_visibility_view.py` passes

## Deferred

Everything below is a real gap measured against the newsjack skill set. None of it
is in this build.

- **Prüfstand für fremde Texte**: the two-model check exists but only reaches assets the tool generated; a text the mandant sends in has no entry point. Not selected, see DEC-1 B.
- **Eingehende Presseanfragen**: mail is outbound plus thread-scoped reply reading; an unsolicited inquiry with a deadline is invisible. Not selected, see DEC-1 C, and it needs a wider Gmail scope than the current thread-only access.
- **Passungsprüfung je Journalist**: the recipient list orders by three tiers and never scores fit against this specific text. Not selected, see DEC-1 D.
- **Redaktionsdisziplin je Haus**: nothing ranks two reporters at one masthead or caps how many of them one story may reach. Same story as the item above.
- **Nachrichtenwert einer eigenen Ankündigung**: the tool judges incoming coverage and market openings, never "der Mandant will X verkünden, ist das eine Meldung". Wants its own intake and is a separate feature.
- **Krisenmodus**: statement, Q&A and talking points exist as formats, but a crisis category changes nothing about the tool's behaviour and there is no legal-review gate. Already deferred by the asset-formats spec; unchanged here.
- **Wer die Meldung zuerst hatte**: story clustering counts distinct outlets, and the lead copy is the best ranked, not the earliest. First-public time and story age are not computed anywhere, so nothing gates on freshness.
- **Gelernte Tonalität je Mandant**: there is a global house style and a per-client written guide, but nothing measured from the client's own copy. The guide covers most of the practical need.
- **Redaktionskalender für eigene Themen**: the market page carries a forward calendar for regulation and events, but there is no six-month plan of hooks this mandate could own. Closest existing surface, worth revisiting after this build.
- **Presseclipping als PDF**: reports export as Excel and as a self-contained HTML document with print styles; there is no per-article branded clip, and producing one needs a rendering dependency the project has so far avoided.
