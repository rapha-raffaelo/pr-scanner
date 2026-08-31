"""The Today view: today's coverage across all clients (DEC-1 option C).

Two-pane triage layout — a narrow left rail of alert cards (the must-see) beside
a full ranked feed on the right. The page defaults to the current local day with
a date picker for any prior day; a day with no coverage renders a clean empty
state rather than an error.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import angles, config, crisis, newsjack
from ...models import (
    Analysis,
    Angle,
    Article,
    Asset,
    Client,
    Crisis,
    NewsjackOpportunity,
    Run,
    TopicHit,
    visible_coverage,
)
from ...outlets import tier_for
from ...stories import cluster
from ..app import get_db, templates
from ..redirects import local_target

router = APIRouter()

# The date-picker exchange format; also how a prior day is passed as ?date=.
_DATE_FORMAT = "%Y-%m-%d"


# Above this many mandates the filter strip becomes a dropdown: a row of cards
# is faster to hit while you can still take it in at a glance, and unusable once
# it wraps to three lines.
_CLIENT_CARD_LIMIT = 10

# The fast lane's deliberate cap: at most this many open opportunities per
# mandate stand on Heute. Three is a selection a consultant weighs in ten
# seconds; ten is the noise this tool was built against. Cut by pickup count —
# the wave that travels widest is the one worth catching — and the cut is
# named on the page rather than happening silently.
_MAX_OPEN_OPPORTUNITIES = 3


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
    # Days between the draft and the viewed day. Shown, because a card that stays
    # for a week would otherwise read as this morning's work every morning.
    age_days: int
    # The brain version the draft was written under, or None for a row stored
    # before the stamp existed. Carried here and not only on the detail page:
    # this column is where the drafts are actually read, and a provenance line
    # that appears only on the page nobody opens is provenance nobody has.
    brain_version: int | None


@dataclass(frozen=True, slots=True)
class QuietClientView:
    """A mandate with no draft, and the reason there is none.

    The column has to say something on a quiet day, or an empty rail reads as a
    broken feature rather than as a market that gave nothing away. What it says has
    to be true, and the two reasons need opposite responses: a radar that ran and
    found no opening is working, a mandate with no themes is not set up.
    """

    client_id: int
    client_name: str
    #: Market items the radar surfaced in the window; the evidence that it ran.
    seen: int
    has_themes: bool
    #: What the last attempt actually concluded, recorded on the client by the
    #: sweep. More precise than the generic sentence, and the reason the page can
    #: say something true about an attempt made at 06:10.
    note: str = ""


@dataclass(frozen=True, slots=True)
class CrisisOffer:
    """The offer to declare a crisis, as the rail renders it.

    DEC-1 locked option A: this is the *whole* effect of crossing the threshold.
    Nothing about the cadence, the drafts or the notifications changes while this
    card is on screen — it is a question, and the answer is a click.
    """

    client_id: int
    client_name: str
    article_id: int
    headline: str
    #: Which of the two stored conditions produced it, so the reader can tell
    #: "the analyzer called this a crisis" from "nobody did and it is everywhere".
    trigger: str
    #: How many outlets carry the story the offer is about.
    outlets: int


@dataclass(frozen=True, slots=True)
class OpenCrisisView:
    """A declared crisis, as the rail renders it: since when, how bad, and the
    way out of it."""

    id: int
    client_id: int
    client_name: str
    level: int
    level_max: int
    declared_at: dt.datetime
    declared_by: str
    outlet_count: int
    negative_count: int
    article_count: int


@dataclass(frozen=True, slots=True)
class OpportunityView:
    """One open newsjack opportunity, as the fast lane's card renders it.

    The card is the ten-second decision (UHR-05): is this worth it, and is
    there still time. So it carries the remaining time as a number rather than
    a colour, and the standing's one sentence rather than a verdict badge.
    """

    id: int
    client_id: int
    client_name: str
    #: The origin piece: who had the story first, and where.
    headline: str
    url: str
    source: str
    published_at: dt.datetime
    #: Distinct outlets carrying the story when it was weighed.
    pickup_count: int
    #: The model's one sentence: what the mandate's standing rests on.
    reason: str
    window_ends_at: dt.datetime
    #: The remaining time, as the two numbers the card can show. ``hours_left``
    #: is whole hours; when it is zero, ``minutes_left`` carries the rest.
    hours_left: int
    minutes_left: int
    #: The occasion this opportunity was opened as, once "Text schreiben" was
    #: pressed — what makes the texts findable from the card.
    angle_id: int | None
    #: How many texts hang on that occasion.
    texts: int


@dataclass(frozen=True, slots=True)
class OpportunityCut:
    """The visible name of the cap: which mandate lost how many cards."""

    client_name: str
    hidden: int


def _remaining(window_ends_at: dt.datetime, now: dt.datetime) -> tuple[int, int]:
    """Hours and minutes until the window closes, floored, never negative.

    Floored rather than rounded because the number is a promise: a card saying
    "noch 2 Std." with 1:59 left has already broken it once.
    """
    left = max(dt.timedelta(0), window_ends_at - now)
    return int(left.total_seconds() // 3600), int((left.total_seconds() // 60) % 60)


def _opportunity_texts(
    session: Session, opportunity_ids: list[int]
) -> tuple[dict[int, int], dict[int, int]]:
    """The occasion each opportunity was opened as, and how many texts hang on
    it — ``(angle_by_opportunity, text_count_by_angle)`` in two bounded reads."""
    if not opportunity_ids:
        return {}, {}
    angle_by_opp = {
        row.newsjack_id: row.id
        for row in session.execute(
            select(Angle.id, Angle.newsjack_id).where(
                Angle.newsjack_id.in_(opportunity_ids)
            )
        ).all()
    }
    if not angle_by_opp:
        return {}, {}
    counts = dict(
        session.execute(
            select(Asset.angle_id, func.count())
            .where(Asset.angle_id.in_(list(angle_by_opp.values())))
            .group_by(Asset.angle_id)
        ).all()
    )
    return angle_by_opp, counts


def _fetch_opportunities(
    session: Session, mandates: list[Client], *, now: dt.datetime
) -> tuple[list[OpportunityView], list[OpportunityCut]]:
    """The open opportunities the fast lane puts above the day, and the cuts.

    Read per mandate off :func:`newspulse.newsjack.open_opportunities`, which
    already applies the whole gate — ``belegt`` only, not waved off, window not
    yet passed against the clock. Capped at :data:`_MAX_OPEN_OPPORTUNITIES` per
    mandate by pickup count (ties keep the sooner-to-expire card, because the
    stored order is soonest-first and the sort is stable); whatever the cap
    removed is returned as a named cut rather than vanishing.

    Across mandates the cards are ordered soonest-to-expire first — the order a
    consultant has to look at them in.
    """
    tz = _local_tz()
    views: list[OpportunityView] = []
    cuts: list[OpportunityCut] = []
    for mandate in mandates:
        rows = newsjack.open_opportunities(session, mandate, now=now)
        if not rows:
            continue
        kept = sorted(rows, key=lambda row: -row.pickup_count)
        if len(kept) > _MAX_OPEN_OPPORTUNITIES:
            cuts.append(
                OpportunityCut(
                    client_name=mandate.name,
                    hidden=len(kept) - _MAX_OPEN_OPPORTUNITIES,
                )
            )
            kept = kept[:_MAX_OPEN_OPPORTUNITIES]
        angle_by_opp, text_counts = _opportunity_texts(
            session, [row.id for row in kept]
        )
        for row in kept:
            hours, minutes = _remaining(row.window_ends_at, now)
            angle_id = angle_by_opp.get(row.id)
            views.append(
                OpportunityView(
                    id=row.id,
                    client_id=mandate.id,
                    client_name=mandate.name,
                    headline=row.article.title,
                    url=row.article.url,
                    source=row.article.source,
                    published_at=row.article.published_at.astimezone(tz),
                    pickup_count=row.pickup_count,
                    reason=row.reason,
                    window_ends_at=row.window_ends_at,
                    hours_left=hours,
                    minutes_left=minutes,
                    angle_id=angle_id,
                    texts=text_counts.get(angle_id, 0) if angle_id else 0,
                )
            )
    views.sort(key=lambda view: view.window_ends_at)
    return views, cuts


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
                Article.published_at >= _day_bounds_utc(start)[0],
                Article.published_at < _day_bounds_utc(day)[0],
                # The same relevance gate _fetch_items uses, not the is_relevant
                # column: the count has to describe the rows the archive will
                # actually show, or the hint promises coverage that isn't there.
                visible_coverage(),
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
                visible_coverage(),
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
            visible_coverage(),
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
    """The newest positioning draft per client in the week up to ``day``.

    A week rather than the viewed day: an opening does not expire at midnight. The
    market development a draft rests on is still current days later, and a column
    that empties itself every night hides work that is still usable — which is
    what it did. Each card carries its age instead, so a draft from Monday is not
    mistaken for this morning's.

    Sources are resolved here rather than in the template, so a draft citing an
    article that no longer exists renders without its citation instead of erroring.
    """
    _start_utc, end_utc = _day_bounds_utc(day)
    drafts = angles.recent(session, end_utc)
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
                age_days=max(
                    0, (day - draft.generated_at.astimezone(_local_tz()).date()).days
                ),
                brain_version=draft.brain_version,
            )
        )
    return views


def _fetch_quiet_clients(
    session: Session, day: dt.date, drafted: set[int], mandates: list[Client]
) -> list[QuietClientView]:
    """The mandates with no draft, each with the reason there is none.

    Counting what the radar *saw* is the point. "Kein Anlass" on its own is
    indistinguishable from a broken feature; "12 Marktmeldungen gesichtet, keine
    davon trug" is a report on work done.
    """
    _start_utc, end_utc = _day_bounds_utc(day)
    since = end_utc - dt.timedelta(days=angles.COLUMN_DAYS)
    seen_counts = dict(
        session.execute(
            select(TopicHit.client_id, func.count())
            .join(Article, Article.id == TopicHit.article_id)
            .where(TopicHit.found_at >= since, TopicHit.found_at < end_utc)
            .group_by(TopicHit.client_id)
        ).all()
    )
    return [
        QuietClientView(
            client_id=client.id,
            client_name=client.name,
            seen=int(seen_counts.get(client.id, 0)),
            has_themes=bool(client.keywords or client.alert_topics),
            note=client.impulse_note or "",
        )
        for client in mandates
        if client.id not in drafted
    ]


def _fetch_crises(
    session: Session, mandates: list[Client]
) -> tuple[list[OpenCrisisView], list[CrisisOffer]]:
    """What the rail says about crises: the declared ones, and the offers.

    Read on every render rather than cached, because both halves are answers
    about *now*: an offer is withdrawn the moment somebody declares, and a
    declared crisis disappears from the rail the moment somebody stands it down.

    *Offers* are read per mandate over the same roster the filter strip uses, so
    a deactivated mandate never offers anything — the same kill switch the
    cadence honours. *Declared* crises are broader on purpose: an open crisis is
    rendered even when its mandate has left the roster (deactivated mid-crisis),
    because this rail is the only close button there is, and a crisis nobody can
    see is a crisis nobody can stand down.
    """
    open_rows = {
        row.client_id: row
        for row in session.scalars(
            select(Crisis).where(Crisis.closed_at.is_(None))
        ).all()
    }
    names = dict(
        session.execute(
            select(Client.id, Client.name).where(Client.id.in_(list(open_rows)))
        ).all()
    ) if open_rows else {}

    def _view(standing: Crisis) -> OpenCrisisView:
        return OpenCrisisView(
            id=standing.id,
            client_id=standing.client_id,
            client_name=names.get(standing.client_id, "—"),
            level=standing.level,
            level_max=crisis.LEVEL_MAX,
            declared_at=standing.declared_at,
            declared_by=standing.declared_by,
            outlet_count=standing.outlet_count,
            negative_count=standing.negative_count,
            article_count=standing.article_count,
        )

    declared: list[OpenCrisisView] = []
    offers: list[CrisisOffer] = []
    for mandate in mandates:
        standing = open_rows.pop(mandate.id, None)
        if standing is not None:
            declared.append(_view(standing))
            # At most one open crisis per mandate, so there is nothing left to
            # offer this one — ``propose`` would return None anyway; this saves
            # the query.
            continue
        offer = crisis.propose(session, mandate)
        if offer is not None:
            offers.append(
                CrisisOffer(
                    client_id=mandate.id,
                    client_name=mandate.name,
                    article_id=offer.article_id,
                    headline=offer.headline,
                    trigger=offer.trigger.value,
                    outlets=offer.outlets,
                )
            )
    # Whatever is left is open on a mandate the roster no longer lists. Shown so
    # it stays closable; never offered for, and never swept (``crisis.due``
    # excludes it), so showing it is the whole remaining duty.
    declared.extend(_view(standing) for standing in open_rows.values())
    return declared, offers


def _fetch_last_run(session: Session) -> RunStatusView | None:
    """The most recent run for the header, or None if the job never ran."""
    run: Run | None = session.execute(
        select(Run).order_by(Run.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        # No sweep has run — but a mandate's onboarding backfill fetches and
        # analyses without writing a runs row, so "Noch kein Lauf" sat in the
        # header above thirty articles it had just imported. Both statements were
        # true and together they read as a broken system. Report the import
        # instead, as what it is.
        imported = session.scalar(select(func.count()).select_from(Article))
        if not imported:
            return None
        newest = session.scalar(select(func.max(Article.fetched_at)))
        return RunStatusView(
            ran_at=newest or dt.datetime.now(dt.UTC),
            is_running=False,
            status="import",
            articles_checked=imported,
            feed_errors=0,
        )
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


def _latest_covered_day(
    session: Session, *, before: dt.date, within: int = 7
) -> dt.date | None:
    """The most recent local day at or before ``before`` that has any coverage.

    Bounded to a week: past that the honest answer is the empty state and the
    setup checklist it carries, not a fortnight-old day dressed up as news.
    """
    tz = _local_tz()
    floor = dt.datetime.combine(
        before - dt.timedelta(days=within), dt.time.min, tzinfo=tz
    ).astimezone(dt.UTC)
    ceiling = dt.datetime.combine(
        before, dt.time.min, tzinfo=tz
    ).astimezone(dt.UTC)
    newest = session.scalar(
        select(func.max(Article.published_at))
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            visible_coverage(),
            Article.published_at >= floor,
            Article.published_at < ceiling,
        )
    )
    return newest.astimezone(tz).date() if newest else None


@router.get("/client/{client_id}/heute")
def client_day(client_id: int) -> RedirectResponse:
    """The day, scoped to one mandate.

    A redirect rather than a second copy of the view: the day filtered to one
    client is exactly what ``/today?client=`` already renders, and two routes
    rendering the same three columns would drift apart within a month. The tab
    strip stays visible because the day view carries it whenever a single mandate
    is selected.
    """
    return RedirectResponse(f"/today?client={client_id}", status_code=303)


@router.post("/gelegenheit/{opportunity_id}/verwerfen")
def dismiss_opportunity(
    opportunity_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Wave a fast-lane opportunity off. No reason asked, none stored.

    Deliberately cheaper than every other stand-down in the tool: a dismissed
    opportunity costs nothing and proves nothing, so demanding a justification
    would only teach people not to press the button. The stamp is the whole
    act — ``open_opportunities`` stops returning the row, and the stored
    verdict (which the stamp never touches) is what keeps the same story from
    being weighed again for this mandate.

    A stale or double click is a no-op rather than an error: the first stamp
    stands, and an unknown id redirects back the same way — the card the click
    aimed at is gone either way, and there is nothing a 404 page would let the
    reader do about it.
    """
    row = session.get(NewsjackOpportunity, opportunity_id)
    if row is not None and row.dismissed_at is None:
        row.dismissed_at = dt.datetime.now(dt.UTC)
        session.commit()
    return RedirectResponse(local_target(redirect_to), status_code=303)


@router.get("/today", response_class=HTMLResponse)
def today_view(
    request: Request,
    date: str | None = None,
    category: str | None = None,
    client: int | None = None,
    show_muted: bool = False,
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

    # A morning sweep runs at 06:10 and brings in what was published overnight —
    # which is yesterday's date. This page filters by publication day, so the page
    # called "Heute" is empty at exactly the hour it is opened, every day, while
    # the header truthfully reports thirty-five new articles. Measured on the live
    # instance at 08:30 on a Monday: today 0 items, yesterday 21 with 14 alerts,
    # none of them ever seen.
    #
    # So an empty *today* falls back to the newest day that has something, and
    # says so. Only when no date was asked for: an explicit ?date= is a question
    # about that day and deserves its real answer, empty or not.
    shown_instead_of: dt.date | None = None
    if not all_items and date is None:
        recent = _latest_covered_day(session, before=day)
        if recent is not None:
            shown_instead_of, day = day, recent
            all_items = _fetch_items(session, day)

    # Each mandate's own muted categories, applied before anything else is
    # counted. A listed retailer's ticker produces three near-identical items a
    # day, each scored 4-5 out of 10 and therefore filed beside a real event; the
    # category filter could hide them but forgot the choice on every page load, so
    # the same decision had to be made every morning. That is the point at which a
    # sixty-second triage stops being sixty seconds.
    #
    # Hidden, never discarded: the count below says how many and offers them back
    # in one click, and the archive, the exports and every number keep all of it.
    muted = {
        client_id: set(cats)
        for client_id, cats in session.execute(
            select(Client.id, Client.muted_categories)
        ).all()
        if cats
    }
    muted_count = 0
    if muted and not show_muted:
        kept = [
            item
            for item in all_items
            if item.category not in muted.get(item.client_id, ())
        ]
        muted_count = len(all_items) - len(kept)
        all_items = kept

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

    # The offer DEC-1 locked, and the crises somebody already declared. Read off
    # the same mandate roster the filter strip uses, and filtered with it below.
    open_crises, crisis_offers = _fetch_crises(session, mandates)

    # The fast lane (UHR-05): open opportunities above the day's coverage,
    # because an opening is worthless the moment it has to be searched for.
    # Read against the clock on every render — expiry is a comparison, not a
    # job, so a card disappears on time with no run having happened.
    opportunities, opportunity_cuts = _fetch_opportunities(
        session, mandates, now=dt.datetime.now(dt.UTC)
    )

    # Follows the client filter, like the rest of the page: looking at one mandate
    # means looking at one mandate, including what to send them.
    drafts = _fetch_angles(session, day)
    quiet = _fetch_quiet_clients(session, day, {d.client_id for d in drafts}, mandates)
    if selected_client is not None:
        drafts = [d for d in drafts if d.client_id == selected_client]
        quiet = [q for q in quiet if q.client_id == selected_client]
        open_crises = [c for c in open_crises if c.client_id == selected_client]
        crisis_offers = [o for o in crisis_offers if o.client_id == selected_client]
        opportunities = [
            o for o in opportunities if o.client_id == selected_client
        ]
        selected_name = next(
            (m.name for m in mandates if m.id == selected_client), ""
        )
        opportunity_cuts = [
            c for c in opportunity_cuts if c.client_name == selected_name
        ]

    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "day": day,
            "day_iso": day.strftime(_DATE_FORMAT),
            # Present only when the day is filtered to a single mandate, which is
            # what makes this page that mandate's workspace tab rather than the
            # portfolio-wide view.
            "workspace_client": (
                session.get(Client, selected_client) if selected_client else None
            ),
            # Set when the reader asked for no particular day, today held nothing
            # and this is the newest day that did. The banner has to say it: a page
            # headed "Heute" quietly showing yesterday is worse than an empty one.
            "shown_instead_of": shown_instead_of,
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
            # Above the alert rail, because a crisis is the one thing on this
            # page that outranks a day's worth of alerts — and because DEC-1's
            # offer has to be where the alerting already is.
            "open_crises": open_crises,
            "crisis_offers": crisis_offers,
            # The fast lane's cards, above the day (UHR-05). Empty means the
            # section is simply not rendered — no placeholder, per acceptance:
            # without an open opportunity, Heute looks exactly as it does today.
            "opportunities": opportunities,
            "opportunity_cuts": opportunity_cuts,
            "angles": drafts,
            "quiet_clients": quiet,
            "angle_days": angles.COLUMN_DAYS,
            "clients": mandates,
            "client_counts": counts,
            "selected_client": selected_client,
            "total_today": len(counts) and sum(counts.values()) or 0,
            "card_limit": _CLIENT_CARD_LIMIT,
            "categories": present,
            "selected_category": selected,
            "hidden_count": len(all_items) - len(items),
            # Separate from hidden_count: that one is the category dropdown the
            # reader just chose, this one is a standing preference they may have
            # forgotten setting. Conflating them would make a remembered decision
            # look like the one they made a second ago.
            "muted_count": muted_count,
            "show_muted": show_muted,
            "last_run": _fetch_last_run(session),
        },
    )
