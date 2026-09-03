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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (
    angles,
    config,
    crisis,
    gnews,
    industry,
    issues,
    mailsync,
    market_sources,
    newsjack,
    notify,
    plan,
    profile_refresh,
    report,
    reputation,
    themes,
    visibility,
)
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
    Crisis,
    Run,
    RunStatus,
    Standing,
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

# How long a measured industry term is trusted before it is measured again. The
# probe is a live search per term, and what it measures — whether the German
# press writes a word at all — moves over months, not overnight. Monthly keeps
# the answer current at roughly one search per mandate per month; asking every
# morning would spend a search a day to re-learn the same fact.
_FIELD_RECHECK = dt.timedelta(days=30)
# How many monthly reports one sweep is willing to draft, for the same reason
# ``_SETTLE_PER_SWEEP`` exists: each is a model call inside the run guard, and on
# the first of the month every mandate is due at once. What is left over is
# drafted by tomorrow's sweep, well inside the window below.
_REPORTS_PER_SWEEP = 3

# How long after the turn of the month the sweep keeps offering to draft. DEC-3
# puts the draft at the Stichtag; a week is what lets that survive a sweep that
# failed on the first, a host that was down, or a mandate added on the second —
# and it stops a mandate whose generation keeps failing from being retried all
# month. Nobody has to press anything meanwhile: the drafts wait, and the surface
# can always draft a period on demand.
_REPORT_DRAFT_DAYS = 7

# How many mandates one sweep is willing to measure for KI-Sichtbarkeit, for the
# same reason ``_REPORTS_PER_SWEEP`` exists and rather more sharply: one
# measurement is the whole accepted set times every configured provider times two
# model calls each, all of it inside the run guard. Three a night clears a
# portfolio inside the weekly window, and a mandate deferred today is simply still
# due tomorrow.
_VISIBILITY_PER_SWEEP = 3

# How many editorial plans one sweep is willing to recompute, for the reason
# every other cap here exists: a recompute with candidates is one model call
# inside the run guard, and on the first morning after a deploy every mandate is
# due at once. Three a night clears a portfolio inside the weekly window
# (plan.PLAN_REFRESH_AFTER), and a mandate deferred today is simply still due
# tomorrow.
_PLANS_PER_SWEEP = 3

# How long the whole visibility stage may hold the sweep before it stops starting
# new measurements. The count above is not a time budget and cannot be one: a set
# is up to twenty-four questions times two providers times two model calls, each
# bounded only by ``ANALYZER_TIMEOUT`` — three mandates behind one slow provider is
# hours, and everything after this stage (the monthly draft, and the notification
# that tells Lucas what fired) waits behind it. Twenty minutes is comfortably more
# than a healthy measurement takes and far less than a morning; a mandate the
# budget defers is still due tomorrow, which is what makes deferring cheap.
_VISIBILITY_BUDGET = dt.timedelta(minutes=20)

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
    # Market signals stored this sweep: studies, regulatory dates and events
    # (see newspulse.market_sources). Defaulted for the same reason as above, and
    # reported separately from ``new_articles`` because a signal is deliberately
    # not an article — counting the two together is exactly the mistake the
    # separate table exists to prevent.
    signals_written: int = 0


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


def _refresh_field_verdict(
    session: Session, client: Client, *, fetch: FetchFeed, now: dt.datetime
) -> None:
    """Measure whether this mandate's industry term is one the press writes.

    Here rather than on the market page, for the reason ``profile_checked_at``
    is on the client rather than in the web process: the answer costs a live
    search per term, and a page render is the one place that cost cannot be
    paid. The page reads what this leaves behind (see
    ``web.routes.client._field_gap``).

    Asked at most every :data:`_FIELD_RECHECK`, and never for a mandate with no
    term to measure or on an installation with the search switched off — the
    probe *is* a search, and there would be nothing for the page to explain. A
    probe that could not reach the search leaves the answer ``None`` rather than
    ``False``: the stamp is still written, so an outage does not make every sweep
    re-probe, but nothing is claimed about the term — an unreachable search is
    not evidence that nobody writes a word.

    Not reached at all for a mandate that has muted every class (see
    :func:`_sweep_market`), which has one consequence worth naming: such a
    mandate's verdict stays where it was until the *next* sweep after it unmutes
    something, so the first page load after unmuting can only say nothing. That
    is the right way round — an unmeasured term claims nothing rather than
    claiming the wrong thing — and it heals itself the following morning.
    """
    if not config.GOOGLE_NEWS_ENABLED or not gnews.context_terms(client):
        return
    checked = client.field_checked_at
    if checked is not None and now - checked < _FIELD_RECHECK:
        return
    try:
        client.field_usable = industry.field_is_usable(client, fetch=fetch, now=now)
    except Exception as exc:  # noqa: BLE001 — a probe must not cost the sweep
        _log.warning("industry probe for %r failed: %s", client.name, exc)
        client.field_usable = None
    client.field_checked_at = now
    session.commit()


def _sweep_market(
    session: Session,
    clients: Sequence[Client],
    since: dt.datetime,
    fetch: FetchFeed,
    now: dt.datetime,
) -> tuple[int, list[str]]:
    """Fetch the three market classes for every mandate. One guard per class.

    Per class rather than per sweep, because the three are independent sources of
    independent things: the regulatory calendar going dark says nothing about
    whether the institutes published a study this morning, and a shared boundary
    would let one dead feed take the other two down with it.

    Louder than the news sweep on purpose. A feed among forty being unreachable is
    a WARNING (:func:`newspulse.ingest.fetch_feed`); a *class* being unreachable is
    an ERROR, because a class has one or two sources and an empty regulatory
    calendar is indistinguishable from a quiet fortnight — the one thing a forward
    calendar must never be wrong about.

    Competitors are skipped for the reason the topic radar skips them: a yardstick
    is tracked to compare its share of the conversation, and nobody reads it a
    market page. A class a mandate has muted is skipped for that mandate alone,
    and a mandate that has muted all three is skipped before any work is done for
    it at all.

    This is also where the industry term is measured (:func:`_refresh_field_verdict`),
    because the market page is what reads the answer and a page render is the one
    place a live search must not happen.

    Returns ``(signals written, what failed)``. The failures are returned rather
    than appended to the run's list in passing, because the caller has to do
    something with them beyond logging: the ``runs`` row is already committed by
    the time this runs, and a class that has been dark for a week must not keep
    showing as a green run.
    """
    written = 0
    errors: list[str] = []
    active = market_sources.fetchers(fetch=fetch)
    for client in clients:
        if client.is_competitor:
            continue
        # A mandate that has switched off every class is skipped whole, before the
        # dedup set is even built and before the term is measured. "Not fetched,
        # not merely hidden" has to be true of the work done for a class as well
        # as of its feeds — and the verdict below is only ever read above those
        # sections, which this mandate does not render.
        if all(client.mutes_signal(fetcher.kind) for fetcher in active):
            continue
        _refresh_field_verdict(session, client, fetch=fetch, now=now)
        try:
            seen = market_sources.already_seen(session, client, now=now)
        except Exception as exc:  # noqa: BLE001 — one mandate must not cost the rest
            _log.error("market dedup set for %r failed: %s", client.name, exc)
            errors.append(f"market {client.name!r}: {exc}")
            session.rollback()
            continue
        for fetcher in active:
            # A muted class is not fetched, not merely hidden. Unlike a muted
            # category there is no count and no report to stay honest to, so
            # asking a dozen sources every morning for a page this mandate has
            # switched off would buy nothing. See ``Client.mutes_signal``.
            if client.mutes_signal(fetcher.kind):
                continue
            try:
                drafts = fetcher.collect(client, since=since, now=now)
                written += len(
                    market_sources.store(session, client, drafts, seen=seen, now=now)
                )
            except Exception as exc:  # noqa: BLE001 — per-class fault boundary
                _log.error(
                    "market class %s for %r failed: %s; storing nothing for it",
                    fetcher.kind.value,
                    client.name,
                    exc,
                )
                errors.append(f"market {fetcher.kind.value} for {client.name!r}: {exc}")
                # A caught exception is not a clean session: ``store`` writes, so a
                # failed flush would otherwise leave the transaction pending and
                # kill the next class with an error about this one.
                session.rollback()
    return written, errors


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
    session: Session,
    clients: Sequence[Client],
    started: dt.datetime,
    window: dt.timedelta = _BACKFILL_WINDOW,
) -> list[tuple[Article, Client]]:
    """Re-match recently stored articles against the active clients (self-healing pass).

    A transient analyzer outage leaves an article stored with no analysis; if the story
    then drops out of its feed, this run's fetch never surfaces it again. Re-matching
    the articles fetched within ``window`` recovers those pairs so a later
    run analyses them once the analyzer is healthy again. Bounded by the window so the
    scan stays proportional to recent history, not the whole archive.

    ``window`` defaults to :data:`_BACKFILL_WINDOW` — the daily sweep's week. The
    crisis reading passes its own, much shorter one: it runs up to twelve times an
    hour, and a week-wide re-match on that cadence would re-read the whole recent
    archive to recover an analysis the next morning's sweep will recover anyway."""
    cutoff = started - window
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
            # Same order and the same reason as _refresh_impulses: the log line
            # reads client.name, which on an expired attribute is a query.
            session.rollback()
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
            # First, before anything else in this handler. A caught exception is
            # not a clean session, and every line below reaches for one:
            # ``client.name`` is a SELECT on an expired attribute and ``_note``
            # commits. Both raise PendingRollbackError on a poisoned session,
            # out of the handler, past a runs row already written as ok.
            session.rollback()
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


def _measure_visibility(
    session: Session, clients: Sequence[Client], *, now: dt.datetime
) -> int:
    """Measure the mandates whose weekly window has come round. Never fails the sweep.

    The measurement has no rhythm of its own — nothing in this tool does except
    the morning sweep — so it rides here, and :func:`newspulse.visibility.due`
    decides per mandate whether the window is open. A mandate with no accepted
    question is not due and costs no call: the question set is proposed and
    accepted by a person, and until somebody has, there is nothing to measure.

    Bounded twice, by :data:`_VISIBILITY_PER_SWEEP` and by
    :data:`_VISIBILITY_BUDGET`, because the count alone bounds calls and not
    minutes. Whatever either bound defers is still due tomorrow, and the stages
    behind this one — the monthly draft — get their morning back.

    Failures are logged and nothing is appended to the run's errors, exactly as
    :func:`_generate_angles` behaves and for the same reason: a missed measurement
    is a missing figure on one tab, not a broken morning, and marking the sweep
    ``partial`` for it would both misreport the coverage pipeline and re-open the
    coverage watermark over something that has nothing to do with coverage. The
    failure that *is* recorded is recorded where it belongs — a provider that
    errored lands in ``providers_failed`` on the visibility run row, which is what
    lets the page say "nicht gemessen" instead of "nicht genannt".
    """
    if not config.VISIBILITY_ENABLED:
        return 0
    measured = 0
    deferred = 0
    # Monotonic rather than the sweep's ``now``: this bounds how long the stage
    # runs, which is a different question from which mandates the window says are
    # due, and a clock the caller injects would answer the wrong one.
    until = time.monotonic() + _VISIBILITY_BUDGET.total_seconds()
    for client in clients:
        # A yardstick is tracked to compare its share of the conversation; nobody
        # reports its AI visibility, and DEC-3 gives it no page to read one on.
        if client.is_competitor:
            continue
        # Read before anything is asked, and used in the fault handler below: a
        # caught exception may have left the session unusable, and a rollback
        # expires every loaded attribute, so reaching for ``client.name`` down
        # there is a fresh SELECT — on exactly the connection that just failed.
        name = client.name
        try:
            if not visibility.due(session, client, now=now):
                continue
            if measured >= _VISIBILITY_PER_SWEEP or time.monotonic() >= until:
                deferred += 1
                continue
            # ``measure`` hands back an existing run rather than a new one when
            # another process has one in flight. That is not a measurement this
            # sweep made: counting it would spend a slot on work nobody did and
            # log answers this sweep never asked for.
            before = visibility.latest_run(session, client)
            before_id = None if before is None else before.id
            run = visibility.measure(session, client, now=now)
        except Exception:  # noqa: BLE001 — a measurement is not worth a failed sweep
            # First, before the log line: a caught exception is not a clean
            # session, and a query on an expired attribute would raise
            # PendingRollbackError straight out of this handler — past a runs row
            # already written as ok.
            session.rollback()
            _log.exception("the visibility measurement for %r failed; skipping", name)
            continue
        # ``before_id`` is None where the mandate had no run at all, and a fresh
        # run is never that: comparing against None would drop the first
        # measurement a mandate ever gets.
        if run is None or (before_id is not None and run.id == before_id):
            continue
        measured += 1
        _log.info(
            "visibility measured for %r: %d answer(s)%s",
            name,
            len(run.answers),
            f", no answer from {', '.join(run.providers_failed)}"
            if run.providers_failed
            else "",
        )
    if deferred:
        _log.info(
            "%d further visibility measurement(s) deferred to a later sweep "
            "(cap %d per run, budget %s)",
            deferred,
            _VISIBILITY_PER_SWEEP,
            _VISIBILITY_BUDGET,
        )
    return measured


def _recompute_plans(
    session: Session, clients: Sequence[Client], *, now: dt.datetime
) -> int:
    """Recompute the editorial plans whose weekly window has come round.

    The plan has no rhythm of its own, so it rides the sweep the way the
    visibility measurement does, and with the same three properties: per-mandate
    ``plan.due`` decides whether anything happens at all, a per-sweep cap bounds
    the model calls, and every mandate sits inside its own fault boundary — a
    missing plan is a stale tab, not a broken morning, so nothing here reaches
    the run's errors.

    A mandate with no evidenced candidate costs no model call inside
    :func:`newspulse.plan.recompute`, and — the part a cap cannot give — a
    recompute never touches a hook a person has decided on, so running this
    every week throws no work away.
    """
    recomputed = 0
    deferred = 0
    for client in clients:
        # A yardstick is tracked to compare its share of the conversation;
        # nobody plans its months, and DEC-5 gives it no page to read one on.
        if client.is_competitor:
            continue
        name = client.name
        try:
            if not plan.due(session, client, now=now):
                continue
            if recomputed >= _PLANS_PER_SWEEP:
                deferred += 1
                continue
            plan.recompute(session, client, now=now)
        except Exception:  # noqa: BLE001 — a plan is not worth a failed sweep
            # First, before the log line: a caught exception is not a clean
            # session, and a query on an expired attribute would raise
            # PendingRollbackError straight out of this handler.
            session.rollback()
            _log.exception("the plan recompute for %r failed; skipping", name)
            continue
        recomputed += 1
    if deferred:
        _log.info(
            "%d further plan recompute(s) deferred to a later sweep (cap %d per run)",
            deferred,
            _PLANS_PER_SWEEP,
        )
    return recomputed


def _draft_reports(
    session: Session,
    clients: Sequence[Client],
    *,
    now: dt.datetime,
    generate: report.Generate | None = None,
) -> int:
    """Draft last month's report for every mandate that has none yet (DEC-3 B).

    "Zum Stichtag vorbereitet, nie verschickt": the work is done before it is
    needed and nothing goes anywhere without a release, which is how every other
    generator in this tool already behaves. A consultant with a jour fixe on the
    third opens the tab and finds the month already read, rather than doing it on
    the second, at night.

    Three bounds, and each is a different failure being kept out. The window
    (:data:`_REPORT_DRAFT_DAYS`) means a mandate whose generation keeps failing is
    retried for a week and not all month. The per-sweep cap
    (:data:`_REPORTS_PER_SWEEP`) means the first of the month does not hold the run
    guard for an hour while a portfolio is read one model call at a time; what is
    deferred is logged, because a cap nobody is told about reads as "everything was
    covered". And every mandate is inside its own fault boundary: a report is worth
    nothing if the price of it is a failed sweep, so a failure is logged, the
    transaction is rolled back, and the next mandate is tried.

    A released report is never touched — :func:`newspulse.report.findings` refuses
    before it costs a model call, and this never reaches one anyway, since a
    mandate with any report for the period is skipped.
    """
    local = now.astimezone(config.local_zone())
    if local.day > _REPORT_DRAFT_DAYS:
        return 0
    period = report.previous_month(now)
    drafted = 0
    deferred = 0
    for client in clients:
        # A competitor is tracked to compare its share of the conversation; nobody
        # writes it a report, so drafting one would spend a call on nothing.
        if client.is_competitor:
            continue
        if report.for_period(session, client.id, period) is not None:
            continue
        if drafted >= _REPORTS_PER_SWEEP:
            deferred += 1
            continue
        try:
            draft = (
                report.findings(session, client, period)
                if generate is None
                else report.findings(session, client, period, generate=generate)
            )
            report.store(session, client, draft)
            drafted += 1
            _log.info(
                "report drafted for %r: %d finding(s)%s",
                client.name,
                len(draft.findings),
                f" — {draft.note}" if draft.note else "",
            )
        except Exception:  # noqa: BLE001 — a report is not worth a failed sweep
            _log.exception("report draft for %r failed; skipping", client.name)
            # A caught exception is not a clean session: ``store`` writes, so a
            # failed flush leaves the transaction in ``PendingRollbackError`` and
            # every later statement in this sweep dies with it, after the run has
            # already been recorded as ok. The same rollback theme settling needs,
            # for the same reason.
            session.rollback()
    if deferred:
        _log.info(
            "%d further report(s) deferred to a later sweep (cap %d per run)",
            deferred,
            _REPORTS_PER_SWEEP,
        )
    return drafted


def _refresh_profiles(session: Session, now: dt.datetime) -> int:
    """Re-read the profiles that have earned a look. Never fails the sweep.

    The mandate profile decays quietly: a CEO leaves, and every generated text
    keeps naming them until a journalist mentions it. Nothing re-reads it today,
    so the rhythm has to come from here — the one thing that runs every morning
    without anybody remembering to.

    Bounded inside :func:`newspulse.profile_refresh.run` (a handful per run,
    oldest-due first) and guarded out here, in the same posture as the drafting
    steps above it: a stale profile is a problem for one mandate, a failed sweep
    is a problem for the whole portfolio, and the second must never be caused by
    the first. Nothing is appended to the run's errors for the same reason
    :func:`_generate_angles` appends nothing — marking the run ``partial`` would
    re-open the coverage watermark over something that has nothing to do with
    coverage.
    """
    try:
        return profile_refresh.run(session, now=now)
    except Exception:  # noqa: BLE001 — a profile refresh is not worth a failed sweep
        _log.exception("profile refresh failed; the sweep's own work stands")
        # A caught exception is not a clean session: an unflushed write would
        # leave the transaction in PendingRollbackError and take the notification
        # below down with it, after the run was already recorded ok.
        session.rollback()
        return 0


def _analysis_targets(
    session: Session,
    candidates: Sequence[Candidate],
    clients: Sequence[Client],
    started: dt.datetime,
    backfill_window: dt.timedelta = _BACKFILL_WINDOW,
) -> list[tuple[Client, list[Article]]]:
    """Every (client, articles) group that still needs analysis this run.

    Unions two sources so a stored-but-un-analysed story self-heals however it was
    lost: this run's candidate pairs resolved to their stored Article (recovers a
    distinct match carried only on a collapsed copy, and re-analyses a still-in-feed
    story whose analysis failed earlier), plus a backfill re-match of recently stored
    articles (recovers a story that has since left its feed). Pairs already analysed are
    dropped, so a re-run adds nothing.

    ``backfill_window`` is how far that second source reaches back; see
    :func:`_backfill_pairs` for why the crisis reading narrows it."""
    pairs = _candidate_pairs(session, candidates)
    pairs.extend(_backfill_pairs(session, clients, started, backfill_window))
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


def _record_late_failure(
    session: Session, run: Run, errors: list[str], reported: Sequence[str]
) -> None:
    """Fold a post-finalize stage's failures into the run row already written.

    The ``runs`` row is committed before the mailbox is read or the market classes
    are fetched — deliberately, so somebody else's dead service cannot cost the
    day's coverage — which leaves it stating ``ok`` for a sweep whose last steps
    failed. Amended here rather than by moving those stages in front of the
    finalize: the row is the thing that must survive, and a second small write is
    cheaper than putting it behind a network call.

    Downgraded to ``partial`` and never to ``failed``: the acceptance criteria are
    that an unreachable mailbox does not fail the daily sweep and that a dark
    market class leaves the news sweep alone, and neither does — every stored row
    stands, nothing is rolled back, and the sweep's own work is untouched. A run
    that already failed keeps that status; there is nothing worse to say about it.
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
    is folded into the run's own errors by :func:`_record_late_failure`, so a
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
        _record_late_failure(session, run, errors, [f"mail sync: {exc}"])
        return 0
    _record_late_failure(session, run, errors, report.errors)
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


@dataclass(slots=True)
class _PostRun:
    """What the stages after the ``runs`` row is written contribute to the report.

    Counters only, and deliberately no status: the status lives on the ``run`` row
    those stages may have downgraded, and reading it back off the row is what keeps
    the report and the stored row saying the same thing.
    """

    replies: int = 0
    signals: int = 0
    angles: int = 0
    profiles: int = 0
    #: Mandates measured for KI-Sichtbarkeit this sweep. Zero on six mornings out
    #: of seven by design: the window is weekly, not daily.
    visibility: int = 0
    #: Editorial plans recomputed this sweep. Weekly per mandate, like the
    #: measurement above, and zero most mornings for the same reason.
    plans: int = 0
    #: Reputation readings written this sweep (RIS-01). Unlike the two above it
    #: this is daily and should equal the mandate count on a healthy morning —
    #: a number below it says a mandate's coverage could not be counted.
    readings: int = 0
    #: Signals the model attached to open issues this sweep (RIS-02, DEC-4).
    #: Zero on most mornings, because most mornings no mandate has an open
    #: issue with fresh coverage clustering into it.
    issue_links: int = 0


def _settle_themes(session: Session, clients: Sequence[Client], fetch: FetchFeed) -> int:
    """Give the mandates that still have no radar one, up to the per-sweep cap.

    A mandate with no themes has no radar, and every downstream feature reads off
    that radar. It was only ever filled in at onboarding, so every mandate created
    before that existed sat permanently in the state the onboarding step prevents
    — reported three times as "hier wird immer noch kein Impuls angezeigt".
    Self-limiting: the call returns immediately once a radar is in place.
    """
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
            # A caught exception is not a clean session. ``settle`` writes, so a
            # failed flush leaves the transaction in ``PendingRollbackError`` and
            # every later statement in this block dies with it — after
            # ``_finalize_run`` has already recorded the sweep as ok. Reproduced:
            # the header shows a green run with zero errors while the drafting,
            # the archive linking and the notification were all skipped.
            session.rollback()
    return settled


def _settle_industries(session: Session, clients: Sequence[Client], fetch: FetchFeed) -> int:
    """Give every company that still has no industry one, up to the per-sweep cap.

    The same class of bug :func:`_settle_themes` fixes, and found the same way:
    the step existed only in the onboarding route, so it ran once down one path.
    Anything created any other way — imported from a spreadsheet, created before
    the step existed, or accepted from a competitor proposal — kept an empty
    field forever.

    Competitors included, and that is the whole point of adding this. The
    analyzer decides relevance from name, industry, aliases and alert topics, so
    a company with an empty industry is judged on its name alone. "G-20", a
    crypto market maker at g20.group, was created from a proposal as a bare name
    and then handed an article about the 2017 Hamburg G20 summit riots as
    coverage of itself, marked relevant with a score of 7. A yardstick needs no
    radar and no pitch — it does need to be the right company.

    Self-limiting: :func:`newspulse.industry.settle` returns immediately once a
    term is in place, so this may run over the whole portfolio every morning.
    Capped all the same, because a *failure* is not self-limiting: a term the
    press never writes is never usable, and without the cap a portfolio of such
    companies would spend the whole budget re-measuring them every sweep.
    """
    settled = 0
    for client in clients:
        if settled >= _SETTLE_PER_SWEEP:
            break
        try:
            if industry.settle(session, client, fetch=fetch):
                settled += 1
        except Exception:  # noqa: BLE001 — an industry term is not worth a failed sweep
            _log.exception("industry settling for %r failed", client.name)
            # Same reason as _settle_themes: ``settle`` writes, and a caught
            # exception over an unflushed write leaves the transaction in
            # PendingRollbackError, taking every later stage down with it after
            # the run was already recorded ok.
            session.rollback()
    return settled


def _read_reputation(
    session: Session, clients: Sequence[Client], *, now: dt.datetime
) -> int:
    """One reading per mandate, for today. Never fails the sweep (RIS-01).

    Daily rather than windowed, unlike the measurement and the plan above it:
    the reading *is* the day, and a morning without one leaves a hole in the
    series that the direction and the mandate's own median both read over.

    Cheap enough to be unconditional. Every input is already in stored rows —
    DEC-2 locked "gerechnet aus gespeicherten Zeilen" — so a reading is a
    handful of queries and no model call, which is what lets it run for every
    mandate every morning rather than for a few of them on a rota.

    The per-mandate fault boundary lives in :func:`newspulse.reputation.sweep`;
    this second one is for the failure *outside* that loop, so an exception here
    cannot end a sweep whose ``runs`` row is already written as ok.
    """
    try:
        written = reputation.sweep(session, list(clients), now=now)
    except Exception:  # noqa: BLE001 — a reading is never worth a failed sweep
        session.rollback()
        _log.exception("the reputation readings failed; the sweep stands")
        return 0
    if written:
        _log.info("wrote %d reputation reading(s)", written)
    return written


def _link_issue_signals(
    session: Session, clients: Sequence[Client], *, now: dt.datetime
) -> int:
    """Attach the day's new signals to open issues (RIS-02, DEC-4). Never fails
    the sweep.

    One fault boundary per mandate: a broken verdict for one issue must not
    cost the other mandates their attachments, and never the sweep its morning.
    A mandate with no open issue costs one query and no model call — see
    :func:`newspulse.issues.link_signals` — which is what makes running it
    unconditionally affordable.

    Yardsticks are skipped for the same reason the reputation sweep skips them:
    a competitor is tracked to compare coverage, and no register is kept on it.
    """
    attached = 0
    for client in clients:
        if client.is_competitor:
            continue
        name = client.name
        try:
            attached += issues.link_signals(session, client, now=now)
        except Exception:  # noqa: BLE001 — a linking pass is never worth a failed sweep
            session.rollback()
            _log.exception("issue linking for %r failed; skipping", name)
            continue
    if attached:
        _log.info("attached %d signal(s) to open issues", attached)
    return attached


def _post_run(
    session: Session,
    run: Run,
    clients: Sequence[Client],
    topic_pairs: Sequence[Candidate],
    errors: list[str],
    *,
    since: dt.datetime,
    started: dt.datetime,
    fetch: FetchFeed,
    now_fn: Callable[[], dt.datetime],
) -> _PostRun:
    """Everything the sweep does once its own row is safely committed.

    Two rules govern the stages here. The mailbox and the market run
    unconditionally, because they are their own sources of their own things and
    whether this morning's *news* feeds answered says nothing about whether a
    journalist replied or the regulatory calendar moved. Everything behind them
    runs only if the sweep itself came through — pitching a positioning message
    off a half-fetched radar would put a confident text in front of the reader on
    the strength of partial data.

    Both of the unconditional stages report what failed into the ``runs`` row that
    is already written, so a source that has been dark for a week shows as a
    partial run rather than a green one with a line in a log nobody tails.

    Every stage sits inside its own fault boundary: the run row is written by the
    time this is called, so an exception escaping here would leave a green run
    beside work that silently did not happen.
    """
    outcome = _PostRun()
    # The mailbox, once a day, with the sweep: reading the replies to letters that
    # went out weeks ago has nothing to do with whether this morning's feeds
    # answered, and a journalist's answer is the one thing in this tool nobody can
    # re-fetch later by pressing a button.
    outcome.replies = _sync_mailbox(session, run, errors, now=now_fn())
    # The three market classes, on the same footing as the mailbox above: a failed
    # news sweep must not also cost a consultation that closes in five weeks.
    outcome.signals, market_errors = _sweep_market(
        session, clients, since, fetch, now_fn()
    )
    _record_late_failure(session, run, errors, market_errors)
    if run.status is RunStatus.FAILED:
        return outcome
    # Before the themes, because the radar scopes with the industry: a mandate
    # that gets both in one sweep gets a radar built on the term rather than one
    # built without it and left that way until somebody notices.
    _settle_industries(session, clients, fetch)
    _settle_themes(session, clients, fetch)
    # The archive first: the registry feeds fetched the trade press this morning and
    # nothing linked it to the mandates whose field it is. Doing this before drafting
    # means today's material is available to today's draft rather than tomorrow's.
    linked = link_archive_to_themes(session, clients, started - IMPULSE_LOOKBACK, started)
    if linked:
        _log.info("linked %d archived article(s) as market material", linked)
    outcome.angles = _generate_angles(session, topic_pairs, errors)
    # And then top up the mandates the radar did not move today. Drafting only from
    # fresh material meant a quiet fortnight showed an empty Impulse column for a
    # fortnight — for exactly the mandate whose consultant most needs something to
    # say.
    outcome.angles += _refresh_impulses(session, clients, errors, now=now_fn())
    outcome.profiles = _refresh_profiles(session, now_fn())
    # The weekly measurement, on the same footing as the profile refresh above it:
    # bounded per sweep, inside its own fault boundary, and reporting nothing into
    # the run's errors.
    outcome.visibility = _measure_visibility(session, clients, now=now_fn())
    # The editorial plan, after the market sweep above has stored this morning's
    # signals so a consultation that arrived today is in tonight's plan rather
    # than next week's. Weekly per mandate, capped per sweep, and never touching
    # a hook a person has decided on — see _recompute_plans.
    outcome.plans = _recompute_plans(session, clients, now=now_fn())
    # After the market sweep and the archive linking, so a signal that arrived
    # this morning is a candidate this morning; before the reading, so the
    # register the reading's Issue floor looks at is today's. Costs a model call
    # only for a mandate with an open issue and fresh clustering coverage.
    outcome.issue_links = _link_issue_signals(session, clients, now=now_fn())
    # After everything that could still add coverage or an analysis to today, so
    # the reading counts the morning the sweep actually brought in rather than
    # the one it started with. Daily, for every mandate, and cheap: no model call
    # and no fetch, only stored rows.
    outcome.readings = _read_reputation(session, clients, now=now_fn())
    # Last, and outside everything above: the monthly report reads a period that
    # has already ended, so it needs nothing this sweep fetched, and putting it
    # here means a failure in it cannot cost the sweep any of the work that came
    # before. The boundary is doubled — per mandate inside, and once here —
    # because the period arithmetic and the client list are outside the
    # per-mandate try and must not be able to end the run either.
    try:
        _draft_reports(session, clients, now=now_fn())
    except Exception:  # noqa: BLE001 — a report is never worth a failed sweep
        _log.exception("the monthly report draft failed; the sweep stands")
        session.rollback()
    return outcome


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
    post = _post_run(
        session,
        run,
        clients,
        topic_pairs,
        errors,
        since=since,
        started=started,
        fetch=fetch,
        now_fn=now_fn,
    )
    # The run row may have been downgraded to partial by an unreadable mailbox or a
    # dark market class; the report has to say the same thing the stored row does.
    status = run.status
    _log.info(
        "run done: status=%s, %d new article(s), %d analysis(es), %d draft(s), "
        "%d market signal(s), %d profile(s), %d visibility measurement(s), "
        "%d plan(s), %d repl(y/ies), %d reputation reading(s), "
        "%d issue signal(s), %d error(s)",
        status.value,
        new_articles,
        analyses_written,
        post.angles,
        post.signals,
        # On the run's own line because a refresh that quietly returns zero for
        # weeks looks exactly like a portfolio where nothing was due, and the
        # difference is only visible here. The same holds, harder, for a weekly
        # measurement that fires on one morning in seven.
        post.profiles,
        post.visibility,
        post.plans,
        post.replies,
        # Daily, unlike the two above it: a number below the mandate count is
        # the only place a mandate whose coverage could not be counted shows up.
        post.readings,
        post.issue_links,
        len(errors),
    )
    # The run's data is committed; deliver any fired-alert notification now. This is
    # the wiring for AC #1 ("after a run, if any alerts fired, a notification ... is
    # delivered") — read-only and fault-isolated, so it can't roll the sweep back.
    # After the post-run stages rather than before them, because the row a
    # notification is about may still be downgraded to ``partial`` there by an
    # unreadable mailbox or a dark market class. Moving it earlier is a change to
    # every story's sweep and belongs to whichever one wants it.
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
        angles_written=post.angles,
        signals_written=post.signals,
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


# --- The light run: the fast lane's every-three-hours pass (UHR-04, DEC-6 A) ---


@dataclass(frozen=True, slots=True)
class NewsjackRun:
    """What one light run produced. Counters only — it writes no ``runs`` row."""

    mandates: int
    opportunities: int
    rejected: int
    errors: list[str]


def run_newsjack(
    session: Session,
    *,
    fetch: FetchFeed = fetch_feed,
    invoke=None,
    now: Callable[[], dt.datetime] | None = None,
    notify_config: notify.NotifyConfig | None = None,
) -> NewsjackRun:
    """One pass of the fast lane: refresh each active mandate's topic radar and
    weigh what it holds. Deliberately poor, and the poverty is the point.

    It reads the topic radar of the active mandates and nothing else: no
    registry feeds, no client name searches, no market classes. It analyses no
    client coverage — the radar's material is stored unanalysed, exactly as the
    daily sweep stores it — and it writes no profile data, no positioning
    drafts, no impulse notes. A model call happens only inside
    :func:`newspulse.newsjack.scan`, and only for a story that has crossed the
    media threshold, so the great majority of runs cost nothing (DEC-6).

    **No ``runs`` row**, for the reason :func:`backfill_client` and
    :func:`run_crisis` state and this inherits unchanged: ``_determine_since``
    takes the last successful run's start as the next sweep's watermark, so
    recording a radar-only pass as a run would tell tomorrow's sweep the whole
    portfolio had already been covered.

    Each mandate sits inside its own fault boundary: a dead radar feed or a
    failed scan is logged and reported, and the next mandate is tried.
    """
    now_fn = now or _utcnow
    errors: list[str] = []
    opportunities = 0
    rejected = 0
    mandates = 0
    found: list[notify.FoundOpportunity] = []
    for client in list_clients(session):
        # A yardstick is tracked to compare its share of the conversation;
        # nobody writes a contribution on its behalf, so weighing openings for
        # it would spend model calls on nothing.
        if client.is_competitor:
            continue
        mandates += 1
        # Read before the boundary: a rollback expires loaded attributes, and
        # the log line below must not query the connection that just failed.
        name = client.name
        try:
            refresh_radar(session, client, fetch=fetch, now=now_fn)
            stored = newsjack.scan(session, client, invoke=invoke, now=now_fn())
        except Exception as exc:  # noqa: BLE001 — per-mandate fault boundary
            session.rollback()
            _log.warning("newsjack pass for %r failed: %s; skipping", name, exc)
            errors.append(f"newsjack {name!r}: {exc}")
            continue
        fresh = [row for row in stored if row.standing is Standing.BELEGT]
        opportunities += len(fresh)
        rejected += len(stored) - len(fresh)
        # Collected for the notification below: only what THIS run stored, so
        # a standing opportunity is announced once and not on every tick.
        found.extend(
            notify.FoundOpportunity(
                client_name=name,
                headline=row.article.title,
                outlets=row.pickup_count,
                hours_left=max(
                    0,
                    int(
                        (row.window_ends_at - now_fn()).total_seconds() // 3600
                    ),
                ),
            )
            for row in fresh
        )
    # After everything is committed, like the daily sweep's _notify: read-only
    # from here on, never raising, so a broken channel cannot cost stored rows.
    # A run that found nothing sends nothing (UHR-05); the card on Heute is the
    # surface either way, this is only the tap on the shoulder DEC-6's ninety
    # minutes are about.
    notify.notify_opportunities(found, notify_config)
    _log.info(
        "newsjack run done: %d mandate(s), %d opportunit(y/ies), "
        "%d rejection(s), %d error(s)",
        mandates,
        opportunities,
        rejected,
        len(errors),
    )
    return NewsjackRun(
        mandates=mandates,
        opportunities=opportunities,
        rejected=rejected,
        errors=errors,
    )


# --- The tighter cadence: one mandate, its own sources, nothing else -----------

#: How far back one crisis reading looks. Bound to — not a copy of — the window a
#: crisis's story is read over, because a lookback shorter than that window would
#: leave the level counting coverage the reading never fetched. The cadence is
#: hourly and a search feed keeps listing what it listed an hour ago, so the
#: width costs one dedup filter and buys the pickups that arrived late.
_CRISIS_LOOKBACK = crisis.STORY_WINDOW


@dataclass(frozen=True, slots=True)
class _CrisisFetch:
    """What the network half of one crisis reading brought back.

    Kept apart from the analysis half so the caller can look at the crisis row
    again in between: the fetch is the long part, and the model calls are the
    expensive one.
    """

    candidates: list[Candidate]
    stored: int
    feeds: int
    feeds_ok: int


def _fetch_crisis_sources(
    session: Session,
    client: Client,
    *,
    started: dt.datetime,
    fetch: FetchFeed,
    errors: list[str],
) -> _CrisisFetch:
    """Read this one mandate's own feeds and store whatever is new. No model."""
    feeds = gnews.client_feeds([client])
    if not feeds:
        return _CrisisFetch(candidates=[], stored=0, feeds=0, feeds_ok=0)
    items, feeds_ok = _fetch_all(
        feeds, started - _CRISIS_LOOKBACK, fetch, started, errors
    )
    # This mandate only. Matching against the whole portfolio would let a crisis
    # run store and analyse coverage for a mandate that is not in one.
    candidates = _match(items, [client], errors)
    known_urls, known_hashes = _load_known(session)
    kept = deduplicate(
        _distinct_items(candidates),
        known_urls=known_urls,
        known_title_hashes=known_hashes,
    )
    stored = _persist_articles(session, kept, started) if kept else []
    return _CrisisFetch(
        candidates=candidates,
        stored=len(stored),
        feeds=len(feeds),
        feeds_ok=feeds_ok,
    )


def _analyse_crisis_sources(
    session: Session,
    client: Client,
    read: _CrisisFetch,
    *,
    started: dt.datetime,
    analyzer: Analyzer | None,
    errors: list[str],
) -> int:
    """Analyse what this mandate still needs analysed. Returns how many were written.

    The analysis targets are resolved as the daily sweep resolves them
    (:func:`_analysis_targets`), and that is the point rather than an economy.
    Analysing only what this reading *stored* would leave a story that is in the
    archive without an analysis — the documented shape of a dropped analyzer
    batch — permanently invisible to the level: every later reading re-fetches
    it, dedup drops it as known, and the crisis stays under-graded until the next
    morning's sweep backfills it. During a crisis the level is the whole point of
    the number, so the reading self-heals on the same terms the sweep does.

    The self-heal reaches back exactly as far as the level can see
    (:data:`_CRISIS_LOOKBACK`) and no further. The sweep's week would re-match the
    whole recent archive up to twelve times an hour to recover an analysis that
    changes nothing about this crisis's level — and would hand the analyzer
    articles from outside the window the story is even counted over.

    It costs nothing on a quiet reading: :func:`_group_pairs` drops every pair
    that already carries an analysis, so an hour with no news resolves its
    candidates, finds them all analysed, and never reaches the model.
    """
    targets = _analysis_targets(
        session, read.candidates, [client], started, _CRISIS_LOOKBACK
    )
    if not targets:
        return 0
    resolved = analyzer or get_analyzer()
    analyses = 0
    for target_client, target_articles in targets:
        analyses += _analyze_and_persist(
            session, target_client, target_articles, resolved, errors
        )
    return analyses


@dataclass(frozen=True, slots=True)
class CrisisSweep:
    """What one crisis reading produced. Counters, and the level it left behind.

    ``feeds`` / ``feeds_ok`` are carried because a crisis reading writes no
    ``runs`` row: a feed that self-isolated to an empty list is otherwise
    indistinguishable from a quiet hour, and a crisis is the worst moment for
    those two to look the same.
    """

    articles: int
    analyses: int
    level: int
    errors: list[str]
    feeds: int = 0
    feeds_ok: int = 0

    @property
    def feeds_failed(self) -> int:
        """How many of this mandate's feeds returned nothing but a failure."""
        return max(0, self.feeds - self.feeds_ok)


def run_crisis(
    session: Session,
    declared: Crisis,
    *,
    analyzer: Analyzer | None = None,
    fetch: FetchFeed = fetch_feed,
    now: Callable[[], dt.datetime] | None = None,
) -> CrisisSweep:
    """Re-read one mandate's own sources, because it is in a declared crisis.

    This is the *only* thing a crisis changes about how the tool runs, and it is
    deliberately the narrowest run in the codebase. It reads the crisis mandate's
    own search feeds and nothing else — not the registry, not the topic radar,
    not another mandate — and it stores coverage and analyses and nothing else.
    No positioning draft is written, no profile field is touched, no market class
    is fetched. A crisis is not a reason to spend a model call on next month's
    impulse.

    **No ``runs`` row**, for the reason :func:`backfill_client` states and this
    inherits unchanged: ``_determine_since`` takes the last successful run's
    start as the next sweep's watermark, so recording an hourly single-mandate
    reading as a run would tell tomorrow's sweep that the whole portfolio had
    already been covered.

    Crash-safety is on the row rather than in the caller. ``last_swept_at`` is
    stamped and committed *before* the fetch, so a reading that dies halfway
    leaves an open crisis that is simply not due again yet — never a hung crisis,
    and never a second reading racing the first one back.

    A stand-down is checked three times, and that is not belt-and-braces. The
    reading is *minutes* long — feed fetches, then a model call per batch — and
    all three of the things it does after "stand down" is pressed are wrong: the
    fetch is work nobody asked for, the analyzer batch is money, and the regrade
    writes new counts onto a row whose level is supposed to be what it was at the
    moment it ended. Each check is read from the table
    (:func:`newspulse.crisis.still_open`), because the object this was handed was
    loaded before any of it.
    """
    now_fn = now or _utcnow
    started = now_fn()
    if not crisis.still_open(session, declared):
        # Somebody stood the crisis down between the scheduler reading its due
        # list and this call. A closed crisis is a finished document: its level is
        # what it was at the moment it ended, and a reading would rewrite that.
        _log.info("crisis %d was closed before its reading; leaving it as it is", declared.id)
        return CrisisSweep(articles=0, analyses=0, level=declared.level, errors=[])
    client = declared.client or session.get(Client, declared.client_id)
    if not client.active:
        # The other half of the same race, and the same answer: ``active`` is the
        # kill switch for a mandate, so a crisis on a deactivated one is read no
        # further. ``crisis.due`` already filters these out; this catches the
        # mandate deactivated between that read and this call.
        _log.info("crisis %d belongs to a deactivated mandate; not reading", declared.id)
        return CrisisSweep(articles=0, analyses=0, level=declared.level, errors=[])
    # Before the reading, and committed. See the docstring above.
    crisis.mark_swept(session, declared, now=started)

    errors: list[str] = []
    read = _fetch_crisis_sources(
        session, client, started=started, fetch=fetch, errors=errors
    )
    if not crisis.still_open(session, declared):
        # Stood down while the feeds were out. Stop before the analyzer: the
        # coverage is stored either way, but the model calls would be spent on a
        # crisis that no longer exists.
        _log.info(
            "crisis %d was closed during its reading; not analysing and not regrading",
            declared.id,
        )
        return _crisis_result(read, analyses=0, level=declared.level, errors=errors)
    analyses = _analyse_crisis_sources(
        session, client, read, started=started, analyzer=analyzer, errors=errors
    )
    if not crisis.still_open(session, declared):
        _log.info("crisis %d was closed during its reading; not regrading", declared.id)
        return _crisis_result(read, analyses=analyses, level=declared.level, errors=errors)

    # Counted from what is stored now, including whatever the reading just added.
    # Still arithmetic and still no model — see :func:`newspulse.crisis.severity`.
    graded = crisis.regrade(session, declared)
    _log.info(
        "crisis reading for %r: %d new article(s), %d analysis(es), level %d "
        "(%d outlet(s), %d/%d negative), %d/%d feed(s) ok, %d error(s)",
        client.name,
        read.stored,
        analyses,
        graded.level,
        graded.outlets,
        graded.negative,
        graded.articles,
        read.feeds_ok,
        read.feeds,
        len(errors),
    )
    return _crisis_result(read, analyses=analyses, level=graded.level, errors=errors)


def _crisis_result(
    read: _CrisisFetch, *, analyses: int, level: int, errors: list[str]
) -> CrisisSweep:
    """One reading's counters, carrying the feed health the fetch half measured."""
    return CrisisSweep(
        articles=read.stored,
        analyses=analyses,
        level=level,
        errors=errors,
        feeds=read.feeds,
        feeds_ok=read.feeds_ok,
    )


__all__ = [
    "CrisisSweep",
    "IMPULSE_LOOKBACK",
    "NewsjackRun",
    "ONBOARDING_ARTICLES",
    "draft_impulse",
    "RunReport",
    "backfill_client",
    "lookback_since",
    "run",
    "run_crisis",
    "run_newsjack",
    "setup_logging",
]
