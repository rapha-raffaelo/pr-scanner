"""The daily job orchestrator.

:func:`run` executes one daily sweep end to end: load the active clients, fetch
every registered feed since the last successful run, match items to clients,
deduplicate against the stored archive, batch the survivors per client through the
analyzer, persist articles and analyses, and write a ``runs`` row recording the
outcome.

Three properties are load-bearing, and each has a test:

* **Idempotent.** Running twice on the same day adds zero duplicate articles or
  analyses. Re-fetched items are dropped by :func:`~newspulse.matching.deduplicate`
  (seeded with the stored URLs and title hashes), and the DB's UNIQUE(articles.url)
  plus UNIQUE(article_id, client_id) are the backstop. A second run over the same
  feeds stores nothing new.

* **Fault-isolated / resumable.** A failing feed, a failing match, or a failing
  analysis batch is logged to the run's ``errors`` and skipped — one failure never
  aborts the whole sweep. Each client's analyses commit in their own transaction,
  so a later client's failure can never roll back an earlier client's stored work,
  and the articles are committed before any analysis runs.

* **Bounded first run.** "Since the last run" is the last OK run's start time; the
  very first run (no prior OK run) looks back a fixed window (:data:`_FIRST_RUN_LOOKBACK`)
  so day one is not an unbounded backfill.

Nothing here writes to stdout: the whole package logs through the stdlib logger to
a rotating file (:func:`setup_logging`), because the failure mode this job must
survive is silently stopping in week three. The CLI (:mod:`newspulse.cli`) owns all
user-facing terminal output.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .analyzer import Analyzer, get_analyzer
from .clients import list_clients
from .feeds import Feed, load_feeds
from .ingest import FeedItem, fetch_feed
from .matching import Candidate, deduplicate, match_candidates, title_hash
from .models import Analysis, Article, Client, Run, RunStatus
from .schemas import Analysis as AnalysisSchema

_log = logging.getLogger(__name__)

# --- Named constants (the "why" lives next to each) ----------------------------

# The very first run (no prior successful run to bound "since") backfills at most
# this far. Without it, day one would fetch every item every feed still lists with
# no lower bound; a week keeps day one to recent coverage without an unbounded pull.
_FIRST_RUN_LOOKBACK = dt.timedelta(days=7)

# Rotating-log knobs. The whole point of the file is week-three survivability, so
# it must never grow without bound or silently truncate: rotate at 5 MB and keep 5
# generations (~30 MB of history) — plenty to see what a job did last night, tiny
# on disk.
_LOG_FILENAME = "newspulse.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_LOG_LEVEL = logging.INFO

# Structured line format: timestamp, level, and the emitting module logger name, so
# a line in the file always says which stage (ingest / matching / analyzer / job)
# produced it. Fielded rather than JSON — greppable and readable for a single-user
# local tool, without pulling in a logging framework.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# The package-root logger. Handlers attached here catch every ``newspulse.*`` module
# logger by propagation, so one setup call routes ingest, matching, and analyzer
# diagnostics into the same rotating file.
_PACKAGE_LOGGER = "newspulse"


# A callable shaped like :func:`newspulse.ingest.fetch_feed`. Injected so tests can
# drive the job over fixture feeds (and exercise a raising feed) without any network.
FetchFeed = Callable[..., list[FeedItem]]


@dataclass(frozen=True, slots=True)
class RunReport:
    """The at-a-glance outcome of a single :func:`run`, returned to the CLI.

    Distinct from the persisted ``runs`` row: this also carries the per-stage
    counts a human wants at the terminal (and that ``--dry-run`` reports without
    writing anything). ``new_articles`` is what the ``runs`` row stores as
    ``articles_found`` — the distinct new stories this run added to the archive.
    """

    status: RunStatus
    feeds_total: int
    feeds_ok: int
    items_fetched: int
    candidates: int
    new_articles: int
    analyses_written: int
    errors: list[str]
    dry_run: bool


def _utcnow() -> dt.datetime:
    """Timezone-aware UTC now. Wrapped so tests can inject a fixed clock."""
    return dt.datetime.now(dt.UTC)


def setup_logging(log_path: Path | None = None) -> Path:
    """Install a rotating file handler on the package-root logger; return its path.

    Idempotent: a second call with the same path does not stack another handler, so
    a long-lived process (or a test) can call it freely. The parent directory is
    created if missing so a first run never fails just because the log directory
    doesn't exist yet. Paths are compared via ``os.path.abspath`` — the same
    normalization ``RotatingFileHandler`` applies to ``baseFilename`` — so the
    idempotency check matches regardless of symlinks in the temp/DB directory.
    """
    default = config.DATABASE_PATH.parent / _LOG_FILENAME
    path = Path(os.path.abspath(str(log_path or default)))
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(_PACKAGE_LOGGER)
    logger.setLevel(_LOG_LEVEL)
    already_attached = any(
        isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path
        for handler in logger.handlers
    )
    if not already_attached:
        handler = RotatingFileHandler(
            path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
    return path


# --- "Since the last run" ------------------------------------------------------


def _determine_since(session: Session, started: dt.datetime) -> dt.datetime:
    """The lower bound for this sweep: the last OK run's start, else the lookback.

    Only a run whose status is exactly ``ok`` advances the watermark. A ``partial``
    or ``failed`` run left the window incompletely covered, so the next run re-covers
    it from the earlier watermark rather than trusting a degraded sweep — dedup makes
    re-fetching free, so this only ever costs re-processing, never missed coverage.
    """
    last_ok = session.execute(
        select(Run)
        .where(Run.status == RunStatus.OK)
        .order_by(Run.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last_ok is not None:
        return last_ok.started_at
    return started - _FIRST_RUN_LOOKBACK


def _load_known(session: Session) -> tuple[set[str], set[str]]:
    """Stored URLs and title hashes, used to seed dedup against the archive."""
    rows = session.execute(select(Article.url, Article.title_hash)).all()
    return {row.url for row in rows}, {row.title_hash for row in rows}


# --- Fetch / match / dedup (the read-only pipeline) ----------------------------


def _fetch_all(
    feeds: Sequence[Feed],
    since: dt.datetime,
    fetch: FetchFeed,
    fetched_at: dt.datetime,
    errors: list[str],
) -> tuple[list[FeedItem], int]:
    """Fetch every feed, isolating a failure to its own feed.

    ``fetch_feed`` already self-isolates network/parse errors to an empty list; this
    wrapper catches anything it does raise (or an injected/unexpected error) so one
    bad feed is logged to the run's errors and skipped, never aborting the sweep.
    Returns the combined items and the count of feeds that fetched without error.
    """
    items: list[FeedItem] = []
    feeds_ok = 0
    for feed in feeds:
        try:
            batch = fetch(feed.url, since, source=feed.name, fetched_at=fetched_at)
        except Exception as exc:  # noqa: BLE001 — per-feed fault-isolation boundary
            _log.warning("feed %r failed to fetch: %s; skipping", feed.name, exc)
            errors.append(f"feed {feed.name!r}: {exc}")
            continue
        feeds_ok += 1
        items.extend(batch)
    return items, feeds_ok


def _match(
    items: Sequence[FeedItem], clients: Sequence[Client], errors: list[str]
) -> list[Candidate]:
    """Run the recall pre-filter, isolating a match failure to zero candidates.

    Matching is a pure, deterministic function, so this boundary is defensive — but
    the AC requires a failing match to be logged and skipped rather than aborting the
    run, and this keeps that promise if the matcher ever does raise.
    """
    try:
        return match_candidates(items, clients)
    except Exception as exc:  # noqa: BLE001 — matching fault-isolation boundary
        _log.warning("matching failed: %s; skipping match for this run", exc)
        errors.append(f"match: {exc}")
        return []


def _distinct_items(candidates: Sequence[Candidate]) -> list[FeedItem]:
    """The distinct items among the candidate pairs, order preserved.

    One item that mentions two clients appears in two candidates as the *same* object;
    dedup by identity collapses those to one item for storage while leaving the two
    (item, client) pairings intact for analysis.
    """
    seen: set[int] = set()
    out: list[FeedItem] = []
    for candidate in candidates:
        if id(candidate.item) in seen:
            continue
        seen.add(id(candidate.item))
        out.append(candidate.item)
    return out


# --- Persistence ---------------------------------------------------------------


def _to_article(item: FeedItem, fetched_at: dt.datetime) -> Article:
    """Map a feed item to an ``articles`` row (feed-provided fields only).

    ``title_hash`` is computed the same way dedup collapses near-duplicates, so a
    stored hash can seed a later run's ``known_title_hashes`` and recognise the same
    wire story. Only ``summary_text`` (the feed snippet) is stored — no article body.
    """
    return Article(
        title=item.title,
        url=item.link,
        source=item.source,
        published_at=item.published_at,
        fetched_at=fetched_at,
        summary_text=item.summary,
        language=item.language,
        title_hash=title_hash(item.title, item.source),
    )


def _persist_articles(
    session: Session, kept_items: Sequence[FeedItem], fetched_at: dt.datetime
) -> list[Article]:
    """Insert the kept items as articles and commit, assigning their ids.

    Committing before analysis makes the stored stories durable even if a later
    analysis batch fails, and it is what gives each article the id the analyzer
    stamps onto its analyses.
    """
    articles = [_to_article(item, fetched_at) for item in kept_items]
    session.add_all(articles)
    session.commit()
    return articles


def _group_by_client(
    candidates: Sequence[Candidate], article_by_url: dict[str, Article]
) -> list[tuple[Client, list[Article]]]:
    """Resolve surviving candidate pairs into per-client persisted-article lists.

    A candidate whose item was dropped by dedup — already stored from an earlier run,
    or collapsed into a kept near-duplicate — has no entry in ``article_by_url`` and
    is skipped. This is exactly what makes a re-run add nothing: on the second run
    every re-fetched item is already stored, so every candidate resolves to ``None``.
    A client that matches the same article twice is deduped to a single pair.
    """
    grouped: dict[int, list[Article]] = {}
    clients_by_id: dict[int, Client] = {}
    seen_pairs: set[tuple[int, int]] = set()
    for item, client in candidates:
        article = article_by_url.get(item.link)
        if article is None:
            continue
        pair = (article.id, client.id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        grouped.setdefault(client.id, []).append(article)
        clients_by_id[client.id] = client
    return [(clients_by_id[cid], articles) for cid, articles in grouped.items()]


def _to_orm_analysis(verdict: AnalysisSchema) -> Analysis:
    """Map an analyzer verdict onto an ``analyses`` row. ``is_alert`` is taken as the
    analyzer already computed it in code (never re-derived here)."""
    return Analysis(
        article_id=verdict.article_id,
        client_id=verdict.client_id,
        is_relevant=verdict.is_relevant,
        summary=verdict.summary,
        category=verdict.category,
        relevance_score=verdict.relevance_score,
        importance_score=verdict.importance_score,
        is_alert=verdict.is_alert,
        reasoning=verdict.reasoning,
    )


def _analyze_and_persist(
    session: Session,
    client: Client,
    articles: Sequence[Article],
    analyzer: Analyzer,
    errors: list[str],
) -> int:
    """Analyze one client's articles and commit its analyses in their own transaction.

    Isolated per client: a failure (the analyzer raising, or a persist error) rolls
    back only this client's uncommitted analyses, is logged to the run's errors, and
    lets the sweep continue with the next client. Returns how many analyses were
    written.
    """
    name = getattr(client, "name", "?")
    try:
        analyses = analyzer.analyze(client, articles)
        for verdict in analyses:
            session.add(_to_orm_analysis(verdict))
        session.commit()
        return len(analyses)
    except Exception as exc:  # noqa: BLE001 — per-client fault-isolation boundary
        session.rollback()
        _log.error(
            "analysis for client %r failed: %s; skipping, run continues", name, exc
        )
        errors.append(f"analysis {name!r}: {exc}")
        return 0


# --- Run-row bookkeeping -------------------------------------------------------


def _final_status(errors: Sequence[str]) -> RunStatus:
    """``ok`` when the sweep was clean, ``partial`` when some piece was isolated."""
    return RunStatus.PARTIAL if errors else RunStatus.OK


def _finalize_run(
    session: Session,
    started: dt.datetime,
    finished: dt.datetime,
    status: RunStatus,
    articles_found: int,
    errors: Sequence[str],
) -> None:
    """Write the ``runs`` row for this sweep in a clean transaction.

    The leading rollback clears any half-open transaction left by a mid-sweep crash
    (a no-op on the happy path, where every prior stage already committed) so the run
    record itself always persists — the one row that must exist even when the sweep
    failed.
    """
    session.rollback()
    session.add(
        Run(
            started_at=started,
            finished_at=finished,
            status=status,
            articles_found=articles_found,
            errors=list(errors),
        )
    )
    session.commit()


# --- Orchestration -------------------------------------------------------------


def run(
    session: Session,
    *,
    analyzer: Analyzer | None = None,
    feeds: Sequence[Feed] | None = None,
    fetch: FetchFeed = fetch_feed,
    now: Callable[[], dt.datetime] | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Execute one daily sweep and return its report.

    ``analyzer``/``feeds``/``fetch``/``now`` are injectable so the sweep can run over
    fixture feeds with a fake analyzer and a fixed clock; the defaults wire up the
    real registry, the subscription analyzer, and the live fetch. ``dry_run`` fetches,
    matches, and deduplicates and reports the counts without calling the analyzer or
    writing anything (no articles, no analyses, no ``runs`` row).
    """
    now_fn = now or _utcnow
    started = now_fn()
    errors: list[str] = []
    resolved_feeds = list(load_feeds() if feeds is None else feeds)
    clients = list_clients(session)
    since = _determine_since(session, started)
    _log.info(
        "run start: %d active client(s), %d feed(s), since=%s%s",
        len(clients),
        len(resolved_feeds),
        since.isoformat(),
        " [dry-run]" if dry_run else "",
    )
    if dry_run:
        return _run_dry(session, resolved_feeds, clients, since, fetch, started, errors)
    return _run_real(
        session, resolved_feeds, clients, since, fetch, now_fn, started, errors, analyzer
    )


def _run_dry(
    session: Session,
    feeds: Sequence[Feed],
    clients: Sequence[Client],
    since: dt.datetime,
    fetch: FetchFeed,
    started: dt.datetime,
    errors: list[str],
) -> RunReport:
    """The read-only preview: fetch, match, dedup, count. Writes nothing."""
    items, feeds_ok = _fetch_all(feeds, since, fetch, started, errors)
    candidates = _match(items, clients, errors)
    known_urls, known_hashes = _load_known(session)
    kept = deduplicate(
        _distinct_items(candidates),
        known_urls=known_urls,
        known_title_hashes=known_hashes,
    )
    _log.info(
        "dry-run: %d item(s) fetched, %d candidate(s), %d new article(s) — no writes",
        len(items),
        len(candidates),
        len(kept),
    )
    return RunReport(
        status=_final_status(errors),
        feeds_total=len(feeds),
        feeds_ok=feeds_ok,
        items_fetched=len(items),
        candidates=len(candidates),
        new_articles=len(kept),
        analyses_written=0,
        errors=list(errors),
        dry_run=True,
    )


def _run_real(
    session: Session,
    feeds: Sequence[Feed],
    clients: Sequence[Client],
    since: dt.datetime,
    fetch: FetchFeed,
    now_fn: Callable[[], dt.datetime],
    started: dt.datetime,
    errors: list[str],
    analyzer: Analyzer | None,
) -> RunReport:
    """The persisting sweep, wrapped so any crash still records a ``failed`` run."""
    new_articles = 0
    analyses_written = 0
    feeds_ok = items_count = candidates_count = 0
    status = RunStatus.OK
    try:
        items, feeds_ok = _fetch_all(feeds, since, fetch, started, errors)
        items_count = len(items)
        candidates = _match(items, clients, errors)
        candidates_count = len(candidates)
        known_urls, known_hashes = _load_known(session)
        kept = deduplicate(
            _distinct_items(candidates),
            known_urls=known_urls,
            known_title_hashes=known_hashes,
        )
        articles = _persist_articles(session, kept, started)
        new_articles = len(articles)
        article_by_url = {article.url: article for article in articles}
        resolved_analyzer = analyzer or get_analyzer()
        for client, client_articles in _group_by_client(candidates, article_by_url):
            analyses_written += _analyze_and_persist(
                session, client, client_articles, resolved_analyzer, errors
            )
        status = _final_status(errors)
    except Exception as exc:  # noqa: BLE001 — top-level so a crash still records a run
        _log.exception("run aborted before completion: %s", exc)
        errors.append(f"run aborted: {exc}")
        status = RunStatus.FAILED
        session.rollback()
    _finalize_run(session, started, now_fn(), status, new_articles, errors)
    _log.info(
        "run done: status=%s, %d new article(s), %d analysis(es), %d error(s)",
        status.value,
        new_articles,
        analyses_written,
        len(errors),
    )
    return RunReport(
        status=status,
        feeds_total=len(feeds),
        feeds_ok=feeds_ok,
        items_fetched=items_count,
        candidates=candidates_count,
        new_articles=new_articles,
        analyses_written=analyses_written,
        errors=list(errors),
        dry_run=False,
    )


__all__ = ["RunReport", "run", "setup_logging"]
