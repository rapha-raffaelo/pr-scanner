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
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from ...models import Analysis, Article, Category, Client
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

# Only coverage the analyzer judged relevant belongs in a client's archive; a
# relevance of 0 means "this story does not concern this client" (a non-matching
# pair the analyzer may still persist). Same gate as the Today view, so the
# archive and the daily view agree on what counts as this client's coverage.
_MIN_RELEVANCE = 1


@dataclass(frozen=True, slots=True)
class ArchiveRow:
    """One rendered archive row — the same field set as a Today coverage row."""

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
        Analysis.relevance_score >= _MIN_RELEVANCE,
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
            Analysis.relevance_score >= _MIN_RELEVANCE,
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


@dataclass(frozen=True, slots=True)
class PortfolioRow:
    """One client as the Mandanten overview lists it."""

    id: int
    name: str
    industry: str | None
    alert_topics: list[str]
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

    relevant = Analysis.relevance_score >= _MIN_RELEVANCE
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
            alert_topics=list(c.alert_topics),
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
            "rows": [r for r in rows if not r.is_competitor],
            "benchmarks": [r for r in rows if r.is_competitor],
            "last_run": _fetch_last_run(session),
            "header_date": day,
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
            # Every other company, so a competitor can be added from this page.
            "candidates": [
                c for c in session.scalars(select(Client).order_by(Client.name)).all()
                if c.id != client.id and c not in client.competitors
            ],
            "competitors": list(client.competitors),
            "last_run": _fetch_last_run(session),
            # The shared header dates every page; the archive view spans many
            # days, so it shows today.
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )
