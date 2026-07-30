"""The Today view: today's coverage across all clients (DEC-1 option C).

Two-pane triage layout — a narrow left rail of alert cards (the must-see) beside
a full ranked feed on the right. The page defaults to the current local day with
a date picker for any prior day; a day with no coverage renders a clean empty
state rather than an error.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import angles, config
from ...models import Analysis, Article, Client, Run
from ...outlets import tier_for
from ...stories import cluster
from ..app import get_db, templates

router = APIRouter()

# The date-picker exchange format; also how a prior day is passed as ?date=.
_DATE_FORMAT = "%Y-%m-%d"

# Only surface analyses the analyzer judged relevant to the client. A relevance
# of 0 means "this story does not concern this client" — a row the future
# analyzer may still persist for a non-matching (article, client) pair — so it
# must never appear as coverage. Importance ordering is applied on top of this
# gate. (The analyzer contract is not yet built; this pins the include rule.)
_MIN_RELEVANCE = 1

# Above this many mandates the filter strip becomes a dropdown: a row of cards
# is faster to hit while you can still take it in at a glance, and unusable once
# it wraps to three lines.
_CLIENT_CARD_LIMIT = 10


@dataclass(frozen=True, slots=True)
class TodayItem:
    """One rendered coverage row (one article as it concerns one client)."""

    # The analysis row this came from: triage state is per (article, client), so
    # a workflow action targets the analysis, not the article.
    analysis_id: int
    triage_state: str
    tonality: str
    client_id: int
    author: str | None
    headline: str
    url: str
    source: str
    published_at: dt.datetime
    summary: str | None
    category: str
    importance: int
    client_name: str
    is_alert: bool


@dataclass(frozen=True, slots=True)
class AngleView:
    """One drafted positioning message as the third column renders it."""

    id: int
    client_id: int
    client_name: str
    subject: str
    message: str
    context: str
    credibility: str
    thesis: str
    overclaim: str
    statements: list[str]
    # The developments the draft was built on, so the reader can check it against
    # the coverage rather than take the text on trust.
    sources: list[tuple[str, str, str]]


@dataclass(frozen=True, slots=True)
class RunStatusView:
    """Header last-run status sourced from the latest ``runs`` row."""

    ran_at: dt.datetime
    is_running: bool
    status: str
    articles_checked: int
    feed_errors: int


def _local_tz() -> dt.tzinfo:
    """The reader's timezone — 'today' is the current day *there*.

    The configured display zone (:func:`config.local_zone`), not the host's: the
    host is a UTC container, and deriving the day window from it rolled the day
    over at 02:00 Berlin time. DST-aware, so bounding any requested day uses that
    day's own offset rather than today's.
    """
    return config.local_zone()


def _parse_day(raw: str | None) -> dt.date:
    """Resolve the requested day: a valid ``?date=YYYY-MM-DD`` or today (local).

    An unparseable value falls back to today rather than erroring, so a hand-typed
    or stale URL never 500s the dashboard.
    """
    if raw:
        try:
            return dt.datetime.strptime(raw, _DATE_FORMAT).date()
        except ValueError:
            pass
    return dt.datetime.now(_local_tz()).date()


def _day_bounds_utc(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """UTC [start, end) covering the given *local* calendar day."""
    tz = _local_tz()
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.UTC), end_local.astimezone(dt.UTC)


#: How far back the empty state looks when a day has nothing.
#: Ten days because that is the shortest backfill window the run offers, so the
#: number it reports is one the operator can actually act on.
_EMPTY_STATE_LOOKBACK_DAYS = 10


def _recent_coverage_count(
    session: Session,
    day: dt.date,
    days: int = _EMPTY_STATE_LOOKBACK_DAYS,
    client_id: int | None = None,
) -> int:
    """How much coverage sits in the ``days`` before ``day``, excluding ``day``.

    Only used to fill the empty state. German feeds publish a handful of relevant
    items per day per mandate, so a normal morning shows one or two rows and a
    quiet one shows none — and a bare "nothing for today" then reads as a broken
    tool rather than a quiet news day. It is the difference between an empty page
    and an empty page that says where the other 40 articles are.

    ``client_id`` scopes it to the mandate the page is filtered to. Without that
    the hint counted the whole portfolio while the reader was looking at one
    company: a page about a mandate with no coverage at all offered "40 Artikel in
    den letzten 10 Tagen", which were somebody else's, behind a link that dropped
    the filter on the way to the archive.
    """
    start = day - dt.timedelta(days=days)
    return int(
        session.execute(
            select(func.count())
            .select_from(Analysis)
            .join(Article, Article.id == Analysis.article_id)
            .join(Client, Client.id == Analysis.client_id)
            .where(
                *( [Analysis.client_id == client_id] if client_id is not None else [] ),
                Article.published_at >= dt.datetime.combine(start, dt.time.min, dt.UTC),
                Article.published_at < dt.datetime.combine(day, dt.time.min, dt.UTC),
                # The same relevance gate _fetch_items uses, not the is_relevant
                # column: the count has to describe the rows the archive will
                # actually show, or the hint promises coverage that isn't there.
                Analysis.relevance_score >= _MIN_RELEVANCE,
                # Mandates only: competitor coverage is context inside a client's
                # own view, and offering to navigate to it here would contradict
                # the rule that competitors are not listed as clients.
                Client.is_competitor.is_(False),
            )
        ).scalar_one()
    )


def _client_has_any_coverage(session: Session, client_id: int | None) -> bool:
    """Whether this mandate has ever had a piece of coverage stored.

    Separates "quiet day" from "nothing was ever found", which need opposite
    advice: the first points at the archive, the second at the configuration that
    is almost always the real cause.
    """
    if client_id is None:
        return False
    return bool(
        session.execute(
            select(Analysis.id)
            .where(
                Analysis.client_id == client_id,
                Analysis.relevance_score >= _MIN_RELEVANCE,
            )
            .limit(1)
        ).first()
    )


def _fetch_items(session: Session, day: dt.date) -> list[TodayItem]:
    """Coverage for ``day`` ordered for triage: alerts first, then importance desc.

    Ordering is ``is_alert`` desc so alerts surface above non-alerts (also shown
    in the left rail), then importance desc, with ``published_at`` desc as a
    stable tiebreak.

    Outlet tier (:mod:`newspulse.outlets`) breaks ties *within* an importance
    score, so among several 7/10 stories the FAZ piece sits above an automated
    share-price item. It only ever reorders: the displayed score stays the
    model's own, and no story is hidden or demoted out of view — weighting the
    score itself was measured on real coverage and lost genuine stories.

    Ranking is finished in Python rather than SQL because tier is a lookup over
    an editable table, not a column; a day's coverage is small enough that
    sorting it in memory costs less than denormalising it into the schema.
    """
    tz = _local_tz()
    start_utc, end_utc = _day_bounds_utc(day)
    stmt = (
        select(Analysis, Article, Client)
        .join(Article, Analysis.article_id == Article.id)
        .join(Client, Analysis.client_id == Client.id)
        .where(
            Article.published_at >= start_utc,
            Article.published_at < end_utc,
            Analysis.relevance_score >= _MIN_RELEVANCE,
            # Mandates only. A competitor is monitored so its volume can be
            # compared, not so it lands in the morning triage queue — coverage
            # of a rival is not work, and mixing the two makes the day look
            # busier than it is.
            Client.is_competitor.is_(False),
        )
        .order_by(
            Analysis.is_alert.desc(),
            Analysis.importance_score.desc(),
            Article.published_at.desc(),
        )
    )
    items = [
        TodayItem(
            analysis_id=analysis.id,
            triage_state=analysis.triage_state.value,
            tonality=analysis.tonality.value,
            client_id=client.id,
            author=article.author,
            headline=article.title,
            url=article.url,
            source=article.source,
            # Stored UTC, shown in local time — the page frames "today" locally.
            published_at=article.published_at.astimezone(tz),
            summary=analysis.summary,
            category=analysis.category.value,
            importance=analysis.importance_score,
            client_name=client.name,
            is_alert=analysis.is_alert,
        )
        for analysis, article, client in session.execute(stmt).all()
    ]
    # The SQL ORDER BY already settled alerts-first, importance, and the
    # published_at tiebreak. Re-sort to slot tier in *below* importance — a lower
    # tier number is the better outlet, so it is negated to sort descending with
    # the rest. Python's sort is stable, so the published_at tiebreak survives.
    items.sort(
        key=lambda item: (item.is_alert, item.importance, -tier_for(item.source)),
        reverse=True,
    )
    return items


def _fetch_angles(session: Session, day: dt.date) -> list[AngleView]:
    """The positioning drafts generated on ``day``, newest first.

    Dated by when the draft was *generated*, not by the coverage under it: it is a
    piece of the day's work, and it arrives the morning the market moved. Sources
    are resolved here rather than in the template so a draft citing an article that
    no longer exists renders without its citation instead of erroring.
    """
    start_utc, end_utc = _day_bounds_utc(day)
    drafts = angles.for_day(session, start_utc, end_utc)
    if not drafts:
        return []

    names = dict(session.execute(select(Client.id, Client.name)).all())
    wanted = {aid for draft in drafts for aid in draft.article_ids}
    articles = (
        {
            row.id: row
            for row in session.scalars(
                select(Article).where(Article.id.in_(wanted))
            ).all()
        }
        if wanted
        else {}
    )
    views: list[AngleView] = []
    for draft in drafts:
        cited = [articles[aid] for aid in draft.article_ids if aid in articles]
        views.append(
            AngleView(
                id=draft.id,
                client_id=draft.client_id,
                client_name=names.get(draft.client_id, "—"),
                subject=draft.subject,
                message=draft.message,
                context=draft.context,
                credibility=draft.credibility,
                thesis=draft.thesis,
                overclaim=draft.overclaim,
                statements=list(draft.statements),
                sources=[(a.title, a.source, a.url) for a in cited],
            )
        )
    return views


def _fetch_last_run(session: Session) -> RunStatusView | None:
    """The most recent run for the header, or None if the job never ran."""
    run: Run | None = session.execute(
        select(Run).order_by(Run.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        return None
    # Left in UTC as stored: base.html renders it through the de_time filter,
    # which applies the reader's zone (the same one the day window uses). A run
    # with no finished_at is still in progress.
    return RunStatusView(
        ran_at=run.finished_at or run.started_at,
        is_running=run.finished_at is None,
        status=run.status.value,
        articles_checked=run.articles_found,
        feed_errors=len(run.errors),
    )


@router.get("/", response_class=HTMLResponse)
def today_view(
    request: Request,
    date: str | None = None,
    category: str | None = None,
    client: int | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the Today view for the current local day (or ``?date=``).

    ``?category=`` narrows the day to one category. Filtering happens after the
    day is fetched, so the category dropdown can offer exactly the categories
    *this day* actually contains rather than the whole enum — an option that
    would return nothing is worse than no option.
    """
    day = _parse_day(date)
    all_items = _fetch_items(session, day)

    # Mandates only, and only those that exist — the filter strip is a way into
    # the day, not a client manager.
    mandates = list(
        session.scalars(
            select(Client)
            .where(Client.is_competitor.is_(False), Client.active.is_(True))
            .order_by(Client.name)
        ).all()
    )
    counts: dict[int, int] = {}
    for item in all_items:
        counts[item.client_id] = counts.get(item.client_id, 0) + 1

    # An unknown id shows the whole day rather than an unexplainable empty page,
    # the same posture as the category filter.
    selected_client = client if client in {m.id for m in mandates} else None
    if selected_client is not None:
        all_items = [i for i in all_items if i.client_id == selected_client]
    # Ordered by the day's own ranking, so the dropdown lists the busiest
    # categories in the order the reader already sees them.
    present = list(dict.fromkeys(item.category for item in all_items))

    selected = (category or "").strip()
    if selected not in present:
        # An unknown or stale category degrades to "no filter" rather than an
        # empty page a reader cannot explain.
        selected = ""
    items = [i for i in all_items if i.category == selected] if selected else all_items

    # Group the day's coverage into stories so one syndicated event occupies one
    # slot carrying its pickup count, rather than crowding the rail with copies.
    # Clustered after filtering, so a story's pickup count reflects what the
    # reader is actually looking at.
    stories = cluster(items)
    # A story is an alert if *any* copy of it fired: the alert may have been
    # computed on a copy that is not the lead.
    alerts = [s for s in stories if any(m.is_alert for m in s.members)]

    # Follows the client filter, like the rest of the page: looking at one mandate
    # means looking at one mandate, including what to send them.
    drafts = _fetch_angles(session, day)
    if selected_client is not None:
        drafts = [d for d in drafts if d.client_id == selected_client]

    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "day": day,
            "day_iso": day.strftime(_DATE_FORMAT),
            # Only computed when there is nothing to show, so the ordinary path
            # does not pay for a count nobody reads.
            # Scoped to the filter the reader is looking through, and only
            # computed when there is nothing to show.
            "recent_count": (
                _recent_coverage_count(session, day, client_id=selected_client)
                if not items
                else 0
            ),
            # Whether this mandate has *any* coverage at all, ever. "Nothing today"
            # and "nothing, ever" are different problems: the first is a quiet news
            # day, the second is almost always a configuration one, and offering an
            # archive link for it sends the reader somewhere that confirms nothing.
            "client_has_archive": (
                _client_has_any_coverage(session, selected_client)
                if not items and selected_client is not None
                else False
            ),
            "selected_client_name": next(
                (m.name for m in mandates if m.id == selected_client), ""
            ),
            "recent_days": _EMPTY_STATE_LOOKBACK_DAYS,
            "items": items,
            "stories": stories,
            "alerts": alerts,
            "angles": drafts,
            "clients": mandates,
            "client_counts": counts,
            "selected_client": selected_client,
            "total_today": len(counts) and sum(counts.values()) or 0,
            "card_limit": _CLIENT_CARD_LIMIT,
            "categories": present,
            "selected_category": selected,
            "hidden_count": len(all_items) - len(items),
            "last_run": _fetch_last_run(session),
        },
    )
