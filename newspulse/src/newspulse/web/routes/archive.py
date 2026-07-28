"""The portfolio-wide Archiv: every client's coverage in one filterable list.

The client detail view (NP-08) answers "what happened to *this* client". This
answers the cross-portfolio questions a monthly report is built from: what did
*Handelsblatt* write about anyone we represent, what happened across the whole
portfolio in *June*, where is a theme showing up across several mandates.

Filters compose as AND terms and run server-side against the full archive — the
same posture as the client view (DEC-3), so the page stays cheap as history grows
rather than shipping the archive to the browser. The month filter is the one
addition: an archive is browsed by month far more often than by an arbitrary day
range, and picking "Juli 2026" from a list beats typing two dates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from ...models import Analysis, Article, Category, Client
from ..app import get_db, templates
from .client import (
    _MIN_RELEVANCE,
    _PAGE_SIZE,
    _archive_conditions,
    _parse_category,
    Pagination,
)
from .today import _fetch_last_run, _local_tz

router = APIRouter()

# A month is exchanged as YYYY-MM: sortable, unambiguous, and the same shape the
# <select> options carry.
_MONTH_FORMAT = "%Y-%m"

_DE_MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


@dataclass(frozen=True, slots=True)
class ArchiveRow:
    """One row of the portfolio archive — an article as it concerns one client."""

    client_id: int
    client_name: str
    headline: str
    url: str
    source: str
    published_at: dt.datetime
    summary: str | None
    category: str
    importance: int
    is_alert: bool


@dataclass(frozen=True, slots=True)
class ArchiveFilters:
    """Applied filter values, echoed back so the form survives an HTMX swap."""

    client: str
    month: str
    with_competitors: bool
    source: str
    category: str
    search: str


@dataclass(frozen=True, slots=True)
class MonthOption:
    """One selectable month: the exchange value plus its German label."""

    value: str
    label: str


def _parse_month(raw: str | None) -> tuple[dt.date, dt.date] | None:
    """``YYYY-MM`` to its [first day, first day of next month) bounds.

    Returns ``None`` for anything unparseable so a hand-edited URL degrades to
    "no month filter" rather than a 500.
    """
    if not raw:
        return None
    try:
        start = dt.datetime.strptime(raw, _MONTH_FORMAT).date()
    except ValueError:
        return None
    # Roll over the year on December rather than constructing month 13.
    end = (
        dt.date(start.year + 1, 1, 1)
        if start.month == 12
        else dt.date(start.year, start.month + 1, 1)
    )
    return start, end


def _month_label(value: str) -> str:
    parsed = _parse_month(value)
    if parsed is None:
        return value
    start, _ = parsed
    return f"{_DE_MONTHS[start.month - 1]} {start.year}"


def _available_months(session: Session) -> list[MonthOption]:
    """Every month the archive actually holds coverage for, newest first.

    Computed over the whole archive rather than the current filter, so the
    dropdown does not shrink to a single option the moment a month is picked.
    Grouping is done in Python over distinct dates rather than with SQLite's
    strftime so the month is derived in *local* time — the same timezone the
    rows are displayed in, or a story near midnight would file under the wrong
    month.
    """
    tz = _local_tz()
    stamps = session.execute(
        select(Article.published_at)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(Analysis.relevance_score >= _MIN_RELEVANCE)
        .distinct()
    ).scalars().all()
    keys = {stamp.astimezone(tz).strftime(_MONTH_FORMAT) for stamp in stamps}
    return [MonthOption(value=k, label=_month_label(k)) for k in sorted(keys, reverse=True)]


def _available_sources(session: Session) -> list[str]:
    """Distinct publishers across the whole archive, for the filter dropdown."""
    return list(
        session.execute(
            select(Article.source)
            .join(Analysis, Analysis.article_id == Article.id)
            .where(Analysis.relevance_score >= _MIN_RELEVANCE)
            .distinct()
            .order_by(Article.source)
        ).scalars().all()
    )


def _available_clients(session: Session) -> list[Client]:
    """Every client, active or not: the archive keeps a deactivated client's
    history, so it must stay reachable here."""
    return list(session.scalars(select(Client).order_by(Client.name)).all())


def _parse_client(raw: str | None) -> int | None:
    """A client id, or ``None`` for "all clients"."""
    if not raw or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def _conditions(
    client_id: int | None,
    month: str,
    source: str,
    category: Category | None,
    search: str,
    with_competitors: bool = False,
) -> list[ColumnElement[bool]]:
    """The portfolio archive's WHERE clauses.

    Delegates to the client view's builder so both pages agree on what a filter
    means, then translates the month into the day-range it already understands.
    """
    bounds = _parse_month(month)
    date_from = bounds[0] if bounds else None
    # The shared builder's `date_to` is inclusive of that day, so step back from
    # the exclusive first-of-next-month to the last day of the selected month.
    date_to = bounds[1] - dt.timedelta(days=1) if bounds else None
    conditions = _archive_conditions(
        client_id, date_from, date_to, source, category, search
    )
    # Competitor coverage is archived like any other, but it is not the agency's
    # own work, so it stays out of the default view. Selecting a specific
    # company overrides this — asking for a competitor by name and being shown
    # nothing would be indefensible.
    if not with_competitors and client_id is None:
        conditions.append(
            Analysis.client_id.in_(select(Client.id).where(Client.is_competitor.is_(False)))
        )
    return conditions


def _count(session: Session, conditions: list[ColumnElement[bool]]) -> int:
    return session.execute(
        select(func.count())
        .select_from(Analysis)
        .join(Article, Analysis.article_id == Article.id)
        .where(*conditions)
    ).scalar_one()


def _fetch_page(
    session: Session, conditions: list[ColumnElement[bool]], page: int
) -> list[ArchiveRow]:
    """One page of rows, newest first, each carrying its client (this list spans
    the portfolio, so the client is part of the row rather than the page)."""
    tz = _local_tz()
    stmt = (
        select(Analysis, Article, Client)
        .join(Article, Analysis.article_id == Article.id)
        .join(Client, Analysis.client_id == Client.id)
        .where(*conditions)
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(_PAGE_SIZE)
        .offset((page - 1) * _PAGE_SIZE)
    )
    return [
        ArchiveRow(
            client_id=client.id,
            client_name=client.name,
            headline=article.title,
            url=article.url,
            source=article.source,
            published_at=article.published_at.astimezone(tz),
            summary=analysis.summary,
            category=analysis.category.value,
            importance=analysis.importance_score,
            is_alert=analysis.is_alert,
        )
        for analysis, article, client in session.execute(stmt).all()
    ]


def _page_url(filters: ArchiveFilters, page: int) -> str:
    """A filter-preserving URL for one page, used by the prev/next links."""
    params = [
        (name, value)
        for name, value in (
            ("client", filters.client),
            ("month", filters.month),
            ("source", filters.source),
            ("category", filters.category),
            ("q", filters.search),
            ("with_competitors", "1" if filters.with_competitors else ""),
        )
        if value
    ]
    if page > 1:
        params.append(("page", str(page)))
    query = urlencode(params)
    return "/archive" + (f"?{query}" if query else "")


def _paginate(
    filters: ArchiveFilters, page: int, total_pages: int, total: int
) -> Pagination:
    return Pagination(
        page=page,
        total_pages=total_pages,
        total_count=total,
        prev_url=_page_url(filters, page - 1) if page > 1 else None,
        next_url=_page_url(filters, page + 1) if page < total_pages else None,
    )


@router.get("/archive", response_class=HTMLResponse)
def archive_view(
    request: Request,
    client: str | None = None,
    month: str | None = None,
    source: str | None = None,
    category: str | None = None,
    q: str | None = None,
    with_competitors: bool = False,
    page: int = 1,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The portfolio archive, filtered by client, month, publisher, category, text."""
    filters = ArchiveFilters(
        client=(client or "").strip(),
        month=(month or "").strip(),
        source=(source or "").strip(),
        category=(category or "").strip(),
        search=(q or "").strip(),
        with_competitors=bool(with_competitors),
    )
    conditions = _conditions(
        _parse_client(filters.client),
        filters.month,
        filters.source,
        _parse_category(filters.category),
        filters.search,
        with_competitors=filters.with_competitors,
    )

    total = _count(session, conditions)
    total_pages = max(1, -(-total // _PAGE_SIZE))  # ceil
    current_page = min(max(page, 1), total_pages)
    rows = _fetch_page(session, conditions, current_page)

    return templates.TemplateResponse(
        request,
        "archive.html",
        {
            "rows": rows,
            "filters": filters,
            "clients": _available_clients(session),
            "months": _available_months(session),
            "sources": _available_sources(session),
            "categories": [c.value for c in Category],
            "pagination": _paginate(filters, current_page, total_pages, total),
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )
