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
| `NEWSPULSE_BATCH_SIZE`      | `20`           | max articles per analyzer batch call     |
| `NEWSPULSE_ANALYZER_BACKEND`| `claude_code`  | analyzer backend (`claude_code` or `claude_api`) |
| `NEWSPULSE_WEB_HOST`        | `127.0.0.1`    | dashboard bind address                   |
| `NEWSPULSE_WEB_PORT`        | `8000`         | dashboard port                           |

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
