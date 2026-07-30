# NewsPulse

A local German media monitor for a PR client portfolio. NewsPulse keeps a client
database, sweeps a curated set of German RSS feeds once a day, matches items to
clients, and lets Claude decide whether each story genuinely concerns a client,
classify it, and score its importance. Everything runs on the local machine; the
archive is yours to keep.

This is a self-contained `uv`-managed package that shares nothing with the
`meme_trader` code that lives alongside it in the same repo.

## Constraints (non-negotiable)

- **No scraping of full article bodies.** Only what a feed already syndicates
  (headline, link, source, date, and the feed's own short summary) plus Claude's
  generated one-line summary is stored. The schema has no full-body-text column.
- **Subscription access via `claude -p` only.** Analysis shells out to Claude Code
  headless mode; the tool never spoofs an OAuth token to hit the API directly.

## Setup

NewsPulse requires Python 3.11+ (it uses `enum.StrEnum`) and [uv](https://docs.astral.sh/uv/).

```sh
cd newspulse
uv sync                          # create the virtualenv and install dependencies
uv run alembic upgrade head      # create the SQLite database from migrations
```

`uv run alembic upgrade head` creates the database at the path configured by
`NEWSPULSE_DATABASE_PATH` (default: `newspulse.db` in the current directory).

## Run the dashboard

The Today view is served by a small FastAPI app (server-rendered Jinja + HTMX,
no build step). Start it with either:

```sh
uv run newspulse-web            # console script
uv run python -m newspulse.web  # equivalent module form
```

Then open <http://127.0.0.1:8000/>. The bind address defaults to loopback
because this is a single-user local tool; override with `NEWSPULSE_WEB_HOST` /
`NEWSPULSE_WEB_PORT` if needed.

## Configuration

All tunables load from environment variables with sane defaults (see
`src/newspulse/config.py`):

| Variable                    | Default        | Meaning                                  |
| --------------------------- | -------------- | ---------------------------------------- |
| `NEWSPULSE_DATABASE_PATH`   | `newspulse.db` | SQLite database file path                |
| `NEWSPULSE_ALERT_THRESHOLD` | `7`            | importance score at/above which an article auto-flags |
| `NEWSPULSE_BATCH_SIZE`      | `10`           | max articles per analyzer batch call     |
| `NEWSPULSE_ANALYZER_BACKEND`| `claude_code`  | analyzer backend (`claude_code` or `claude_api`) |
| `NEWSPULSE_ANALYZER_TIMEOUT`| `180`          | seconds before one `claude -p` batch call is abandoned |
| `NEWSPULSE_WEB_HOST`        | `127.0.0.1`    | dashboard bind address                   |
| `NEWSPULSE_WEB_PORT`        | `8000`         | dashboard port                           |
| `NEWSPULSE_GOOGLE_NEWS`     | `1`            | also run one Google News search per client (see below) |
| `NEWSPULSE_TIMEZONE`        | `Europe/Berlin`| zone every displayed time is shown in, and where a day starts |

## Sources

Two kinds of source feed the sweep:

**The curated registry** (`src/newspulse/feeds_default.toml`) — national outlets,
public broadcasters, regional dailies, trade press, and Presseportal (the press
release wire operated by news aktuell, a dpa subsidiary). Editable without
touching code. Feed URLs rot silently, so check them periodically:

```sh
uv run newspulse check-feeds        # exits non-zero if any feed is unreachable
```

**Per-client Google News searches** — one RSS search per active client, built
from its name and aliases. The registry is client-agnostic and can only find
coverage in the outlets it lists; the searches reach the regional and trade press
that no fixed list covers. In practice this is where most coverage comes from —
a measured 3-day sweep found 55 articles from the registry alone and 246 with
searches enabled.

Three properties are worth knowing:

- Links are `news.google.com` redirects, not publisher URLs, so a story arriving
  from both routes is collapsed by the **normalized-title** hash rather than by
  URL.
- Each result is credited to its real publisher (from the RSS `<source>`
  element), never to "Google News".
- Google supplies no real summary — only the headline wrapped in a link — so
  those items are stored with no summary rather than a duplicate of the title.

Set `NEWSPULSE_GOOGLE_NEWS=0` to run on the curated registry alone.

## Backfill

A normal run covers everything since the last successful run. To catch up a
wider window (after a break, or on first use):

```sh
uv run newspulse run --since-days 30
```

The dashboard offers the same under **Einstellungen → Lauf starten**. Backfill
only widens which *fetched* items are accepted — RSS carries no "everything
since X" request, so a feed still returns only what it currently syndicates
(often days, not weeks). Re-running is free: dedup drops anything already stored.

## Working with coverage

**Triage.** Each item carries a state — *gelesen*, *für Mandant*, *erledigt* —
set with one click on the Heute view. State is per (article, client), so a story
can be handled for one mandate and still open for another. `erledigt` is not a
delete: the archive and every report still count it.

**Stories, not articles.** Coverage of one event is grouped, and a syndicated
story shows how far it travelled (*"3× aufgegriffen"*) with the outlet list.
Deduplication is deliberately *not* made stricter to achieve this — the stored
copies are what make a pickup count possible at all.

**Bylines.** The author is captured when a feed supplies one (heise and
Netzpolitik do; Spiegel and Tagesspiegel do not), so repeat coverage by the same
journalist is visible. It appears in the feed, the archive, and the Excel export.

**Competitors.** Toggle any client to *Wettbewerber* under Einstellungen. It is
then monitored identically but excluded from the digest, and appears in the
Share-of-Voice table on Mandanten. Share is over the monitored set only — it
answers "of the coverage we watch, how much was ours".

## Reports and notifications

```sh
uv run newspulse digest --print      # today's brief, printed
uv run newspulse digest              # …emailed (needs SMTP configured)
```

The digest goes out whether or not an alert fired, so its *absence* means the run
failed. Alerts remain a separate, urgency-driven notification.

An Excel report per client — coverage plus per-category and per-publisher
summaries — is at **Mandanten → Excel**, or `/client/{id}/export.xlsx?days=30`.

## Empfehlungen (suggested PR actions)

**Mandanten → Empfehlungen** generates a brief from a client's recent coverage:
a read of the situation plus concrete actions, each citing the stories behind it.

It is **advisory and explicitly not autonomous** — nothing is sent, scheduled, or
acted on. Generation is an explicit button, never a side effect of the daily run,
and the result is stored so the brief that was current during a crisis stays on
the record. The prompt permits an empty answer: a tool that invents busywork on a
quiet week trains its user to ignore it.

It reasons over headlines and short summaries only (the no-scrape rule), so it is
directional — not a substitute for reading the coverage.

## Data model

- `clients` — the tracked portfolio companies (name, aliases, industry, country,
  keywords, alert topics, active flag). `country` defaults to `DE`.
- `articles` — one row per deduped feed story (title, url, source, dates,
  feed summary, language, normalized-title hash for dedup). Not per client.
- `analyses` — one row per `(article, client)` pair (summary, category, scores,
  alert flag, reasoning). A single story about two portfolio companies is one
  article with two analyses.
- `runs` — one row per daily sweep (timings, status, articles found, errors).
- `settings` — key/value app settings.

All schema changes go through Alembic migrations; no code hand-edits a live schema.

## Tests

```sh
uv run pytest
```
