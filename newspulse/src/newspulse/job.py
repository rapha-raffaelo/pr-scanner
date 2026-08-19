"""The daily job orchestrator.

:func:`run` executes one daily sweep end to end: load the active clients, fetch
every registered feed since the last successful run, match items to clients,
deduplicate against the stored archive, batch the survivors per client through the
analyzer, persist articles and analyses, and write a ``runs`` row recording the
outcome.

Three properties are load-bearing, and each has a test:

* **Idempotent.** Running twice on the same day adds zero duplicate articles or
  analyses. Re-fetched items are dropped by :func:`~newspulse.matching.deduplicate`
  (seeded with the stored URLs and title hashes) so no article is stored twice, and
  every candidate pair is resolved to its stored article and analysed only if it has
  no Analysis yet — so the DB's UNIQUE(articles.url) and UNIQUE(article_id, client_id)
  are genuine backstops, not dead weight. A second run over the same feeds stores
  nothing new.

* **Fault-isolated / resumable / self-healing.** A failing feed, a failing match, or
  a failing analysis batch is logged to the run's ``errors`` and skipped — one failure
  never aborts the whole sweep. Each client's analyses commit in their own transaction,
  so a later client's failure can never roll back an earlier client's stored work, and
  the articles are committed before any analysis runs. Crucially, a story left stored
  but un-analysed by a transient outage is *not* lost: because analysis targets are
  resolved from the stored archive (not just this run's fresh inserts), a later run
  re-analyses it — whether it is still in the feed (resolved from the re-fetched
  candidate) or has since dropped out (recovered by the bounded backfill re-match).

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
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import angles, config, gnews, mailsync, notify, themes
from .analyzer import Analyzer, get_analyzer
from .clients import list_clients
from .feeds import Feed, load_feeds
from .ingest import FeedItem, fetch_feed
from .matching import (
    Candidate,
    dedup_title_hash,
    deduplicate,
    match_candidates,
    mentions_client,
    name_matcher,
    on_theme,
    radar_matcher,
    terms_matcher,
    theme_matcher,
    title_hash,
)
from .models import (
    Analysis,
    Article,
    Client,
    Run,
    RunStatus,
    TopicHit,
    visible_coverage,
)
from .schemas import Analysis as AnalysisSchema

_log = logging.getLogger(__name__)

# --- Named constants (the "why" lives next to each) ----------------------------

# The very first run (no prior successful run to bound "since") backfills at most
# this far. Without it, day one would fetch every item every feed still lists with
# no lower bound; a week keeps day one to recent coverage without an unbounded pull.
_FIRST_RUN_LOOKBACK = dt.timedelta(days=7)

# How far back the self-healing backfill re-examines stored articles for a missing
# analysis. A transient analyzer outage leaves an article stored but un-analysed; the
# next run re-matches every article fetched within this window and analyses any
# (article, client) pair still lacking a verdict, so coverage self-heals even after
# the story has dropped out of its feed. Bounded (a week) so the pass stays O(recent),
# never O(whole archive), as the history grows to thousands of articles (DEC-3).
_BACKFILL_WINDOW = dt.timedelta(days=7)

# Below this, a field-scoped radar has not really answered. The fallback used to
# fire only on *nothing* usable, and one item was enough to suppress it — measured
# on a cannabis wholesaler whose scoped query returned exactly one German article
# while the same themes unscoped returned twenty. The field clause is an AND
# against an industry label, and a label written in English ("Pharmaceuticals")
# excludes German coverage almost completely; one survivor is noise, not a radar.
#
# Widening is also cheaper in consequences than it was when the "only if empty"
# rule was written: the widened batch now has to carry one of the mandate's own
# themes in its syndicated text (:func:`_on_theme`) before anything is recorded.
_MIN_RADAR_ITEMS = 3

# How many mandates may be given a radar in one sweep. Settling costs a model call
# and up to sixteen live searches, all of it inside the run guard — a portfolio of
# ten themeless mandates would hold that guard for over an hour on the first
# morning, and every page would show "a sweep is running" throughout. Three a
# night clears such a portfolio inside a week without anyone noticing the cost.
_SETTLE_PER_SWEEP = 3

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
    # Positioning drafts the topic radar produced (see newspulse.angles). Defaulted
    # so a caller constructing a report positionally — every existing test — keeps
    # working, and because zero is the honest value on a day with no opening.
    angles_written: int = 0


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


def lookback_since(days: int, *, now: Callable[[], dt.datetime] | None = None) -> dt.datetime:
    """The ``since`` bound for a backfill of the last ``days`` days.

    Shared by the CLI's ``--since-days`` and the dashboard's backfill control so
    both mean exactly the same window.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    return (now or _utcnow)() - dt.timedelta(days=days)


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
            batch = fetch(
                feed.url,
                since,
                source=feed.name,
                fetched_at=fetched_at,
                per_entry_source=feed.per_entry_source,
            )
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


def _fetch_topics(
    feeds_by_client: dict[int, Feed],
    clients: Sequence[Client],
    since: dt.datetime,
    fetch: FetchFeed,
    fetched_at: dt.datetime,
    errors: list[str],
) -> tuple[list[Candidate], int]:
    """Fetch each client's topic radar, pairing every item with *that* client.

    The pairing is carried, not derived. These items are market coverage the
    client can speak to (:mod:`newspulse.angles`), and the client's name does not
    appear in them — so the term matcher would reject every one, correctly. What
    makes an item belong to a mandate here is which mandate's themes found it.

    Fault-isolated per feed like :func:`_fetch_all`, and returns the number of
    radar feeds that fetched cleanly so a run's feed count stays truthful.

    A radar query is scoped to the client's field (:func:`gnews.topic_feeds`),
    which is what keeps a theme like "Wachstum" from returning Canada's GDP. For
    a mandate whose themes are already narrow phrases that scope can intersect to
    nothing — measured: ``"KI in der Kosmetik" AND "Beauty Tech"`` returns zero —
    so a result with nothing usable in it falls back to the unscoped query once.

    "Nothing usable" rather than "nothing at all", because the failure that
    matters looks different from the outside. A young company dominates its own
    niche: measured for a beauty-tech mandate, the scoped query returned three
    items, two of them its own launch coverage. Non-empty, so the old fallback
    never fired — and the one remaining item was all that stood between the
    mandate and an impulse page that said "no market news" for months. A radar
    that only finds the client is a radar that found nothing.
    """
    by_id = {client.id: client for client in clients}
    pairs: list[Candidate] = []
    feeds_ok = 0
    for client_id, feed in feeds_by_client.items():
        client = by_id.get(client_id)
        if client is None:
            continue
        try:
            batch = fetch(
                feed.url,
                since,
                source=feed.name,
                fetched_at=fetched_at,
                per_entry_source=feed.per_entry_source,
            )
            about_client = name_matcher(client)
            usable = [i for i in batch if not mentions_client(i, about_client)]
            if len(usable) < _MIN_RADAR_ITEMS:
                widened = gnews.unscoped_topic_url(client)
                if widened is not None and widened != feed.url:
                    _log.info(
                        "topic radar for %r yielded %d usable item(s) in its field; "
                        "widening",
                        client.name,
                        len(usable),
                    )
                    # Added to, not replacing: an item naming the client is still a
                    # real radar hit, and the material query filters it later on the
                    # stronger evidence of an actual analysis.
                    batch = [
                        *batch,
                        *_on_theme(
                            fetch(
                                widened,
                                since,
                                source=feed.name,
                                fetched_at=fetched_at,
                                per_entry_source=feed.per_entry_source,
                            ),
                            client,
                        ),
                    ]
        except Exception as exc:  # noqa: BLE001 — per-feed fault-isolation boundary
            _log.warning("topic radar %r failed to fetch: %s; skipping", feed.name, exc)
            errors.append(f"feed {feed.name!r}: {exc}")
            continue
        feeds_ok += 1
        pairs.extend(Candidate(item=item, client=client) for item in batch)
    return pairs, feeds_ok


def _on_theme(items: Sequence[FeedItem], client: Client) -> list[FeedItem]:
    """Keep the widened query's items that actually carry one of the themes.

    Three doors lead into ``topic_hits`` and only two were guarded. The archive
    linker demands theme *and* field with word boundaries. The scoped search
    carries "AND (Branche)" in the query itself, and that clause does real work —
    measured on a fashion mandate it took 100 loose results down to 6 relevant.

    The third door is this one: when the scoped query comes back with nothing
    usable, the radar re-asks *without* the field clause, leaving a bare OR-chain
    of themes. Google answers such a chain generously and nothing downstream knows
    a hit arrived that way. That is how "Putin Signs Russia's First Crypto Law"
    ended up in a Neobank's market radar — stored, indistinguishable from signal,
    and eventually pitched.

    Measured on that mandate's real themes: the scoped query returned 1 item, the
    widened one 45 — of which 32 carry a theme term in the syndicated text and 13
    carry none. Applied here and only here, so a legitimate hit whose headline
    does not repeat the theme ("BitMEX stellt den Betrieb ein" for a mandate whose
    theme is "Onchain-Liquidität") still arrives through the scoped door, where a
    field clause vouches for it.

    The cost is a genuine synonym now and then ("Firmeninsolvenzen" where the
    theme says "Unternehmensinsolvenzen"). On the unanchored query that is the
    right side to err on: a missing item is invisible, a wrong one is believed.
    """
    matcher = radar_matcher(client)
    if matcher is None:
        return list(items)
    kept = [item for item in items if on_theme(item, matcher)]
    dropped = len(items) - len(kept)
    if dropped:
        _log.info(
            "widened radar for %r: dropped %d of %d item(s) that carry none of "
            "its themes in the syndicated text",
            client.name, dropped, len(items),
        )
    return kept


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
        author=item.author,
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


def _link(item: FeedItem | Article) -> str:
    """The item's link (``link`` on a FeedItem, ``url`` on a stored Article)."""
    return (getattr(item, "url", None) or getattr(item, "link", None) or "").strip()


def _articles_by_url(session: Session, urls: set[str]) -> dict[str, Article]:
    """Stored articles keyed by URL, for the URLs among this run's candidates."""
    if not urls:
        return {}
    rows = session.scalars(select(Article).where(Article.url.in_(urls))).all()
    return {article.url: article for article in rows}


def _articles_by_hash(session: Session, hashes: set[str]) -> dict[str, Article]:
    """Stored articles keyed by title hash, for the collapse-hashes among candidates.

    Dedup + UNIQUE(url) keep at most one stored article per significant title hash, so
    this is a 1:1 map used to resolve a collapsed near-duplicate copy to the article it
    was folded into."""
    if not hashes:
        return {}
    rows = session.scalars(select(Article).where(Article.title_hash.in_(hashes))).all()
    return {article.title_hash: article for article in rows}


def _existing_analysis_pairs(
    session: Session, article_ids: set[int]
) -> set[tuple[int, int]]:
    """The (article_id, client_id) pairs already carrying an analysis.

    Reads the UNIQUE(article_id, client_id) space so re-analysis is idempotent: a pair
    already present is skipped, making that constraint the real idempotency backstop
    rather than dead weight."""
    if not article_ids:
        return set()
    rows = session.execute(
        select(Analysis.article_id, Analysis.client_id).where(
            Analysis.article_id.in_(article_ids)
        )
    ).all()
    return {(row.article_id, row.client_id) for row in rows}


def _resolve_articles(
    session: Session, candidates: Sequence[Candidate]
) -> dict[int, Article]:
    """Map each candidate item (by object identity) to the stored Article it concerns.

    Resolves by URL first, then by collapse title-hash so a near-duplicate copy that
    dedup folded into a *different* kept article still resolves to the stored story —
    the copy carrying a distinct client match (its matching text lives only on the
    dropped copy, never in the DB) is what would otherwise lose coverage. Because the
    lookup hits the whole archive, a story already stored from an earlier run (e.g. one
    whose analysis failed) resolves too, so it can be re-analysed rather than dropped.
    """
    urls = {u for item, _ in candidates if (u := _link(item))}
    hashes = {h for item, _ in candidates if (h := dedup_title_hash(item.title, item.source)) is not None}
    by_url = _articles_by_url(session, urls)
    by_hash = _articles_by_hash(session, hashes)
    resolved: dict[int, Article] = {}
    for item, _ in candidates:
        article = by_url.get(_link(item))
        if article is None:
            thash = dedup_title_hash(item.title, item.source)
            article = by_hash.get(thash) if thash is not None else None
        if article is not None:
            resolved[id(item)] = article
    return resolved


def _candidate_pairs(
    session: Session, candidates: Sequence[Candidate]
) -> list[tuple[Article, Client]]:
    """This run's candidate pairs, each resolved to its stored Article."""
    resolved = _resolve_articles(session, candidates)
    pairs: list[tuple[Article, Client]] = []
    for item, client in candidates:
        article = resolved.get(id(item))
        if article is not None:
            pairs.append((article, client))
    return pairs


def _backfill_pairs(
    session: Session, clients: Sequence[Client], started: dt.datetime
) -> list[tuple[Article, Client]]:
    """Re-match recently stored articles against the active clients (self-healing pass).

    A transient analyzer outage leaves an article stored with no analysis; if the story
    then drops out of its feed, this run's fetch never surfaces it again. Re-matching
    the articles fetched within :data:`_BACKFILL_WINDOW` recovers those pairs so a later
    run analyses them once the analyzer is healthy again. Bounded by the window so the
    scan stays proportional to recent history, not the whole archive."""
    cutoff = started - _BACKFILL_WINDOW
    articles = session.scalars(
        select(Article).where(Article.fetched_at >= cutoff)
    ).all()
    if not articles:
        return []
    return [(candidate.item, candidate.client) for candidate in match_candidates(articles, clients)]


def _group_pairs(
    session: Session, pairs: Sequence[tuple[Article, Client]]
) -> list[tuple[Client, list[Article]]]:
    """Collapse (article, client) pairs into per-client work, dropping analysed ones.

    Deduplicates pairs within this run and drops any that already have an Analysis row,
    so nothing is analysed twice — the second run of an idempotent sweep resolves every
    pair but finds them all already analysed and does no work."""
    article_ids = {article.id for article, _ in pairs}
    analysed = _existing_analysis_pairs(session, article_ids)
    grouped: dict[int, list[Article]] = {}
    clients_by_id: dict[int, Client] = {}
    seen_pairs: set[tuple[int, int]] = set()
    for article, client in pairs:
        key = (article.id, client.id)
        if key in seen_pairs or key in analysed:
            continue
        seen_pairs.add(key)
        grouped.setdefault(client.id, []).append(article)
        clients_by_id[client.id] = client
    return [(clients_by_id[cid], articles) for cid, articles in grouped.items()]


def _record_topic_hits(
    session: Session, topic_pairs: Sequence[Candidate], found_at: dt.datetime
) -> int:
    """Link each stored radar article to the client whose themes surfaced it.

    Nothing in the article says which mandate it concerns — its themes found it,
    and the article never names them — so without this row the market material is
    in the archive attached to nobody. It is what makes the Marktumfeld view and
    the outlet ranking possible at all.

    Idempotent against the UNIQUE (article_id, client_id): a re-run that
    re-surfaces the same story adds nothing.
    """
    if not topic_pairs:
        return 0
    resolved = _resolve_articles(session, topic_pairs)
    wanted: set[tuple[int, int]] = set()
    for item, client in topic_pairs:
        article = resolved.get(id(item))
        if article is not None:
            wanted.add((article.id, client.id))
    if not wanted:
        return 0
    existing = {
        (row.article_id, row.client_id)
        for row in session.execute(
            select(TopicHit.article_id, TopicHit.client_id).where(
                TopicHit.article_id.in_({article_id for article_id, _ in wanted})
            )
        ).all()
    }
    fresh = [pair for pair in sorted(wanted) if pair not in existing]
    session.add_all(
        TopicHit(article_id=article_id, client_id=client_id, found_at=found_at)
        for article_id, client_id in fresh
    )
    session.commit()
    return len(fresh)


def _generate_angles(
    session: Session, topic_pairs: Sequence[Candidate], errors: list[str]
) -> int:
    """Draft a positioning message per mandate whose radar found something new.

    Runs after the sweep's own row is written, and — like :func:`_notify` — logs
    its failures instead of appending to the run's errors. A missing draft is a
    missed opportunity, not a broken sweep, and marking the run ``partial`` for it
    would both misreport the coverage pipeline and re-open the "since" window for
    a reason that has nothing to do with coverage.

    ``topic_pairs`` has already been narrowed to this run's *fresh* radar items by
    the caller. That narrowing is load-bearing: a search feed keeps listing a story
    for days, so drafting off everything it returns would re-pitch the same
    development every morning — the behaviour that trains a reader to ignore a
    column.

    One call per mandate with material, none for a mandate without. ``errors`` is
    accepted so a future caller can choose to surface these; nothing is appended
    today, deliberately.
    """
    if not topic_pairs:
        return 0
    resolved = _resolve_articles(session, topic_pairs)
    grouped: dict[int, tuple[Client, list[tuple[Article, str]]]] = {}
    for item, client in topic_pairs:
        article = resolved.get(id(item))
        if article is None:
            continue
        entry = grouped.setdefault(client.id, (client, []))
        entry[1].append((article, item.source))

    written = 0
    for client, material in grouped.values():
        try:
            result = angles.suggest(session, client, material)
        except Exception as exc:  # noqa: BLE001 — per-client fault-isolation boundary
            _log.warning("positioning draft for %r failed: %s; skipping", client.name, exc)
            continue
        if result is None:
            continue
        draft, numbered = result
        angles.store(session, client, draft, numbered)
        written += 1
        _log.info("positioning draft stored for %r: %s", client.name, draft.subject)
    return written


def refresh_radar(
    session: Session,
    client: Client,
    *,
    fetch: FetchFeed = fetch_feed,
    now: Callable[[], dt.datetime] | None = None,
) -> int:
    """Fetch this client's topic radar and link what it finds. No model call.

    What a newly added theme is for: the search runs immediately instead of at the
    next nightly sweep, so the person who just added it can see whether it brought
    anything back. Returns the number of (article, client) links that are new —
    the honest measure of what the theme added, since the articles themselves may
    well have been in the archive already.
    """
    now_fn = now or _utcnow
    started = now_fn()
    since = started - IMPULSE_LOOKBACK
    errors: list[str] = []

    radar = gnews.topic_feeds([client])
    if not radar:
        return 0
    pairs, _ok = _fetch_topics(radar, [client], since, fetch, started, errors)
    if not pairs:
        return 0
    known_urls, known_hashes = _load_known(session)
    fresh = deduplicate(
        _distinct_items(pairs), known_urls=known_urls, known_title_hashes=known_hashes
    )
    if fresh:
        _persist_articles(session, fresh, started)
    return _record_topic_hits(session, pairs, started)


#: How many market items an impulse falls back to when the window holds none.
#: Three, because that is what was asked for and because a fourth adds little: the
#: model reads them as "what has happened in this field lately", and a field that
#: produced three items in six months has told you its pace.
IMPULSE_FALLBACK_ITEMS = 3


def market_material(
    session: Session,
    client: Client,
    since: dt.datetime | None = None,
    *,
    newest: int = IMPULSE_FALLBACK_ITEMS,
) -> list[tuple[Article, str]]:
    """What the radar has stored for ``client`` — minus its own press.

    A theme search finds the mandate's own coverage too, and offering that back as
    "a market development to position against" asks for a statement about itself.
    The model's own words for it were "both items report on the mandate itself — a
    text about that would be self-promotion, not analysis". The radar is defined
    as coverage the client can speak *to*, never coverage *of* it, and this is
    where that definition has to hold, because this is where the material is read.

    Dismissed and irrelevant matches are not coverage, so ``visible_coverage()``
    is the right gate rather than "has any analysis": an article the analyzer
    scored as not about this client is market material like any other.

    ``since`` bounds the window, and an empty window falls back to the ``newest``
    items whatever their age — *"vielleicht lösen wir einfach die 90-Tage-
    Restriktion und schauen immer auf die letzten 3 Artikel"*. The cutoff is a
    freshness preference, not a fact about the world: a mandate in a field that
    moves twice a year had an empty column for months because of a boundary it
    could not see, and a four-month-old development it never spoke to is worth
    more than nothing at all. The model still refuses stale material — the age
    goes into the prompt — so the bar stays where it belongs, with the judgement
    rather than with the SQL.
    """
    own_coverage = (
        select(Analysis.article_id)
        .where(Analysis.client_id == client.id, visible_coverage())
        .scalar_subquery()
    )
    base = (
        select(Article)
        .join(TopicHit, TopicHit.article_id == Article.id)
        .where(TopicHit.client_id == client.id, Article.id.not_in(own_coverage))
        .order_by(Article.published_at.desc())
    )
    if since is not None:
        inside = session.scalars(base.where(Article.published_at >= since)).all()
        if inside:
            return [(article, "Themen-Radar") for article in inside]
        base = base.limit(max(1, newest))
    return [(article, "Themen-Radar") for article in session.scalars(base).all()]


def link_archive_to_themes(
    session: Session,
    clients: Sequence[Client],
    since: dt.datetime,
    found_at: dt.datetime,
) -> int:
    """Link stored articles that match a client's themes as market material.

    The gap this closes is the largest one in the feature. Market material had
    exactly one source — a Google News search per client — while the registry's
    68 subscribed feeds fetched the German trade press every morning and left it
    attached to nobody. Measured on a real archive: 397 articles in the ninety-day
    window, **6** of them linked as market material, with 38 carrying "Logistik"
    in the headline and 18 carrying "Mode" — precisely what a fashion mandate
    would position on, already paid for, already stored, and invisible.

    So a mandate's radar could return nothing while the answer sat in its own
    archive. That is why "keine Marktmeldung gefunden" kept appearing after every
    fix upstream of it: the search was never the only place to look.

    Costs nothing new: no fetch, no model call, one bounded query per client. The
    client's own coverage is excluded — an article about the mandate belongs in
    its coverage, and offering it back as a market development to position
    against asks for a statement about itself.

    Scoped to the client's field, exactly as the radar query is, and for the same
    measured reason. Without it this reproduces the failure the field clause was
    invented to prevent: matching Zalando's themes against the archive unscoped
    linked 129 articles, among them "Wirtschaft in Kanada: Wachstum 3,4 %" and
    "Apple Watch: 21 % Wachstum". A mandate with no industry is therefore skipped
    rather than filled with noise — a theme like "Wachstum" means nothing without
    a field to read it in, and guessing one would change what the tool finds
    without saying so.
    """
    written = 0
    for client in clients:
        if client.is_competitor:
            continue
        matcher = theme_matcher(client)
        field = terms_matcher(gnews.context_terms(client))
        if matcher is None or field is None:
            if matcher is not None:
                _log.info(
                    "no industry for %r; skipping archive linking rather than "
                    "matching its themes against everything",
                    client.name,
                )
            continue
        about_client = name_matcher(client)
        own = (
            select(Analysis.article_id)
            .where(Analysis.client_id == client.id, visible_coverage())
            .scalar_subquery()
        )
        already = (
            select(TopicHit.article_id)
            .where(TopicHit.client_id == client.id)
            .scalar_subquery()
        )
        candidates = session.scalars(
            select(Article).where(
                Article.published_at >= since,
                Article.id.not_in(own),
                Article.id.not_in(already),
            )
        ).all()
        hits = []
        for article in candidates:
            text = _article_text(article)
            # Theme AND field, the same conjunction the search URL builds.
            if not (matcher.search(text) and field.search(text)):
                continue
            if mentions_client(article, about_client):
                continue
            hits.append(article)
        if not hits:
            continue
        session.add_all(
            TopicHit(article_id=article.id, client_id=client.id, found_at=found_at)
            for article in hits
        )
        session.commit()
        written += len(hits)
        _log.info(
            "linked %d archived article(s) to %r's themes", len(hits), client.name
        )
    return written


def _article_text(article: Article) -> str:
    """Title plus feed summary, case-folded. Only syndicated text — no body."""
    return f"{article.title or ''}\n{article.summary_text or ''}".casefold()


def _refresh_impulses(
    session: Session,
    clients: Sequence[Client],
    errors: list[str],
    *,
    now: dt.datetime,
) -> int:
    """Make sure every mandate has a current positioning draft, not only the ones
    whose radar happened to move today.

    :func:`_generate_angles` drafts from *fresh* radar items, which is right for
    "react to what arrived this morning" and wrong as the only source of drafts: a
    mandate whose field was quiet for a fortnight had an empty Impulse column for a
    fortnight, and that is exactly the mandate whose consultant most needs
    something to say. So after the sweep, any mandate whose newest draft has aged
    out gets one built from everything the radar has stored in the window.

    Bounded by construction rather than by a cap: a client is only asked about
    once its previous draft is ``IMPULSE_REFRESH_AFTER`` old, so the steady state
    is a handful of calls a day, and a client with no market material costs
    nothing at all. Failures are logged, never appended to the run's errors — a
    missing draft is a missed opportunity, not a broken sweep.
    """
    cutoff = now - IMPULSE_REFRESH_AFTER
    since = now - IMPULSE_LOOKBACK
    written = 0
    for client in clients:
        if client.is_competitor:
            continue
        current = angles.latest(session, client.id)
        if current is not None and current.generated_at >= cutoff:
            continue

        def _note(text: str, *, target: Client = client) -> None:
            """Record why, on the client, so the page can say it hours later.

            The reason used to live in a dict in the web process, written only by
            the button — so a sweep that found nothing left the page silent, and
            "es funktioniert immer noch nicht" came back over work that was
            running correctly.
            """
            target.impulse_note = text
            target.impulse_checked_at = now
            session.commit()

        material = market_material(session, client, since)
        if not material:
            _note(
                "Kein Marktmaterial — das Radar fand nichts, was nicht schon "
                "Berichterstattung über den Mandanten selbst ist. Ein Impuls "
                "braucht ein Thema, über das auch ohne ihn geschrieben wird."
            )
            continue
        try:
            result = angles.suggest(session, client, material, note=_note)
        except Exception as exc:  # noqa: BLE001 — per-client fault-isolation boundary
            _log.warning("impulse refresh for %r failed: %s; skipping", client.name, exc)
            _note(f"Der Entwurf ist mit einem Fehler abgebrochen: {exc}")
            continue
        if result is None:
            _log.info("impulse refresh for %r: no opening in stored material", client.name)
            if not client.impulse_note:
                _note(f"Aus {len(material)} Marktmeldung(en) ergab sich kein Anlass.")
            continue
        draft, numbered = result
        angles.store(session, client, draft, numbered)
        client.impulse_note = ""
        client.impulse_checked_at = now
        session.commit()
        written += 1
        _log.info("impulse refreshed for %r: %s", client.name, draft.subject)
    return written


def _analysis_targets(
    session: Session,
    candidates: Sequence[Candidate],
    clients: Sequence[Client],
    started: dt.datetime,
) -> list[tuple[Client, list[Article]]]:
    """Every (client, articles) group that still needs analysis this run.

    Unions two sources so a stored-but-un-analysed story self-heals however it was
    lost: this run's candidate pairs resolved to their stored Article (recovers a
    distinct match carried only on a collapsed copy, and re-analyses a still-in-feed
    story whose analysis failed earlier), plus a backfill re-match of recently stored
    articles (recovers a story that has since left its feed). Pairs already analysed are
    dropped, so a re-run adds nothing."""
    pairs = _candidate_pairs(session, candidates)
    pairs.extend(_backfill_pairs(session, clients, started))
    return _group_pairs(session, pairs)


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
        tonality=verdict.tonality,
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
    # Read before the call so only this client's failures are attributed to it.
    before = getattr(analyzer, "failed_batches", 0)
    try:
        analyses = analyzer.analyze(client, articles)
        for verdict in analyses:
            session.add(_to_orm_analysis(verdict))
        session.commit()
        # analyze() is contractually forbidden from raising — a failing batch
        # must not sink the sweep — so the except clause below never sees the
        # ordinary backend failure. That left the worst outage silent: an
        # unreachable or unauthenticated `claude` dropped every batch, wrote no
        # error, and the run was recorded OK, so the dashboard showed a healthy
        # header over an empty day. Asking the analyzer what it dropped turns
        # that back into something the run reports.
        dropped = getattr(analyzer, "failed_batches", 0) - before
        if dropped > 0:
            reason = getattr(analyzer, "last_error", None) or "unbekannter Fehler"
            _log.error(
                "analysis backend dropped %d batch(es) for client %r: %s", dropped, name, reason
            )
            errors.append(f"analysis {name!r}: {dropped} batch(es) dropped: {reason}")
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
) -> Run:
    """Write the ``runs`` row for this sweep in a clean transaction and return it.

    The leading rollback clears any half-open transaction left by a mid-sweep crash
    (a no-op on the happy path, where every prior stage already committed) so the run
    record itself always persists — the one row that must exist even when the sweep
    failed. The committed row is returned so the caller can hand it to the post-run
    notification (:func:`_notify`).
    """
    session.rollback()
    run = Run(
        started_at=started,
        finished_at=finished,
        status=status,
        articles_found=articles_found,
        errors=list(errors),
    )
    session.add(run)
    session.commit()
    return run


def _record_sync_failure(
    session: Session, run: Run, errors: list[str], reported: Sequence[str]
) -> None:
    """Fold the mailbox's failures into the run row that is already written.

    The ``runs`` row is committed before the mailbox is touched — deliberately,
    so a dead Google cannot cost the day's coverage — which leaves it stating
    ``ok`` for a sweep whose last step failed. Amended here rather than by moving
    the sync in front of the finalize: the row is the thing that must survive,
    and a second small write is cheaper than putting it behind a network call.

    Downgraded to ``partial`` and never to ``failed``: the acceptance criterion
    is that an unreachable mailbox does not fail the daily sweep, and it does not
    — every stored row stands, nothing is rolled back, and the sweep's own work
    is untouched. A run that already failed keeps that status; there is nothing
    worse to say about it.
    """
    if not reported:
        return
    errors.extend(reported)
    if run.status is RunStatus.FAILED:
        return
    run.errors = [*run.errors, *reported]
    run.status = RunStatus.PARTIAL
    session.add(run)
    session.commit()


def _sync_mailbox(
    session: Session, run: Run, errors: list[str], *, now: dt.datetime
) -> int:
    """Read the replies to released letters; a mail failure never fails the sweep.

    Runs after the day's coverage is stored and the ``runs`` row is written, so
    by the time Google is asked anything the part of the sweep that does not
    depend on somebody else's service is already safe. A mailbox that is
    unreachable or an access that was revoked is reported at ERROR by the sync
    itself and returned in its report; anything unexpected is caught here for the
    same reason the notification is — the alternative is a green run's data being
    rolled back because a journalist's mail could not be read.

    Not failing the sweep is not the same as saying nothing. Whatever went wrong
    is folded into the run's own errors by :func:`_record_sync_failure`, so a
    mailbox that has been unreadable for a week shows as a partial run instead of
    a green one with a line in a log nobody tails. ``now`` is the sweep's clock:
    ``fetched_at`` says when this tool took a copy of somebody else's mail, and a
    run with a frozen clock has to be able to answer that.

    Returns how many replies were newly filed, for the run's own log line.
    """
    try:
        report = mailsync.sync(session, now=now)
    except Exception as exc:  # noqa: BLE001 — the mailbox must never fail the run
        _log.error(
            "mail sync failed: %s; run data already persisted, not rolled back", exc
        )
        # A raised write leaves the transaction half-open, and every later
        # statement on this session would die with it.
        session.rollback()
        _record_sync_failure(session, run, errors, [f"mail sync: {exc}"])
        return 0
    _record_sync_failure(session, run, errors, report.errors)
    return report.replies


def _notify(session: Session, run: Run) -> None:
    """Deliver the post-run alert notification; a failure here never fails the run.

    Called only after the ``runs`` row (and every article/analysis) is committed, so
    the sweep's data is already safe. :func:`newspulse.notify.notify_after_run` reads
    only and is non-raising by contract, but this wraps it in the same fault boundary
    the rest of the sweep uses — a broken notifier or an unexpected read error is
    logged at ERROR and swallowed, never rolling back a run that already succeeded.
    """
    try:
        notify.notify_after_run(session, run)
    except Exception as exc:  # noqa: BLE001 — notification must never fail the run
        _log.error(
            "post-run notification failed: %s; run data already persisted, not rolled back",
            exc,
        )


# --- Orchestration -------------------------------------------------------------


def run(
    session: Session,
    *,
    analyzer: Analyzer | None = None,
    feeds: Sequence[Feed] | None = None,
    fetch: FetchFeed = fetch_feed,
    now: Callable[[], dt.datetime] | None = None,
    dry_run: bool = False,
    since: dt.datetime | None = None,
) -> RunReport:
    """Execute one daily sweep and return its report.

    ``analyzer``/``feeds``/``fetch``/``now`` are injectable so the sweep can run over
    fixture feeds with a fake analyzer and a fixed clock; the defaults wire up the
    real registry, the subscription analyzer, and the live fetch. ``dry_run`` fetches,
    matches, and deduplicates and reports the counts without calling the analyzer or
    writing anything (no articles, no analyses, no ``runs`` row).

    ``since`` overrides the normal watermark, to backfill a wider window. It only
    widens which *fetched* items are accepted — RSS carries no "everything since
    X" request, so a feed still returns only the entries it currently syndicates
    (often days, not weeks). Backfill is therefore best-effort by nature, and
    re-running it is free: dedup drops everything already stored.

    The topic radar (per-mandate theme searches feeding
    :mod:`newspulse.angles`) runs only on the persisting path. A dry run reports
    what the coverage pipeline would store; drafting a positioning message is a
    model call whose output has nowhere to go without writes, so it is skipped
    rather than spent.
    """
    now_fn = now or _utcnow
    started = now_fn()
    errors: list[str] = []
    resolved_feeds = list(load_feeds() if feeds is None else feeds)
    clients = list_clients(session)
    since = since if since is not None else _determine_since(session, started)

    # Per-client searches are appended to the registry, never a replacement for
    # it: the registry gives reliable, well-formed publisher feeds, the searches
    # give reach beyond them. Only when the caller did not inject an explicit
    # feed list — an injected list is exactly what a test is pinning down.
    query_feeds: list[Feed] = []
    topic_by_client: dict[int, Feed] = {}
    if feeds is None and config.GOOGLE_NEWS_ENABLED:
        query_feeds = gnews.client_feeds(list(clients))
        resolved_feeds.extend(query_feeds)
        # Mandates only. A competitor is tracked to compare its share of the
        # conversation; nobody writes it a positioning message, so spending a
        # model call on one would be spending it on nothing.
        topic_by_client = gnews.topic_feeds(
            [client for client in clients if not client.is_competitor]
        )

    _log.info(
        "run start: %d active client(s), %d feed(s) (%d registry + %d client search "
        "+ %d topic radar), since=%s%s",
        len(clients),
        len(resolved_feeds) + len(topic_by_client),
        len(resolved_feeds) - len(query_feeds),
        len(query_feeds),
        len(topic_by_client),
        since.isoformat(),
        " [dry-run]" if dry_run else "",
    )
    if dry_run:
        return _run_dry(session, resolved_feeds, clients, since, fetch, started, errors)
    return _run_real(
        session,
        resolved_feeds,
        clients,
        since,
        fetch,
        now_fn,
        started,
        errors,
        analyzer,
        topic_by_client,
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
    """The read-only preview: fetch, match, dedup, count. Writes nothing.

    Wrapped in the same top-level fault boundary as :func:`_run_real` so an
    unexpected error (a DB error loading the known set, anything in fetch/match/dedup)
    is logged and reported as a ``failed`` preview rather than escaping as a raw
    traceback to the CLI. A dry run writes no ``runs`` row, so the report is the only
    record of the failure — it must not be lost."""
    feeds_ok = items_count = candidates_count = new_articles = 0
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
        new_articles = len(kept)
        _log.info(
            "dry-run: %d item(s) fetched, %d candidate(s), %d new article(s) — no writes",
            items_count,
            candidates_count,
            new_articles,
        )
        status = _final_status(errors)
    except Exception as exc:  # noqa: BLE001 — top-level so a dry-run crash reports cleanly
        _log.exception("dry-run aborted before completion: %s", exc)
        errors.append(f"run aborted: {exc}")
        status = RunStatus.FAILED
    return RunReport(
        status=status,
        feeds_total=len(feeds),
        feeds_ok=feeds_ok,
        items_fetched=items_count,
        candidates=candidates_count,
        new_articles=new_articles,
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
    topic_feeds: dict[int, Feed] | None = None,
) -> RunReport:
    """The persisting sweep, wrapped so any crash still records a ``failed`` run."""
    new_articles = 0
    analyses_written = 0
    angles_written = 0
    feeds_ok = items_count = candidates_count = 0
    # Bound before the try so the post-run drafting step has a defined value even
    # when the sweep aborts on its first line.
    topic_pairs: list[Candidate] = []
    radar = topic_feeds or {}
    status = RunStatus.OK
    try:
        items, feeds_ok = _fetch_all(feeds, since, fetch, started, errors)
        topic_pairs, topics_ok = _fetch_topics(radar, clients, since, fetch, started, errors)
        feeds_ok += topics_ok
        items_count = len(items) + len(topic_pairs)
        candidates = _match(items, clients, errors)
        candidates_count = len(candidates)
        known_urls, known_hashes = _load_known(session)
        # The radar's items are deduplicated and stored alongside the matched ones,
        # in one pass: a development that arrives both as coverage and as a radar
        # hit must become one article, or the archive holds it twice and the draft
        # cites a copy the reader cannot find.
        kept = deduplicate(
            _distinct_items([*candidates, *topic_pairs]),
            known_urls=known_urls,
            known_title_hashes=known_hashes,
        )
        # Narrow the radar's pairs to what dedup actually kept, i.e. what is new
        # today. Without this the drafting step sees every item the radar returned
        # — including the ones stored days ago, which a search feed keeps listing —
        # and re-pitches the same development every morning.
        #
        # Identity, because dedup returns the very objects it was given. A story
        # that arrived through both routes and lost its radar copy to the kept
        # coverage copy drops out here; that is the right outcome, since a story
        # naming the client is coverage and already has a column.
        fresh = {id(item) for item in kept}
        articles = _persist_articles(session, kept, started)
        new_articles = len(articles)
        # Attach the radar's stories to the client whose themes found them, so the
        # market material is browsable rather than merely stored.
        #
        # Every pair, not just today's new ones. A TopicHit is an association, not
        # a copy: it says "this client's themes surfaced this story". Recording
        # only the fresh ones meant an article the archive already held — the
        # common case, since a search feed keeps listing the same items for days,
        # and since a story can arrive first as another client's coverage — never
        # got linked to this client at all. Its radar then read as empty forever,
        # and the impulse button, which draws on exactly these rows, could never
        # find material. The write is idempotent against UNIQUE(article, client).
        _record_topic_hits(session, topic_pairs, started)
        # Drafting is the part that must see only what is new today, or the same
        # development gets re-pitched every morning.
        topic_pairs = [pair for pair in topic_pairs if id(pair.item) in fresh]
        resolved_analyzer = analyzer or get_analyzer()
        for client, client_articles in _analysis_targets(session, candidates, clients, started):
            analyses_written += _analyze_and_persist(
                session, client, client_articles, resolved_analyzer, errors
            )
        status = _final_status(errors)
    except Exception as exc:  # noqa: BLE001 — top-level so a crash still records a run
        _log.exception("run aborted before completion: %s", exc)
        errors.append(f"run aborted: {exc}")
        status = RunStatus.FAILED
        session.rollback()
    run = _finalize_run(session, started, now_fn(), status, new_articles, errors)
    # The mailbox, once a day, with the sweep. Deliberately outside the
    # status check below: reading the replies to letters that went out weeks ago
    # has nothing to do with whether this morning's feeds answered, and a
    # journalist's answer is the one thing in this tool nobody can re-fetch
    # later by pressing a button.
    replies = _sync_mailbox(session, run, errors, now=now_fn())
    # The run row may have been downgraded to partial by an unreadable mailbox;
    # the report has to say the same thing the stored row does. Never to failed,
    # so the post-run work below still runs — a dead Google says nothing about
    # whether this morning's feeds answered.
    status = run.status
    # Drafting happens after the run is recorded and only if the sweep itself came
    # through: pitching a positioning message off a half-fetched radar would put a
    # confident text in front of the reader on the strength of partial data.
    if status is not RunStatus.FAILED:
        # A mandate with no themes has no radar, and every downstream feature
        # reads off that radar. It was only ever filled in at onboarding, so every
        # mandate created before that existed sat permanently in the state the
        # onboarding step prevents — reported three times as "hier wird immer noch
        # kein Impuls angezeigt". Self-limiting: the call returns immediately once
        # a radar is in place.
        settled = 0
        for client in clients:
            if client.is_competitor:
                continue  # a yardstick has no impulse page for a radar to fill
            if settled >= _SETTLE_PER_SWEEP:
                break
            try:
                if themes.settle(session, client, fetch=fetch):
                    settled += 1
            except Exception:  # noqa: BLE001 — a radar is not worth a failed sweep
                _log.exception("theme settling for %r failed", client.name)
                # A caught exception is not a clean session. ``settle`` writes,
                # so a failed flush leaves the transaction in
                # ``PendingRollbackError`` and every later statement in this
                # block dies with it — after ``_finalize_run`` has already
                # recorded the sweep as ok. Reproduced: the header shows a green
                # run with zero errors while the drafting, the archive linking
                # and the notification were all skipped.
                session.rollback()
        # The archive first: the registry feeds fetched the trade press this
        # morning and nothing linked it to the mandates whose field it is. Doing
        # this before drafting means today's material is available to today's
        # draft rather than tomorrow's.
        linked = link_archive_to_themes(
            session, clients, started - IMPULSE_LOOKBACK, started
        )
        if linked:
            _log.info("linked %d archived article(s) as market material", linked)
        angles_written = _generate_angles(session, topic_pairs, errors)
        # And then top up the mandates the radar did not move today. Drafting only
        # from fresh material meant a quiet fortnight showed an empty Impulse
        # column for a fortnight — for exactly the mandate whose consultant most
        # needs something to say.
        angles_written += _refresh_impulses(session, clients, errors, now=now_fn())
    _log.info(
        "run done: status=%s, %d new article(s), %d analysis(es), %d draft(s), "
        "%d repl(y/ies), %d error(s)",
        status.value,
        new_articles,
        analyses_written,
        angles_written,
        replies,
        len(errors),
    )
    # The run's data is committed; deliver any fired-alert notification now. This is
    # the wiring for AC #1 ("after a run, if any alerts fired, a notification ... is
    # delivered") — read-only and fault-isolated, so it can't roll the sweep back.
    _notify(session, run)
    return RunReport(
        status=status,
        feeds_total=len(feeds) + len(radar),
        feeds_ok=feeds_ok,
        items_fetched=items_count,
        candidates=candidates_count,
        new_articles=new_articles,
        analyses_written=analyses_written,
        errors=list(errors),
        dry_run=False,
        angles_written=angles_written,
    )


# --- Onboarding: the first fill for a newly added client -----------------------

# How much coverage a new mandate arrives with. A cap on *articles*, not on days,
# because that is what bounds the cost: every one of them goes through the
# analyzer in batches. Thirty is roughly three batch calls — a few minutes and a
# small slice of the subscription — and enough that the mandate's page is not
# empty on the day it is created, which was the complaint that started this.
ONBOARDING_ARTICLES = 30

# How far back the onboarding fetch is willing to look. Generous, because the cap
# above is the real limit: a search feed reaching six months back simply yields
# its newest thirty, and a mandate whose coverage is older than this was not
# being written about anyway.
_ONBOARDING_LOOKBACK = dt.timedelta(days=180)


def backfill_client(
    session: Session,
    client: Client,
    *,
    limit: int = ONBOARDING_ARTICLES,
    analyzer: Analyzer | None = None,
    fetch: FetchFeed = fetch_feed,
    now: Callable[[], dt.datetime] | None = None,
) -> int:
    """Fetch a new client's recent coverage — and the market it sits in.

    Runs when a mandate is created, so its page has something from the first
    minute. Two fetches, because a young company has two very different starting
    positions and the second is the common one:

    * its **own coverage**, from its name search, analysed and capped at ``limit``;
    * its **market**, from the topic radar — articles that never mention it but
      discuss its subject. Those are stored unanalysed, exactly as the daily sweep
      stores them, and they are what a positioning draft is made of.

    Without the second fetch a mandate nobody writes about yet arrives empty and
    stays empty until the next nightly sweep, which is precisely the company that
    most needs something to say. With it, the run can offer a draft on day one.

    Deliberately narrow otherwise: only this client's feeds, only this client
    matched, only this client analysed. The registry feeds are portfolio-wide, and
    re-fetching them to onboard one company would be a sweep, not an onboarding.

    **No ``runs`` row is written**, and that is load-bearing rather than an
    omission. ``_determine_since`` takes the last successful run's start as the
    watermark for the next sweep, so recording this narrow, single-client fetch as
    a run would tell the next daily sweep that everything up to now had already
    been covered — and the rest of the portfolio would silently lose a day.

    Returns the number of articles stored. Raises nothing on a dead feed: the
    fetch is fault-isolated per feed like the sweep's.
    """
    now_fn = now or _utcnow
    started = now_fn()
    since = started - _ONBOARDING_LOOKBACK
    errors: list[str] = []
    # Newest first, then capped: "the last 30" is a recency promise, and taking
    # whichever thirty the feed happened to list would break it. The fallback
    # keeps the sort from crashing on an item that somehow carries no date; such
    # an item sorts last rather than taking a slot from a dated one.
    oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
    newest_first = lambda batch: sorted(  # noqa: E731 — a sort key, not a function
        batch, key=lambda item: item.published_at or oldest, reverse=True
    )
    known_urls, known_hashes = _load_known(session)

    # --- Its own coverage ------------------------------------------------------
    articles: list[Article] = []
    feeds = gnews.client_feeds([client])
    if feeds:
        items, _ok = _fetch_all(feeds, since, fetch, started, errors)
        candidates = _match(items, [client], errors)
        kept = newest_first(
            deduplicate(
                _distinct_items(candidates),
                known_urls=known_urls,
                known_title_hashes=known_hashes,
            )
        )[:limit]
        if kept:
            articles = _persist_articles(session, kept, started)
            resolved_analyzer = analyzer or get_analyzer()
            _analyze_and_persist(session, client, articles, resolved_analyzer, errors)
            # Re-read what is stored, so the radar below cannot store a second copy
            # of a story that just arrived through the name search.
            known_urls, known_hashes = _load_known(session)

    # --- The market it sits in -------------------------------------------------
    topic_pairs: list[Candidate] = []
    radar = gnews.topic_feeds([client]) if not client.is_competitor else {}
    if radar:
        topic_pairs, _radar_ok = _fetch_topics(radar, [client], since, fetch, started, errors)
        fresh = newest_first(
            deduplicate(
                _distinct_items(topic_pairs),
                known_urls=known_urls,
                known_title_hashes=known_hashes,
            )
        )[:limit]
        if fresh:
            # Unanalysed on purpose, exactly as the sweep stores them: these are
            # not coverage of the client, and filing them as such would put a
            # market story into the mandate's own archive.
            _persist_articles(session, fresh, started)
        # Linked whether or not the archive already held them — a new mandate
        # whose field overlaps an existing one would otherwise start with an
        # empty radar, because every story it found was already stored.
        _record_topic_hits(session, topic_pairs, started)
        keep = {id(item) for item in fresh}
        topic_pairs = [pair for pair in topic_pairs if id(pair.item) in keep]

    drafted = _generate_angles(session, topic_pairs, errors) if topic_pairs else 0
    _log.info(
        "onboarded %r: %d article(s) about it, %d from its market, %d draft(s), %d error(s)",
        client.name,
        len(articles),
        len(topic_pairs),
        drafted,
        len(errors),
    )
    return len(articles)


#: How far back an on-demand impulse looks for market material. Wide, because the
#: question is different from the sweep's: the sweep asks "did something happen
#: overnight", a person clicking the button asks "is there anything to say at
#: all" — and three weeks answered "no" for a field that had moved two months ago.
#:
#: The width is nearly free. Radar material is stored unanalysed, so a longer
#: window costs one wider database read and the same single model call; the cap on
#: developments handed to the model (angles._MAX_DEVELOPMENTS) is what bounds the
#: prompt, and it takes the newest ones.
IMPULSE_LOOKBACK = dt.timedelta(days=90)

#: How long a positioning draft counts as current. Past this the sweep builds a
#: new one from stored material rather than waiting for the radar to move.
#:
#: Seven days, to match the window the Today column reads (angles.COLUMN_DAYS):
#: anything longer and that column empties out while a draft still exists, which
#: is the state that reads as "the feature is broken". Shorter would re-pitch the
#: same development to a mandate whose field simply had a quiet week.
IMPULSE_REFRESH_AFTER = dt.timedelta(days=7)


def draft_impulse(
    session: Session,
    client: Client,
    *,
    fetch: FetchFeed = fetch_feed,
    now: Callable[[], dt.datetime] | None = None,
    note: Callable[[str], None] | None = None,
) -> bool:
    """Fetch this client's market and draft one positioning message from it.

    The sweep only drafts from material that arrived *that morning*, which is right
    for a daily rhythm and wrong for a person asking the question directly: a
    mandate whose field was quiet today may still have plenty worth saying from the
    past fortnight. So this refreshes the radar and then works from everything
    stored for the client in the window, fresh or not.

    Returns whether a draft was stored. False covers "no themes", "nothing in the
    market" and the model's own "no opening here" — all three are honest answers
    to the question, and none of them is an error. ``note`` receives which one it
    was, in a sentence the reader can act on: a button that produces nothing and
    says nothing is indistinguishable from a broken one, and this one produced
    nothing far more often than anybody could tell.
    """
    now_fn = now or _utcnow
    started = now_fn()
    since = started - IMPULSE_LOOKBACK
    errors: list[str] = []

    said: list[str] = []

    def _say(message: str) -> None:
        # First explanation wins: the model's own reason is more use than the
        # generic fallback that follows it.
        if note is not None and not said:
            said.append(message)
            note(message)

    radar = gnews.topic_feeds([client])
    if not radar:
        _log.info("no themes for %r; nothing to draft from", client.name)
        _say(
            "Für diesen Mandanten sind keine Themen hinterlegt — ohne Themen gibt "
            "es kein Marktumfeld, aus dem ein Impuls entstehen könnte."
        )
        return False

    # Refresh first, so a click picks up what appeared since the last sweep.
    pairs, _ok = _fetch_topics(radar, [client], since, fetch, started, errors)
    known_urls, known_hashes = _load_known(session)
    fresh = deduplicate(
        _distinct_items(pairs), known_urls=known_urls, known_title_hashes=known_hashes
    )
    if fresh:
        _persist_articles(session, fresh, started)
    # Link every pair the radar returned, not just the ones stored a moment ago.
    # This is the click that is supposed to answer "what should we say?", and the
    # material query below reads these rows: gating them on novelty meant a
    # mandate whose stories the archive already held asked the question and got
    # "the radar has collected nothing" back, every time, forever.
    _record_topic_hits(session, pairs, started)

    # Then draw on everything stored for this client in the window — the point of
    # the button is that a quiet morning must not mean an empty answer.
    material = market_material(session, client, since)
    if not material:
        _log.info("no market material for %r in the last %s", client.name, IMPULSE_LOOKBACK)
        _say(
            "Das Themen-Radar hat keine einzige Marktmeldung gefunden, die nicht "
            "schon Berichterstattung über den Mandanten selbst ist — auch außerhalb "
            "der letzten "
            f"{IMPULSE_LOOKBACK.days} Tage nicht. Meist sind die hinterlegten Themen "
            "zu eng am Unternehmen formuliert: ein Impuls braucht ein Thema, über "
            "das auch ohne den Mandanten geschrieben wird."
        )
        return False

    result = angles.suggest(session, client, material, note=_say)
    if result is None:
        # suggest() has already explained a refusal; this covers the case where it
        # had nothing to refuse over.
        _say(
            f"Aus {len(material)} Marktmeldung(en) ergab sich kein tragfähiger "
            "Anlass."
        )
        return False
    draft, numbered = result
    angles.store(session, client, draft, numbered)
    _log.info("impulse drafted for %r on request: %s", client.name, draft.subject)
    return True


__all__ = [
    "IMPULSE_LOOKBACK",
    "ONBOARDING_ARTICLES",
    "draft_impulse",
    "RunReport",
    "backfill_client",
    "lookback_since",
    "run",
    "setup_logging",
]
