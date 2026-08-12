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
from dataclasses import dataclass
from urllib.parse import urlencode

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from ... import gnews
from ...models import Analysis, Article, Category, Client, TopicHit, visible_coverage
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
        conditions.append(Article.source == source)
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


@router.get("/clients", response_class=HTMLResponse)
def clients_index(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Mandanten: the whole portfolio at a glance, each row a way into its archive.

    Counts are aggregated in two grouped queries rather than per client, so the
    page stays one round trip regardless of portfolio size.
    """
    day = dt.datetime.now(_local_tz()).date()
    start, end = _day_bounds_utc(day)

    relevant = visible_coverage()
    totals = dict(
        session.execute(
            select(Analysis.client_id, func.count())
            .where(relevant)
            .group_by(Analysis.client_id)
        ).all()
    )
    today_counts = dict(
        session.execute(
            select(Analysis.client_id, func.count())
            .join(Article, Article.id == Analysis.article_id)
            .where(relevant, Article.published_at >= start, Article.published_at < end)
            .group_by(Analysis.client_id)
        ).all()
    )

    clients = session.scalars(select(Client).order_by(Client.name)).all()
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
            today_count=today_counts.get(c.id, 0),
            total_count=totals.get(c.id, 0),
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
    """The radar's articles for this client, newest first."""
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


@router.get("/client/{client_id}/market", response_class=HTMLResponse)
def market_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The client's market: coverage of its subject that never names it.

    The material the positioning drafts are made of, and until now visible only
    as citations underneath one. Seeing it whole answers the question a draft
    cannot: why there was an opening today, or why there was not.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return templates.TemplateResponse(
        request,
        "client_market.html",
        {
            "client": client,
            "items": _market_items(session, client_id, days=_MARKET_DAYS),
            "days": _MARKET_DAYS,
            "themes": list(client.keywords or []) + list(client.alert_topics or []),
            **_nominations(session, client_id, days=_MARKET_DAYS),
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )
