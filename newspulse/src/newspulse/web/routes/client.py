"""Client detail and full historical archive (NP-08).

``/client/{id}`` renders the client profile plus the complete archive of coverage
about that client, newest first, back to the tool's first run. Date-range,
source, category, and free-text (headline + summary) filters run server-side
against the full archive and compose; the filtered, paginated list is HTMX-
swapped in place so the view scales as history grows to thousands of rows (DEC-3,
FastAPI server-rendered — filtering/search/pagination against the full SQLite
archive rather than loading it all into the page).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlencode

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from ... import config, gnews, industry
from ... import angles
from ... import onboarding, profile as profiles
from ...models import (
    Analysis,
    Angle,
    Article,
    Category,
    Client,
    MarketSignal,
    SignalKind,
    SignalOrigin,
    TopicHit,
    visible_coverage,
)
from ... import coverage_map
from ...reporting import client_workbook, share_of_voice
from ..app import get_db, templates

# Reuse the Today route's shared, tested chrome/zone helpers rather than
# duplicating the DST-aware local-day math and the last-run header query: the
# local zone and the "last run" banner are shared page chrome, single-sourced in
# one place so both views agree on day boundaries and header content.
from .today import _day_bounds_utc, _fetch_last_run, _local_tz

router = APIRouter()

_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# Archive rows per page. History can grow to thousands of rows across years, so
# the list is paginated rather than rendered all at once; 50 fills a screen with
# headroom while keeping each page query cheap.
_PAGE_SIZE = 50

# Exchange format for the date-range inputs and the ?date_from=/?date_to= params.
_DATE_FORMAT = "%Y-%m-%d"

# POST-then-redirect, so a mute survives a refresh instead of being re-posted.
_SEE_OTHER = 303



@dataclass(frozen=True, slots=True)
class ArchiveRow:
    """One rendered archive row — the same field set as a Today coverage row."""

    # Carried so a row can be dismissed from here: the client's own archive is
    # where a wrong match is most obvious, since every row claims to be about them.
    analysis_id: int
    headline: str
    url: str
    source: str
    published_at: dt.datetime
    summary: str | None
    category: str
    importance: int
    is_alert: bool


@dataclass(frozen=True, slots=True)
class ClientProfile:
    """The client header shown above the archive."""

    id: int
    name: str
    industry: str | None
    country: str
    aliases: list[str]
    keywords: list[str]
    alert_topics: list[str]
    active: bool
    logo_url: str | None


@dataclass(frozen=True, slots=True)
class ArchiveFilters:
    """The applied filter values, normalized and echoed back into the form so it
    stays populated across HTMX swaps and the pagination links carry them."""

    date_from: str
    date_to: str
    source: str
    category: str
    search: str


@dataclass(frozen=True, slots=True)
class Pagination:
    """Page state plus prebuilt prev/next URLs (None when at an edge)."""

    page: int
    total_pages: int
    total_count: int
    prev_url: str | None
    next_url: str | None


def _parse_date(raw: str | None) -> dt.date | None:
    """A valid ``YYYY-MM-DD`` or ``None`` — absent/blank/malformed applies no
    bound rather than erroring, so a hand-typed or stale URL never 500s."""
    if not raw:
        return None
    try:
        return dt.datetime.strptime(raw, _DATE_FORMAT).date()
    except ValueError:
        return None


def _parse_category(raw: str | None) -> Category | None:
    """A valid ``Category`` or ``None`` (unknown/blank applies no filter)."""
    if not raw:
        return None
    try:
        return Category(raw)
    except ValueError:
        return None


def _profile(client: Client) -> ClientProfile:
    """Snapshot the client's mutable ORM fields into an immutable view object."""
    return ClientProfile(
        id=client.id,
        name=client.name,
        industry=client.industry,
        country=client.country,
        aliases=list(client.aliases),
        keywords=list(client.keywords),
        alert_topics=list(client.alert_topics),
        active=client.active,
        logo_url=client.logo_url,
    )


def _archive_conditions(
    client_id: int | None,
    date_from: dt.date | None,
    date_to: dt.date | None,
    source: str,
    category: Category | None,
    search: str,
) -> list[ColumnElement[bool]]:
    """WHERE clauses for an archive query with the active filters applied.

    Every filter is an independent AND term, so any combination composes; each is
    added only when its value is present, and the date bounds reuse the Today
    route's DST-aware local-day math (from-day start .. to-day end, exclusive).

    ``client_id`` of ``None`` spans the whole portfolio — what the cross-client
    Archiv view needs — instead of scoping to one client's detail page.
    """
    conditions: list[ColumnElement[bool]] = [
        visible_coverage(),
    ]
    if client_id is not None:
        conditions.append(Analysis.client_id == client_id)
    if date_from is not None:
        conditions.append(Article.published_at >= _day_bounds_utc(date_from)[0])
    if date_to is not None:
        conditions.append(Article.published_at < _day_bounds_utc(date_to)[1])
    if source:
        conditions.append(func.lower(Article.source) == source.casefold())
    if category is not None:
        conditions.append(Analysis.category == category)
    if search:
        # ILIKE contains-match over the headline and the analyzer summary (the
        # displayed summary field — not the feed snippet). Parameterized (never
        # interpolated) so a term can't inject SQL; LIKE wildcards in the term are
        # escaped so a literal "%"/"_" matches itself instead of acting as a
        # wildcard. SQLite LIKE is ASCII case-insensitive, enough for the POC.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conditions.append(
            or_(
                Article.title.ilike(like, escape="\\"),
                Analysis.summary.ilike(like, escape="\\"),
            )
        )
    return conditions


def _count(session: Session, conditions: list[ColumnElement[bool]]) -> int:
    """Total matching rows across the whole archive (drives pagination)."""
    stmt = (
        select(func.count())
        .select_from(Analysis)
        .join(Article, Analysis.article_id == Article.id)
        .where(*conditions)
    )
    return session.execute(stmt).scalar_one()


def _fetch_page(
    session: Session, conditions: list[ColumnElement[bool]], page: int
) -> list[ArchiveRow]:
    """One page of archive rows, newest first (published_at desc, id desc tie)."""
    tz = _local_tz()
    stmt = (
        select(Analysis, Article)
        .join(Article, Analysis.article_id == Article.id)
        .where(*conditions)
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(_PAGE_SIZE)
        .offset((page - 1) * _PAGE_SIZE)
    )
    return [
        ArchiveRow(
            analysis_id=analysis.id,
            headline=article.title,
            url=article.url,
            source=article.source,
            # Stored UTC, shown in local time so dates line up with the day filter.
            published_at=article.published_at.astimezone(tz),
            summary=analysis.summary,
            category=analysis.category.value,
            importance=analysis.importance_score,
            is_alert=analysis.is_alert,
        )
        for analysis, article in session.execute(stmt).all()
    ]


def _archive_sources(session: Session, client_id: int) -> list[str]:
    """Distinct sources across this client's whole archive for the filter dropdown.

    Computed over the full archive (not the current filter) so the dropdown stays
    stable regardless of which filters are active.
    """
    stmt = (
        select(Article.source)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
        )
        .distinct()
        .order_by(Article.source)
    )
    return list(session.execute(stmt).scalars().all())


def _page_url(client_id: int, filters: ArchiveFilters, page: int) -> str:
    """Build a filter-preserving URL for a given page, used by prev/next links."""
    params: list[tuple[str, str]] = []
    if filters.date_from:
        params.append(("date_from", filters.date_from))
    if filters.date_to:
        params.append(("date_to", filters.date_to))
    if filters.source:
        params.append(("source", filters.source))
    if filters.category:
        params.append(("category", filters.category))
    if filters.search:
        params.append(("q", filters.search))
    params.append(("page", str(page)))
    return f"/client/{client_id}?{urlencode(params)}"


def _paginate(
    client_id: int,
    filters: ArchiveFilters,
    page: int,
    total_pages: int,
    total: int,
) -> Pagination:
    """Assemble page state with prev/next URLs (page 1 is newest)."""
    return Pagination(
        page=page,
        total_pages=total_pages,
        total_count=total,
        prev_url=_page_url(client_id, filters, page - 1) if page > 1 else None,
        next_url=_page_url(client_id, filters, page + 1) if page < total_pages else None,
    )


@router.get("/client/{client_id}/map", response_class=HTMLResponse)
def coverage_map_view(
    request: Request, client_id: int, days: int = 90, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Which outlet writes about whom — and which ones never write about us."""
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return templates.TemplateResponse(
        request,
        "coverage_map.html",
        {
            "map": coverage_map.build(session, client, days=days),
            "client": client,
            "days": days,
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.get("/client/{client_id}/export.xlsx")
def client_export(
    client_id: int, days: int = 30, session: Session = Depends(get_db)
) -> Response:
    """Download this client's coverage as an .xlsx — the agency's deliverable.

    Streamed from memory rather than a temp file: a month of coverage is small,
    and nothing about the report needs to survive the request.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    payload = client_workbook(session, client, days=days)
    stamp = dt.datetime.now(_local_tz()).strftime("%Y-%m-%d")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", client.name).strip("_") or "mandant"
    return Response(
        content=payload,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="newspulse_{safe_name}_{stamp}.xlsx"'
            )
        },
    )


#: Tags shown per group on a portfolio card. A card is a glance, not a
#: configuration screen; the rest is one click away under Bearbeiten.
_CARD_TAGS = 6


@dataclass(frozen=True, slots=True)
class PortfolioRow:
    """One client as the Mandanten overview lists it."""

    id: int
    name: str
    industry: str | None
    # Both lists, because they do different work and a card that shows only one
    # says nothing about the other: keywords drive the matcher and the topic
    # radar, alert topics decide what gets escalated. Trimmed to the first few —
    # one mandate with twenty-five topics made its card five times the height of
    # its neighbours and still explained nothing.
    keywords: list[str]
    alert_topics: list[str]
    extra_keywords: int
    extra_topics: int
    active: bool
    is_competitor: bool
    logo_url: str | None
    today_count: int
    total_count: int
    # The two numbers that decide whether this mandate needs the consultant this
    # morning. The archive total does not: it is a fact about the past, and it was
    # the larger of the two figures on every card, which is the wrong emphasis for
    # a front door.
    alerts_today: int
    open_impulses: int
    # How much of its own foundation this mandate has. On the front door because a
    # thinly set-up client is otherwise merely quiet: no alerts and no impulses
    # look exactly like a calm week until you notice nobody ever asked it the
    # twenty questions.
    kickoff: onboarding.Completeness
    # When the mandate profile was last re-read. On the roster because a profile
    # decays quietly: nothing about a card says its facts are two years old, and
    # the consultant picks the mandate to work on from exactly this screen.
    profile_checked: profiles.Checked


@dataclass(frozen=True, slots=True)
class _PortfolioCounts:
    """The four per-client numbers a portfolio card carries, each keyed by id.

    One object rather than four loose dicts so the aggregation can leave the view
    entirely: what the page does with them is assemble rows, and the SQL is a
    separate job from the assembly.
    """

    totals: dict[int, int]
    today: dict[int, int]
    alerts_today: dict[int, int]
    open_impulses: dict[int, int]


def _portfolio_counts(
    session: Session, *, start: dt.datetime, end: dt.datetime
) -> _PortfolioCounts:
    """Everything the roster counts, in four grouped queries.

    Grouped rather than per client: the page renders every mandate, and sixty
    mandates asking the same question sixty times is sixty round trips for an
    answer a GROUP BY gives once.
    """
    relevant = visible_coverage()
    today_window = (
        relevant,
        Article.published_at >= start,
        Article.published_at < end,
    )
    # A draft stands for a week (angles.COLUMN_DAYS); past that it is history, not
    # something waiting to be sent.
    impulse_since = dt.datetime.now(dt.UTC) - dt.timedelta(days=angles.COLUMN_DAYS)
    return _PortfolioCounts(
        totals=dict(
            session.execute(
                select(Analysis.client_id, func.count())
                .where(relevant)
                .group_by(Analysis.client_id)
            ).all()
        ),
        today=dict(
            session.execute(
                select(Analysis.client_id, func.count())
                .join(Article, Article.id == Analysis.article_id)
                .where(*today_window)
                .group_by(Analysis.client_id)
            ).all()
        ),
        alerts_today=dict(
            session.execute(
                select(Analysis.client_id, func.count())
                .join(Article, Article.id == Analysis.article_id)
                .where(*today_window, Analysis.is_alert.is_(True))
                .group_by(Analysis.client_id)
            ).all()
        ),
        open_impulses=dict(
            session.execute(
                select(Angle.client_id, func.count())
                .where(Angle.generated_at >= impulse_since)
                .group_by(Angle.client_id)
            ).all()
        ),
    )


# The portfolio is the front door now. A consultant does not open this tool to
# read "the news" — he opens it to work on a mandate, and the first question is
# always which one. The day view keeps its own address at /today for the mornings
# when the question really is "what happened anywhere".
@router.get("/", response_class=HTMLResponse)
@router.get("/clients", response_class=HTMLResponse)
def clients_index(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Mandanten: the whole portfolio at a glance, each row a way into its archive.

    Fetch, assemble, render. The counting is :func:`_portfolio_counts`, which
    keeps this to the shape of the page rather than to the shape of the SQL.
    """
    day = dt.datetime.now(_local_tz()).date()
    start, end = _day_bounds_utc(day)
    counts = _portfolio_counts(session, start=start, end=end)

    clients = session.scalars(select(Client).order_by(Client.name)).all()
    # One clock for the whole list, so two cards checked the same morning cannot
    # be rendered a day apart by a render that straddles midnight.
    now = dt.datetime.now(dt.UTC)
    # One query for the whole portfolio rather than one per card.
    kickoffs = onboarding.completeness_by_client(session, [c.id for c in clients])
    rows = [
        PortfolioRow(
            id=c.id,
            name=c.name,
            industry=c.industry,
            keywords=list(c.keywords or [])[:_CARD_TAGS],
            alert_topics=list(c.alert_topics or [])[:_CARD_TAGS],
            extra_keywords=max(0, len(c.keywords or []) - _CARD_TAGS),
            extra_topics=max(0, len(c.alert_topics or []) - _CARD_TAGS),
            active=c.active,
            is_competitor=c.is_competitor,
            logo_url=c.logo_url,
            today_count=counts.today.get(c.id, 0),
            total_count=counts.totals.get(c.id, 0),
            alerts_today=counts.alerts_today.get(c.id, 0),
            open_impulses=counts.open_impulses.get(c.id, 0),
            kickoff=kickoffs[c.id],
            profile_checked=profiles.checked(c.profile_checked_at, now=now),
        )
        for c in clients
    ]
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            # Split rather than flagged: a benchmark is not a mandate, and a
            # single list invites reading a competitor's coverage as work.
            # Mandates only. A benchmark belongs to the client it is measured
            # against, not to a list of its own — with several mandates a flat
            # list mixes unrelated markets into one meaningless roster.
            "rows": [r for r in rows if not r.is_competitor],
            "last_run": _fetch_last_run(session),
            "header_date": day,
        },
    )


def _render_detail(
    request: Request,
    session: Session,
    client: Client,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    source: str | None = None,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    """Render the client page. Shared, so a POST that has something to show can
    render the same page rather than redirecting and losing it."""
    client_id = client.id
    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    parsed_category = _parse_category(category)
    source_value = (source or "").strip()
    search_value = (q or "").strip()

    conditions = _archive_conditions(
        client_id, parsed_from, parsed_to, source_value, parsed_category, search_value
    )
    total = _count(session, conditions)
    # Ceil-divide without floats; at least one page so an empty archive still
    # renders "Seite 1/1". Clamp the requested page into range so ?page=0, a
    # negative, or a past-the-end value never offsets into nothing.
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    current_page = min(max(page, 1), total_pages)
    rows = _fetch_page(session, conditions, current_page)

    # Echo normalized values (parsed dates reformatted, only a valid category)
    # so a malformed query param never round-trips back into the form or links.
    filters = ArchiveFilters(
        date_from=parsed_from.strftime(_DATE_FORMAT) if parsed_from else "",
        date_to=parsed_to.strftime(_DATE_FORMAT) if parsed_to else "",
        source=source_value,
        category=parsed_category.value if parsed_category else "",
        search=search_value,
    )
    return templates.TemplateResponse(
        request,
        "client_detail.html",
        {
            "client": _profile(client),
            "rows": rows,
            "filters": filters,
            "sources": _archive_sources(session, client_id),
            "categories": [category.value for category in Category],
            "pagination": _paginate(
                client_id, filters, current_page, total_pages, total
            ),
            "voice": share_of_voice(session, client, days=30),
            # Companies marked as competitors, and only those. Offering the other
            # mandates here invited exactly the nonsense it produced: a beauty-tech
            # startup proposed as Zalando's benchmark. A mandate is work to be done;
            # a competitor is a yardstick, and the two are not interchangeable just
            # because both happen to be monitored.
            "candidates": _competitor_candidates(session, client),
            "competitors": list(client.competitors),
            "last_run": _fetch_last_run(session),
            # The shared header dates every page; the archive view spans many
            # days, so it shows today.
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.get("/client/{client_id}", response_class=HTMLResponse)
def client_detail(
    request: Request,
    client_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    source: str | None = None,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render a client's profile and their filtered, paginated archive page."""
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return _render_detail(
        request,
        session,
        client,
        date_from=date_from,
        date_to=date_to,
        source=source,
        category=category,
        q=q,
        page=page,
    )


# --- Marktumfeld: what the topic radar saw, and who reported it ----------------

#: How far back the market view and the outlet ranking look. A quarter, because
#: the question they answer ("who covers our subject?") is about a beat, not about
#: this week — a single month of a quiet field would rank on two articles.
_MARKET_DAYS = 90

#: Outlets and journalists shown. A ranking is a shortlist; past this it stops
#: being a recommendation and becomes a directory.
_MARKET_TOP = 12

#: How close a deadline has to be before the page marks it. Two weeks, because
#: that is the shortest lead time in which a consultant can still get a client to
#: agree a statement, draft it and send it — inside it the row is not a date any
#: more, it is an instruction.
_DEADLINE_SOON = dt.timedelta(days=14)

#: Below a week, remaining time is stated in days. "In 0 Wochen" is not German,
#: and rounding three days up to a week would overstate the time a reader has.
_DAYS_PER_WEEK = 7


@dataclass(frozen=True, slots=True)
class MarketItem:
    """One market article the radar surfaced for this client."""

    headline: str
    url: str
    source: str
    author: str | None
    published_at: dt.datetime
    summary: str | None


@dataclass(frozen=True, slots=True)
class Remaining:
    """How long is left until a date, in the units a consultant thinks in.

    Counted in whole local days rather than as a ``timedelta``, so "in 5 Wochen"
    does not become "in 4 Wochen" because the page was opened in the evening.
    """

    days: int

    @property
    def weeks(self) -> int:
        return self.days // _DAYS_PER_WEEK

    @property
    def in_weeks(self) -> bool:
        """Whether to state this in weeks at all — see :data:`_DAYS_PER_WEEK`."""
        return self.days >= _DAYS_PER_WEEK


@dataclass(frozen=True, slots=True)
class SignalRow:
    """One study, regulatory date or event, as the market page renders it."""

    kind: SignalKind
    title: str
    url: str
    publisher: str
    summary: str
    origin: SignalOrigin
    published_at: dt.datetime | None
    effective_at: dt.datetime | None
    deadline_at: dt.datetime | None
    #: Time left until the date that makes this row actionable, ``None`` when
    #: that date has passed or the source never stated one.
    until: Remaining | None
    #: Time left until the door closes, on the classes that have a door.
    until_deadline: Remaining | None

    @property
    def from_search(self) -> bool:
        """The half of DEC-1 B a reader has to judge for himself."""
        return self.origin is SignalOrigin.SUCHE

    @property
    def deadline_soon(self) -> bool:
        """Whether the deadline is inside :data:`_DEADLINE_SOON`."""
        return (
            self.until_deadline is not None
            and self.until_deadline.days <= _DEADLINE_SOON.days
        )

    @property
    def next_in(self) -> Remaining | None:
        """Time left until whichever of this row's dates comes first.

        The one number the calendar is ordered and read by. A row usually carries
        one date, but regulation routinely carries two that mean opposite things
        — "you may still speak" and "it now applies to you" — and the nearer of
        them is the one that decides what a consultant does this week.
        """
        ahead = [r for r in (self.until, self.until_deadline) if r is not None]
        return min(ahead, key=lambda r: r.days) if ahead else None

    @property
    def next_is_deadline(self) -> bool:
        """Whether :attr:`next_in` counts down to the door rather than the event.

        The template says which, because "in 5 Wochen" beside a conference means
        two entirely different things depending on the answer: five weeks to
        submit a talk, or five weeks until the doors open.
        """
        if self.until_deadline is None:
            return False
        return self.until is None or self.until_deadline.days < self.until.days

    @property
    def next_at(self) -> dt.datetime | None:
        """The date :attr:`next_in` counts down to."""
        return self.deadline_at if self.next_is_deadline else self.effective_at


@dataclass(frozen=True, slots=True)
class Nomination:
    """One outlet or journalist, ranked by how much of this subject they cover."""

    name: str
    articles: int
    last_seen: dt.datetime
    # True when the count comes from coverage of the client itself rather than
    # from its market: the two mean different things to a PR consultant and must
    # not be added together into one meaningless number.
    writes_about_client: bool


def _market_items(session: Session, client_id: int, *, days: int) -> list[MarketItem]:
    """The radar's articles for this client, newest first.

    Deliberately unfiltered, unlike the pitch list built from the same rows. This
    page is material to read and judge; a story whose headline does not repeat the
    theme can still be the one worth positioning on — "BitMEX stellt den Betrieb
    ein" never says "Onchain-Liquidität". An address list cannot afford that
    latitude and does not get it (:func:`newspulse.pitch.targets_for`), because
    the cost of a wrong row there is an email to the wrong journalist.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    tz = _local_tz()
    rows = session.execute(
        select(Article)
        .join(TopicHit, TopicHit.article_id == Article.id)
        .where(TopicHit.client_id == client_id, Article.published_at >= since)
        .order_by(Article.published_at.desc())
    ).scalars().all()
    return [
        MarketItem(
            headline=article.title,
            url=article.url,
            source=article.source,
            author=(article.author or "").strip() or None,
            published_at=article.published_at.astimezone(tz),
            summary=article.summary_text,
        )
        for article in rows
    ]


def _rank(rows, *, about_client: bool) -> list[Nomination]:
    """Turn (name, count, last) tuples into nominations, blanks dropped."""
    out: list[Nomination] = []
    for name, count, last in rows:
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        out.append(
            Nomination(
                name=cleaned,
                articles=int(count),
                last_seen=last,
                writes_about_client=about_client,
            )
        )
    return out


def _competitor_candidates(session: Session, client: Client) -> list[Client]:
    """Companies that could be this client's benchmark, its own field first.

    Every monitored competitor used to be offered to every mandate, so a finance
    platform was invited to benchmark itself against ASOS and H&M — fashion
    brands that exist in the portfolio only because a fashion mandate needed
    them. Share of voice is a statement about *a market*; a number computed
    across two of them is not a fact about anything.

    Sorted rather than filtered: the operator may know a cross-industry rival the
    labels cannot see, and hiding a real option to prevent a silly one trades a
    small embarrassment for a wrong answer. The template groups the two.
    """
    field = {t.casefold() for t in gnews.context_terms(client)}
    others = [
        c
        for c in session.scalars(
            select(Client).where(Client.is_competitor.is_(True)).order_by(Client.name)
        ).all()
        if c.id != client.id and c not in client.competitors
    ]
    if not field:
        return others
    return sorted(
        others,
        key=lambda c: (
            not (field & {t.casefold() for t in gnews.context_terms(c)}),
            c.name,
        ),
    )


def _nominations(session: Session, client_id: int, *, days: int) -> dict[str, list[Nomination]]:
    """Who to talk to, ranked from what the archive already knows.

    Two lists, kept apart on purpose. Outlets that write *about the client* are
    the existing relationships; outlets that write *about its subject* are the
    ones worth pitching — and for a young company the second list is the only one
    with anything in it.

    Journalists come from the feeds' author field, which most feeds simply do not
    set (Google News never does), so that list is thin by nature. It is shown for
    what it is rather than padded: naming a journalist who does not cover the beat
    would cost the consultant a relationship, and no ranking is worth that.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)

    def _by(column, join, extra):
        return session.execute(
            select(column, func.count(), func.max(Article.published_at))
            .select_from(Article)
            .join(join[0], join[1])
            .where(extra, Article.published_at >= since)
            .group_by(column)
            .order_by(func.count().desc(), func.max(Article.published_at).desc())
            .limit(_MARKET_TOP)
        ).all()

    topic_join = (TopicHit, TopicHit.article_id == Article.id)
    own_join = (Analysis, Analysis.article_id == Article.id)
    # Coverage *of* the client has to pass the same gate as everywhere else.
    # Without it an outlet kept credit for stories a human had already marked
    # "nicht relevant", and for matches the analyzer scored as not about this
    # client at all — so the ranking that decides who to pitch was built partly
    # from coverage the rest of the app had agreed to forget.
    own_is_coverage = and_(Analysis.client_id == client_id, visible_coverage())
    return {
        "market_outlets": _rank(
            _by(Article.source, topic_join, TopicHit.client_id == client_id),
            about_client=False,
        ),
        "own_outlets": _rank(_by(Article.source, own_join, own_is_coverage), about_client=True),
        "market_authors": _rank(
            _by(Article.author, topic_join, TopicHit.client_id == client_id),
            about_client=False,
        ),
        "own_authors": _rank(_by(Article.author, own_join, own_is_coverage), about_client=True),
    }


# --- The three market classes: a calendar, not three more lists -----------------


def _remaining(when: dt.datetime | None, *, today: dt.date, tz) -> Remaining | None:
    """Whole days from today until ``when``, or ``None`` if that is behind us.

    ``None`` for a date in the past is what makes the page a calendar rather than
    an archive: under DEC-2 C an item leaves because nothing is coming, and this
    is the function that answers whether anything is.
    """
    if when is None:
        return None
    days = (when.astimezone(tz).date() - today).days
    return Remaining(days=days) if days >= 0 else None


def _signal_row(signal: MarketSignal, *, today: dt.date, tz) -> SignalRow:
    return SignalRow(
        kind=signal.kind,
        title=signal.title,
        url=signal.url,
        publisher=signal.publisher,
        summary=signal.summary,
        origin=signal.origin,
        published_at=signal.published_at.astimezone(tz) if signal.published_at else None,
        effective_at=signal.effective_at.astimezone(tz) if signal.effective_at else None,
        deadline_at=signal.deadline_at.astimezone(tz) if signal.deadline_at else None,
        until=_remaining(signal.effective_at, today=today, tz=tz),
        until_deadline=_remaining(signal.deadline_at, today=today, tz=tz),
    )


def _by_publication(rows: Sequence[SignalRow]) -> list[SignalRow]:
    """Studies: no date lands in the future, so newest published first (DEC-2 C).

    Nothing ages out. A study's whole value to a consultant is that it stays
    citable for months, and a timer would throw it away at roughly the point he
    would want it.
    """
    # An aware sentinel, so the key is totally ordered even between two undated
    # rows — a naive one would raise the moment the tuple's first element tied.
    undated_first = dt.datetime.min.replace(tzinfo=dt.UTC)
    return sorted(
        rows,
        key=lambda r: (r.published_at is not None, r.published_at or undated_first),
        reverse=True,
    )


def _forward(rows: Sequence[SignalRow]) -> list[SignalRow]:
    """A calendar of the dated classes: soonest first, what has passed gone.

    DEC-2 C in one function. The ranking key is the nearest date the row still
    carries — its own or its deadline, whichever comes first — rather than when
    the sweep found it, so a consultation opening in five weeks sits above a law
    landing next year no matter which arrived this morning.

    A row every date of which is behind us is dropped: nothing is coming, which
    is the only reason an item leaves the page under the locked rule. A row the
    parser could read *no* date out of has not passed and is not dropped either;
    it sorts to the end, where the template says the date could not be read
    rather than pretending the item does not exist. Dropping it would be the one
    failure a forward calendar must never have — a silent one.
    """
    dated = [r for r in rows if r.next_in is not None]
    undated = [r for r in rows if r.effective_at is None and r.deadline_at is None]
    dated.sort(key=lambda r: r.next_in.days)
    return [*dated, *_by_publication(undated)]


def _signals_by_kind(
    session: Session, client: Client, *, now: dt.datetime, tz
) -> dict[str, list[SignalRow]]:
    """This mandate's market signals, one ordered list per class it still wants.

    Muted classes are absent from the mapping entirely rather than present and
    empty, so the template renders no section at all for them — a class a reader
    switched off must not come back as an empty box explaining itself.
    """
    wanted = [kind for kind in SignalKind if not client.mutes_signal(kind)]
    if not wanted:
        return {}
    today = now.astimezone(tz).date()
    rows = [
        _signal_row(signal, today=today, tz=tz)
        for signal in session.scalars(
            select(MarketSignal).where(
                MarketSignal.client_id == client.id,
                MarketSignal.kind.in_(wanted),
            )
        ).all()
    ]
    order = {
        SignalKind.STUDIE: _by_publication,
        SignalKind.REGULIERUNG: _forward,
        SignalKind.VERANSTALTUNG: _forward,
    }
    return {
        kind.value: order[kind]([r for r in rows if r.kind is kind]) for kind in wanted
    }


class FieldGap(StrEnum):
    """Why the searched half of the market radar (DEC-1 B) is missing.

    Two reasons, kept apart for the reason the theme radar's two empty states are
    kept apart: an unconfigured mandate and a term the press does not write are
    different facts, they send the reader to different places, and one message
    covering both would be wrong about whichever case it was not written for.
    """

    #: No industry term at all — nothing to search the field with.
    UNSET = "unset"
    #: A term the German press does not write often enough to filter on.
    UNUSABLE = "unusable"


def _field_gap(
    client: Client,
    rows: Sequence[SignalRow],
    *,
    probe: Callable[[Client], bool] | None = None,
) -> FieldGap | None:
    """Why the searched half of DEC-1 B is missing, or ``None`` if it is not.

    The curated list applies to every mandate; the search on top of it only works
    if the industry term is one the German press actually writes. A mandate whose
    term is accurate and absent from print — "Beauty Tech" is the measured
    example — silently gets half the market radar, and a page that shows the
    curated half without a word looks like a quiet market rather than a broken
    filter. So the page says which half is missing and why.

    Deliberately narrow, because :func:`newspulse.industry.field_is_usable`
    issues a live search and this runs inside a page render. It is only asked
    when there is a gap to explain at all: a single search-found row anywhere on
    the page proves the field works, and no probe is issued. A mandate with no
    industry term at all is answered without one too — there is nothing to
    measure, and the answer is the other reason anyway. And with the search
    switched off installation-wide the missing half is not the field's fault, so
    nothing is claimed.
    """
    if not config.GOOGLE_NEWS_ENABLED:
        return None
    if any(row.from_search for row in rows):
        return None
    if not gnews.context_terms(client):
        return FieldGap.UNSET
    if (probe or industry.field_is_usable)(client):
        return None
    return FieldGap.UNUSABLE


def _set_mute(session: Session, client_id: int, kind: str, *, muted: bool) -> None:
    """Switch one market class off or back on for one mandate.

    A kind the enum does not know is a 404 rather than a stored string: the list
    is read back by the sweep to decide what not to fetch, and a typo in it would
    be a class that quietly keeps arriving with no way to see why.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if kind not in {k.value for k in SignalKind}:
        raise HTTPException(status_code=404, detail="Unknown signal kind")
    current = [k for k in (client.muted_signal_kinds or []) if k != kind]
    client.muted_signal_kinds = [*current, kind] if muted else current
    session.commit()


@router.post("/client/{client_id}/market/{kind}/mute")
def mute_signal_kind(
    client_id: int, kind: str, session: Session = Depends(get_db)
) -> Response:
    """Switch off one market class for this mandate, on the page that shows it."""
    _set_mute(session, client_id, kind, muted=True)
    return RedirectResponse(f"/client/{client_id}/market", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/market/{kind}/unmute")
def unmute_signal_kind(
    client_id: int, kind: str, session: Session = Depends(get_db)
) -> Response:
    """Bring a muted class back. The way out has to be on the same page."""
    _set_mute(session, client_id, kind, muted=False)
    return RedirectResponse(f"/client/{client_id}/market", status_code=_SEE_OTHER)


@router.get("/client/{client_id}/market", response_class=HTMLResponse)
def market_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The client's market: four classes of signal, and who reported them.

    The theme radar is coverage of the client's subject that never names it —
    the material the positioning drafts are made of. Beside it now sit the three
    classes a news feed cannot carry: studies that stay citable for months,
    regulation dated in the future, and events with a stage and a deadline.

    Four independent sections rather than one ranked list, because they are read
    for different reasons and on different clocks. Mixing a consultation closing
    in five weeks into a feed sorted by what arrived this morning is how it gets
    delivered on the day it is too late to say anything.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    tz = _local_tz()
    now = dt.datetime.now(dt.UTC)
    signals = _signals_by_kind(session, client, now=now, tz=tz)
    return templates.TemplateResponse(
        request,
        "client_market.html",
        {
            "client": client,
            "items": _market_items(session, client_id, days=_MARKET_DAYS),
            "days": _MARKET_DAYS,
            "themes": list(client.keywords or []) + list(client.alert_topics or []),
            **_nominations(session, client_id, days=_MARKET_DAYS),
            "signals": signals,
            "muted_kinds": [
                kind.value for kind in SignalKind if client.mutes_signal(kind)
            ],
            "field_gap": _field_gap(
                client, [row for rows in signals.values() for row in rows]
            ),
            "deadline_weeks": _DEADLINE_SOON.days // _DAYS_PER_WEEK,
            "last_run": _fetch_last_run(session),
            "header_date": now.astimezone(tz).date(),
        },
    )
