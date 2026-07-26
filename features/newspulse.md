# NewsPulse: local German media monitor for a PR portfolio

prefix: NP

**Type:** feature
**Complexity:** 4
**Estimated Duration:** ~1 week
**Risk:** medium
**Scope:** newspulse
**Test Strategy:** unit tests on captured RSS fixtures (parsing, matching, dedup) and mocked `claude -p` output (analysis parse + retry); a deterministic end-to-end test drives the daily job over fixture feeds with a fake analyzer and asserts idempotency (a second run creates zero duplicates); the dashboard views render from a seeded SQLite fixture. The real integration test is the two-week live shadow run in the validation phase (see Deferred).

## Context

A PR agent tracks coverage for dozens of German client companies by hand across
Spiegel, Handelsblatt, FAZ and the rest. It is slow and easy to miss a crisis, a
regulatory move, or a competitor story. Commercial monitors (Meltwater, Cision,
Landau) are expensive per-seat SaaS with rigid client tiers.

NewsPulse is a local desktop tool that does this automatically. It keeps a client
database (imported from Excel), sweeps a curated set of German RSS feeds once a
day, matches items to clients, and lets Claude read each candidate to decide
whether it genuinely concerns the client, classify it, and score its importance.
The result is a dashboard the agent opens each morning that already knows what
happened to his clients overnight, with the noise filtered out. Everything runs on
his own machine, the archive is his to keep, and the analysis runs on his existing
Claude Code subscription (via `claude -p` headless mode) rather than metered API
billing.

This is a proof of concept. It ships Germany-only but stores country on the client
from day one, so adding a second country later is a config change, not a rewrite.
It lives in a new, self-contained `newspulse/` package alongside the existing
`meme_trader` code; the two share nothing.

Two legal and platform constraints shape the build and are non-negotiable
acceptance, not preferences:

- **No scraping of full article bodies.** German publishers enforce the
  Leistungsschutzrecht aggressively and most valuable content is paywalled.
  NewsPulse stores only what a feed already syndicates (headline, link, source,
  date, and the feed's own short summary) plus Claude's generated one-line
  summary. It never fetches or stores full paywalled body text.
- **Subscription access via `claude -p` only.** The analysis shells out to Claude
  Code headless mode, which is the supported, subscription-authorized path. The
  tool never extracts or spoofs an OAuth token to hit the API directly. An API
  path exists behind the same interface for the day this graduates to metered
  billing, but it is off by default.

## Summary

Today, tracking client coverage is fully manual: the agent opens a dozen German
news sites each morning, searches each client name by hand, and hopes nothing slipped
past overnight. There is no client database, no archive, and no way to tell a real
story about a client from a name collision without reading every hit himself.

After this build, he imports his client list from Excel once, and a scheduled job
runs every morning before he sits down. It sweeps 30 to 60 German RSS feeds, pulls
everything published since the last run, matches items to his clients by name and
alias, throws away duplicates (dpa wire copy republished across many outlets), and
has Claude read the survivors in batches to decide which are genuinely about the
client, summarize each in one sentence, tag a category (product, executive change,
crisis, regulatory, financial, competitor), and score importance. He opens one
local dashboard: today's coverage across all clients ranked by importance, with
alerts pinned at the top, each client clickable into a complete, searchable,
filterable archive that grows into a coverage record he owns rather than rents. It
runs on the Claude subscription he already pays for, and if a client name never
appears in the news that day, that client simply shows nothing rather than a wall
of false matches.

## Decisions

### DEC-1: How should the daily dashboard present the day's coverage?  [mock]
**Status:** locked
**Chosen:** C
**Recommend:** A
**Locks as:** the built Today view, matched against the chosen mock
- **A · Grouped by client** Alerts pinned in a banner up top, then coverage grouped under each client heading, ranked by importance within. Mirrors how a PR agent thinks (per mandate), and makes "nothing for this client today" obvious. Best when the mental model is client-first.
  `features/mocks/today-grouped.html`
- **B · Unified importance stream** One single feed ranked purely by importance across all clients, each row carrying a client chip, a category tag, and an alert flag, with quick category filters. Answers "what is the most important thing that happened anywhere in my portfolio today" in one scan.
  `features/mocks/today-stream.html`
- **C · Two-pane triage** A narrow left rail of alert cards (the must-see) beside a full ranked feed on the right. Separates "act now" from "good to know" so a busy morning starts with the three things that matter.
  `features/mocks/today-triage.html`

### DEC-2: How should the data live and how do schema changes happen?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** the storage + migration approach in the foundation story
- **A · SQLAlchemy models + Alembic migrations** The data is relational (clients to articles to analyses with foreign keys) and the schema is explicitly designed to grow (more countries, more fields). SQLAlchemy gives typed models and clean query-building for the dashboard's filters and search; Alembic makes every future schema change a versioned migration, which is the house rule in CLAUDE.md. Slightly more setup than raw SQLite, but it pays for itself the first time the schema changes.
- **B · Raw sqlite3 + a small migration runner** Matches the existing `meme_trader` style (a `SCHEMA` constant, plain `sqlite3`). Leanest possible for a POC, no ORM. But it means hand-writing query and filter logic for the dashboard and a hand-rolled versioned-migration mechanism to satisfy the "changes go through migrations" rule, which is most of what Alembic gives for free.

### DEC-3: How should the dashboard be delivered, a running web server or a regenerated static file?  [options]
**Status:** locked
**Chosen:** A
**Recommend:** A
**Locks as:** the presentation architecture across the Today, client, and settings views
- **A · FastAPI server-rendered app (Jinja + HTMX)** Filtering, search, and pagination run server-side against the full SQLite archive, so the client detail view stays fast as history grows to thousands of articles across years, routes are trivial to extend, and settings write straight back through the same app. Cost: the agent needs a local server process running (a port, a start step) rather than double-clicking a file.
- **B · Static `dashboard.html` regenerated each run (Jinja + vanilla JS)** Zero daemon, zero port, zero firewall prompt: the agent double-clicks one file each morning and search, sort, and filter run client-side in the browser. Best fit for a non-technical PR agent. Cost: the whole archive has to load into the page for client-side filtering, which degrades as history grows, there is no server-side pagination, and client CRUD/import needs a separate path since a static file cannot write back.

## Stories

### NP-01: Package scaffold, data model, and migrations
**Decisions:** DEC-2
**Files:** `newspulse/pyproject.toml` (new), `newspulse/README.md` (new), `newspulse/alembic.ini` (new), `newspulse/migrations/env.py` (new), `newspulse/migrations/versions/0001_initial.py` (new), `newspulse/src/newspulse/__init__.py` (new), `newspulse/src/newspulse/config.py` (new), `newspulse/src/newspulse/db.py` (new), `newspulse/src/newspulse/models.py` (new), `newspulse/tests/test_models.py` (new)
**Acceptance:**
  - `newspulse/` is a self-contained `uv`-managed project (its own `pyproject.toml`, src layout), independent of `meme_trader`; `uv sync` and `uv run alembic upgrade head` create the SQLite database from the initial migration
  - Models exist for `clients` (id, name, aliases[], industry, country, keywords[], alert_topics[], active, created_at), `articles` (id, title, url, source, published_at, fetched_at, summary_text, language) — one row per deduped feed story, NOT per client, `analyses` (id, article_id FK, client_id FK, summary, category, relevance_score, importance_score, is_alert, reasoning, analyzed_at) — one per (article, client) pair with a UNIQUE (article_id, client_id), so a single story about two portfolio companies is one article with two analyses, `runs` (id, started_at, finished_at, status, articles_found, errors), and `settings` (key, value)
  - `country` is a required column on `clients` defaulting to `DE`, so the schema supports future countries without a migration to add the column
  - `category` is a typed enum (StrEnum) with exactly: `produkt`, `personalie`, `krise`, `regulatorik`, `finanzen`, `wettbewerb`, `sonstiges`; `relevance_score` and `importance_score` are integers 0 to 10
  - `articles.url` has a UNIQUE index and there is an indexed `title_hash` column (normalized-title hash) for dedup; there is no full-body-text column, only `summary_text` for the feed-provided snippet (enforces the no-scrape rule at the schema level)
  - All schema changes go through Alembic migrations; no code hand-edits a live schema. A round-trip test creates a client with array fields (aliases, keywords, alert_topics) and reads them back intact
  - `config.py` loads all tunables (database path, default alert threshold, batch size, analyzer backend) from env vars with module-level named-constant defaults; no magic numbers inline

### NP-02: Client management: Excel/CSV import and CRUD service
**Depends on:** NP-01
**Files:** `newspulse/src/newspulse/clients.py` (new), `newspulse/tests/test_clients.py` (new), `newspulse/tests/fixtures/clients_sample.xlsx` (new)
**Acceptance:**
  - `import_clients(path, mapping)` reads an `.xlsx` or `.csv` (via pandas/openpyxl) and creates/updates clients; a `preview_import(path, mapping)` returns the parsed rows without committing, so the UI can show a preview before writing
  - Column mapping is explicit (source-column to client-field), not positional, so an arbitrary sheet layout imports without reordering; unmapped required fields (name) raise a clear validation error naming the row
  - `aliases`, `keywords`, and `alert_topics` parse from a delimited cell (comma or semicolon) into arrays; whitespace trimmed, blanks dropped
  - Re-importing the same sheet updates existing clients matched by name (case-insensitive, trimmed), never duplicates them
  - CRUD service functions cover create, update, deactivate (soft, sets `active=false`), and list; deactivating a client keeps its articles and analyses (the archive is permanent)
  - Tests cover a clean import, a re-import (no duplicates), a bad sheet (missing name column), and array-field parsing, against a committed fixture sheet

### NP-03: RSS ingestion and feed registry
**Depends on:** NP-01
**Files:** `newspulse/src/newspulse/ingest.py` (new), `newspulse/src/newspulse/feeds.py` (new), `newspulse/src/newspulse/feeds_default.toml` (new), `newspulse/tests/test_ingest.py` (new), `newspulse/tests/fixtures/feed_sample.xml` (new)
**Acceptance:**
  - A curated feed registry (30 to 60 German feeds: Spiegel, Zeit, FAZ, Handelsblatt, Süddeutsche, Welt, n-tv, Tagesschau, plus trade press) ships as an editable config file (`feeds_default.toml`); each entry has a name, url, and optional industry tag
  - `fetch_feed(url, since)` parses a feed (via `feedparser`) and returns items newer than `since`, each carrying title, link, source, published_at, the feed-provided summary/description, and language; it never issues a second request to fetch the article body
  - Only feed-provided fields are captured. Full article body text is never fetched or stored (Leistungsschutzrecht); a test asserts that ingestion makes no HTTP call beyond the feed URL itself
  - A single feed that is unreachable, malformed, or times out logs a WARNING and returns empty; it never aborts a multi-feed sweep
  - `published_at` is normalized to timezone-aware UTC; feeds with missing or unparseable dates fall back to `fetched_at` and log at DEBUG
  - Tests parse the committed fixture feed into the expected items and cover the timeout/malformed-feed isolation path with a mocked fetch

### NP-04: Client matching and deduplication
**Depends on:** NP-01, NP-03
**Files:** `newspulse/src/newspulse/matching.py` (new), `newspulse/tests/test_matching.py` (new)
**Acceptance:**
  - `match_candidates(items, clients)` returns candidate (item, client) pairs using a cheap pre-filter: case-insensitive, word-boundary matching of client name plus each alias plus each keyword against the item's title and feed summary; substring-inside-a-word matches (German compound nouns) do not count as hits
  - The pre-filter deliberately favors recall over precision, because the downstream Claude analysis is what actually disambiguates a real story from a name collision; a comment in the code states this explicitly so a later reader does not "tighten" the filter into missing real coverage
  - `deduplicate(items)` drops items already stored (by `url`) and collapses near-duplicates by normalized-title hash (lowercased, punctuation and whitespace stripped, source-suffix removed), so dpa wire copy republished across outlets is stored once; the retained copy is deterministic (earliest published_at, tie-broken by source name) so re-runs are stable
  - An item matching two clients yields two candidate pairs (the same story can be about two portfolio companies); it is stored as one `articles` row with two per-client `analyses`, never a duplicated article
  - Tests cover a compound-noun false-positive that the word-boundary rule rejects, an alias hit, a URL duplicate, and a near-duplicate title collapse

### NP-05: Analysis layer: pluggable analyzer with `claude -p` backend
**Depends on:** NP-01
**Files:** `newspulse/src/newspulse/analyzer.py` (new), `newspulse/src/newspulse/schemas.py` (new), `newspulse/src/newspulse/prompts/analysis.txt` (new), `newspulse/tests/test_analyzer.py` (new)
**Acceptance:**
  - An `Analyzer` Protocol defines `analyze(client, articles) -> list[Analysis]`. Two implementations exist behind it: `ClaudeCodeAnalyzer` (default, shells out to `claude -p "<prompt>" --output-format json`, subscription auth) and `ClaudeApiAnalyzer` (opt-in, metered API). The backend is chosen by one config value; default is the subscription backend
  - The subscription backend never extracts or reuses an on-disk OAuth token and never sets spoofed API headers; it invokes the `claude` CLI as a subprocess only (a test asserts the invocation shape and that no Anthropic API endpoint is contacted by this backend)
  - One batched prompt handles up to a configured number of articles per client (named constant, default 20) in a single call, passing the client profile (name, industry, aliases, alert_topics) and the candidate articles, and asking for one JSON object per article: `is_relevant` (bool), `summary` (one sentence), `category` (from the enum), `relevance_score` and `importance_score` (0 to 10), `is_alert` (bool), and `reasoning`
  - Output is validated with a Pydantic model; on a parse or schema failure the call retries once, and a second failure logs an ERROR and yields no analyses for that batch (the run continues) rather than raising
  - `is_alert` is true when the article matches one of the client's `alert_topics` or `importance_score` meets the configured alert threshold; the mapping is computed in code from the returned scores/topics, not trusted blindly from the model
  - Claude's `reasoning` is stored on every analysis so a later "why was this flagged" question has an answer and thresholds can be tuned
  - The subprocess call has a timeout and treats a non-zero exit as a batch failure (logged, run continues); tests mock the `claude -p` subprocess with canned JSON to cover the happy path, the retry-then-succeed path, and the give-up path

### NP-06: Daily job orchestrator
**Depends on:** NP-02, NP-04, NP-05
**Files:** `newspulse/src/newspulse/job.py` (new), `newspulse/src/newspulse/cli.py` (new), `newspulse/tests/test_job.py` (new)
**Acceptance:**
  - `newspulse run` (a CLI entry point) executes: load active clients, fetch every registered feed since the last successful run, match items to clients, deduplicate against stored articles, batch survivors per client through the analyzer, persist articles and analyses, and write a `runs` row
  - The job is idempotent: running it twice on the same day creates zero duplicate articles or analyses (guaranteed by the `articles.url` unique index plus the title-hash collapse, and the UNIQUE (article_id, client_id) on analyses). A deterministic end-to-end test runs the job twice over fixture feeds with a fake analyzer and asserts the second run adds nothing
  - The job is resumable and fault-isolated: a failing feed, a failing match, or a failing analysis batch is logged to the run's `errors` and skipped; one failure never aborts the whole run. The `runs` row records started_at, finished_at, status (`ok`/`partial`/`failed`), articles_found, and the error list
  - "Since the last run" uses the last successful run's timestamp; the very first run uses a configured lookback window (named constant) so day one is not unbounded
  - Everything is logged to a rotating log file (structured, via the stdlib logger, no bare prints in the package), because the failure mode this must survive is silently stopping in week three
  - The CLI supports `newspulse run` and `newspulse run --dry-run` (fetch, match, dedup, and report counts without calling the analyzer or writing analyses)

### NP-07: Web app and Today view
**Depends on:** NP-01
**Decisions:** DEC-1
**Files:** `newspulse/src/newspulse/web/app.py` (new), `newspulse/src/newspulse/web/routes/today.py` (new), `newspulse/src/newspulse/web/templates/base.html` (new), `newspulse/src/newspulse/web/templates/today.html` (new), `newspulse/src/newspulse/web/static/app.css` (new), `newspulse/tests/test_web_today.py` (new)
**Acceptance:**
  - A FastAPI app (server-rendered with Jinja + HTMX, no build step) serves a Today view at `/` matching the locked mock from DEC-1: today's coverage across all clients with alerts surfaced prominently, each item showing headline (linking out), source, time, the one-line summary, category tag, importance, and client
  - The layout (grouped-by-client vs unified stream vs two-pane triage) matches the specific mock chosen in DEC-1; the built page is checked against `features/mocks/today-*.html` for that option
  - "Today" is the current local day by default with a date picker to view any prior day; a day with no coverage renders a clean empty state, not an error
  - Items are ordered by importance_score descending; alerts (`is_alert`) are surfaced above non-alerts per the chosen layout
  - The header shows last-run status (time, feeds ok, articles checked) sourced from the latest `runs` row
  - A route test seeds articles and analyses and asserts the Today page renders the expected items in importance order with alerts surfaced

### NP-08: Client detail and history view
**Depends on:** NP-07
**Files:** `newspulse/src/newspulse/web/routes/client.py` (new), `newspulse/src/newspulse/web/templates/client_detail.html` (new), `newspulse/tests/test_web_client.py` (new)
**Acceptance:**
  - `/client/{id}` shows the client profile and the full historical archive of their articles, newest first, scrollable back to the tool's first run
  - Filters work and compose: date range, source, and category; plus a free-text search over headline and summary. Filtering is server-side (query params, HTMX-swapped list) so it works with the full archive, not just the loaded page
  - Each row shows the same fields as Today (headline out-link, source, date, summary, category, importance, alert flag)
  - An archive with hundreds of articles paginates (named-constant page size) rather than rendering everything at once
  - A route test seeds a multi-month archive for one client and asserts each filter (date, source, category, search) narrows the list correctly and that filters compose

### NP-09: Settings view: client CRUD, import, feeds, threshold, run status
**Depends on:** NP-02, NP-07
**Files:** `newspulse/src/newspulse/web/routes/settings.py` (new), `newspulse/src/newspulse/web/templates/settings.html` (new), `newspulse/tests/test_web_settings.py` (new)
**Acceptance:**
  - `/settings` provides client CRUD (add, edit, deactivate) through the NP-02 service, and an Excel/CSV import flow with the column-mapping step and a preview of parsed rows before commit
  - The alert threshold and the active feed list are viewable and editable and persist to the `settings` table; changing the threshold changes which future articles are flagged (it does not retroactively rewrite stored analyses)
  - Run history is shown: recent `runs` rows with status, articles_found, and any errors, so the agent can see at a glance whether this morning's run succeeded
  - The import preview surfaces validation errors from NP-02 (e.g. missing name column) inline, before anything is written
  - A route test covers adding a client, editing the alert threshold (persisted and reloaded), and an import preview that reports a validation error without committing

### NP-10: Scheduling and alert notifications
**Depends on:** NP-06
**Files:** `newspulse/src/newspulse/notify.py` (new), `newspulse/src/newspulse/schedule/com.newspulse.daily.plist` (new), `newspulse/src/newspulse/schedule/newspulse-daily.cmd` (new), `newspulse/docs/scheduling.md` (new), `newspulse/tests/test_notify.py` (new)
**Acceptance:**
  - After a run, if any alerts fired, a notification summarizing them (client, count, top headline) is delivered; the channel is config-selected between a desktop notification and email, defaulting to desktop, and defaulting to off if unconfigured
  - Email, when enabled, sends via SMTP from config (host, port, credentials from env, never hardcoded, never logged); no alerts means no notification (no empty "0 alerts" noise)
  - Ready-to-install scheduler artifacts ship for both platforms (a macOS launchd plist invoking `newspulse run` once daily, and a Windows Task Scheduler command script), with `docs/scheduling.md` giving the exact install command for each using absolute paths
  - Notification failure never fails the run: a broken SMTP config logs an ERROR after the data is already safely persisted, it does not roll back the run
  - Tests cover alert-summary formatting from a run's fired alerts and the no-alerts-no-send path, with the delivery channel mocked

## Custom Notes

- **Reuse check (per CLAUDE.md):** NewsPulse shares nothing with `meme_trader`
  (Solana forensics) beyond living in the same git repo. It is a separate
  `uv`-managed package with its own `pyproject.toml`, database, and dependencies.
  No `meme_trader` module is imported and no `meme_trader` constant is reused,
  because none of the domains overlap. New dependencies proposed here:
  `sqlalchemy` + `alembic` (storage/migrations, per DEC-2 option A), `feedparser`
  (RSS), `fastapi` + `jinja2` + `python-multipart` (web), `pandas` + `openpyxl`
  (Excel import), `pydantic` (analysis schema). All are lightweight and
  well-maintained; propose-before-adopt is satisfied by listing them here.
- **The `claude -p` contract:** the subscription analyzer is a subprocess call to
  the user's installed `claude` CLI, so the machine running the daily job must be
  logged in to Claude Code. This is a documented, supported path and the whole
  reason the tool runs on a subscription rather than metered billing. Batching (up
  to 20 articles per call) keeps daily usage well inside interactive territory.
- **Precision/recall is the whole product.** The relevance filter is what makes or
  breaks trust: too many false alerts and he stops reading it, too quiet and he
  stops believing it. The two-week validation phase (Deferred) exists to tune the
  alert threshold and the matching rules against real client data; storing Claude's
  `reasoning` on every analysis is what makes that tuning possible.
- **Known POC limitations, stated up front:** RSS-only coverage misses paywalled
  and regional print that a commercial monitor catches; no social, broadcast, or
  podcast monitoring; it only runs when the machine is on (a missed day is a gap
  unless catch-up is added later); German compound-noun and common-noun company
  names will generate false positives until the filter is tuned; single-user,
  single-machine, no sharing.

## Deferred

- **Two-week live validation and threshold tuning.** Run NP against the real
  client list for two weeks, measure precision/recall on the relevance filter, and
  tune the alert threshold and matching rules. This is a run-and-measure protocol,
  not a build story, but it is the phase that actually proves the product.
- **Commercial news API** (NewsAPI.ai, GDELT, or a German-focused provider) as the
  honest coverage upgrade once the concept is proven. Slots in behind the same
  ingestion seam as a new source.
- **Metered API backend as the default.** `ClaudeApiAnalyzer` already exists behind
  the Protocol (NP-05); graduating to it is a one-config-value flip if the tool
  outgrows subscription usage.
- **Second country / language** (schema already carries `country`), weekly digest
  exports for client reporting, sentiment trending over time, competitor tracking
  as first-class entities, catch-up logic for missed days, and a hosted multi-user
  version.
