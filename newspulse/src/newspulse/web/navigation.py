"""What the sidebar knows.

The mandates are the subject of this tool, not a destination inside it, so they
sit in the navigation itself rather than behind a link to a list of them. That
means every page needs the roster, and asking each of the twenty-odd routes to
pass a list it does not otherwise care about is how one of them ends up
forgetting and rendering a sidebar with nothing in it.

So it is a template global instead, reading the session off the request. That
detail matters: a fresh ``get_session()`` here would open the configured
database even under a test that overrode ``get_db`` with its own, which is both
a wrong answer and a stray file on disk. ``stash_db`` in ``app`` puts the
request's real session on ``request.state``, and this reads it back.

Two queries per render, both grouped, regardless of portfolio size.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Analysis, Article, Client, visible_coverage


@dataclass(frozen=True)
class NavClient:
    """One mandate, as the sidebar needs it."""

    id: int
    name: str
    logo_url: str | None
    active: bool
    #: Alerts published today. Drives the red badge, and only that: the sidebar
    #: says which mandate needs attention, the portfolio says why.
    alerts: int


def _today_bounds() -> tuple[dt.datetime, dt.datetime]:
    """Local "today", expressed in UTC, matching the portfolio's own day."""
    # Imported here rather than at module top: today.py imports from the app
    # module, which imports this one, and a top-level import closes that loop.
    from .routes.today import _day_bounds_utc, _local_tz

    return _day_bounds_utc(dt.datetime.now(_local_tz()).date())


def clients_for_nav(session: Session) -> list[NavClient]:
    """The mandate roster with today's alert count against each."""
    start, end = _today_bounds()
    alerts = dict(
        session.execute(
            select(Analysis.client_id, func.count())
            .join(Article, Article.id == Analysis.article_id)
            .where(
                visible_coverage(),
                Analysis.is_alert.is_(True),
                Article.published_at >= start,
                Article.published_at < end,
            )
            .group_by(Analysis.client_id)
        ).all()
    )
    # Benchmarks are excluded for the same reason they are excluded from the
    # portfolio: a competitor is something a mandate is measured against, not a
    # mandate of its own, and a flat list invites reading its coverage as work.
    rows = session.scalars(
        select(Client).where(Client.is_competitor.is_(False)).order_by(Client.name)
    ).all()
    return [
        NavClient(
            id=c.id,
            name=c.name,
            logo_url=c.logo_url,
            active=c.active,
            alerts=alerts.get(c.id, 0),
        )
        for c in rows
    ]


def nav_clients(request) -> list[NavClient]:
    """Template entry point. Empty rather than raising if no session is stashed,
    because a missing sidebar is a better failure than a 500 on every page."""
    session = getattr(request.state, "db", None)
    if session is None:
        return []
    return clients_for_nav(session)


@dataclass(frozen=True)
class NavPath:
    """Where the reader is, as the top bar's breadcrumb says it (GEL-03).

    Two crumbs at most: the workspace, and — inside a mandate's workspace — the
    mandate. Deeper than that would be a path of eleven tabs, which is a list,
    not a location.

    ``area`` is the German source string the sidebar already uses for the same
    destination, so the bar and the sidebar cannot disagree about what a place
    is called; the template runs it through ``t()``. ``mandate`` is a stored
    name and is never translated.
    """

    area: str
    area_href: str
    mandate: str | None = None
    mandate_href: str | None = None


#: Path prefix -> the sidebar's own label for it. Deliberately the same strings
#: the sidebar renders: a second vocabulary for the same four pages is how a
#: breadcrumb ends up naming a place the navigation does not. First match wins,
#: and no prefix here is a prefix of another.
_AREAS: tuple[tuple[str, str], ...] = (
    ("/today", "Heute"),
    ("/archive", "Archiv"),
    ("/contacts", "Kontakte"),
    ("/settings", "Einstellungen"),
)
#: Everything else, and the workspace a mandate's pages hang under.
_PORTFOLIO = "Portfolio"
_MANDATES = "Mandanten"


def _mandate_id(request) -> int | None:
    """The mandate the page being rendered is about, or None.

    Two ways to be about one, and both count: a ``/client/{id}/...`` path, and
    ``/today?client={id}`` — which is where the sidebar's mandate row actually
    lands (the route redirects), and which carries the workspace strip for
    exactly that reason. Reading only the path would leave the day's own crumb
    saying "Heute" over a page whose tab strip names a mandate.
    """
    parts = request.url.path.split("/")
    if len(parts) > 2 and parts[1] == "client" and parts[2].isdigit():
        return int(parts[2])
    chosen = request.query_params.get("client") if request.url.path == "/today" else None
    return int(chosen) if chosen and chosen.isdigit() else None


def _area(path: str) -> tuple[str, str]:
    """The workspace a path belongs to: its label and the page it opens on."""
    if path.startswith("/client/"):
        return _MANDATES, "/"
    for prefix, label in _AREAS:
        if path.startswith(prefix):
            return label, prefix
    return _PORTFOLIO, "/"


def nav_crumbs(request) -> NavPath:
    """The breadcrumb for the page being rendered.

    A template global for the same reason ``nav_clients`` is: the bar lives in
    the shared layout, and asking every route to pass a crumb it does not
    otherwise care about is how one of them ends up with an empty one.

    Every href it returns is a page this app already serves — the bar adds no
    destination of its own (DEC-4: an Anstrich, not an Umbau).
    """
    area, area_href = _area(request.url.path)
    mandate_id = _mandate_id(request)
    if mandate_id is None:
        return NavPath(area=area, area_href=area_href)

    # One row by primary key, not the roster: the sidebar has already paid for
    # the roster on this render and this needs exactly one name. A missing row
    # leaves the workspace crumb standing alone — the 404 under it says the
    # rest, and the bar invents no name.
    session = getattr(request.state, "db", None)
    client = session.get(Client, mandate_id) if session is not None else None
    if client is None:
        return NavPath(area=area, area_href=area_href)
    return NavPath(
        area=area,
        area_href=area_href,
        mandate=client.name,
        mandate_href=f"/client/{mandate_id}",
    )
