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
#: Everything else, and the workspace a mandate's own pages hang under. The
#: sidebar calls "/" Portfolio; "Mandanten" is the section header above the
#: roster, which is not a link and not this destination. A crumb takes the word
#: for the page it opens, not the word above the row it came from — otherwise
#: clicking it lands the reader on a page whose sidebar highlights a
#: differently-named row.
_PORTFOLIO = "Portfolio"

#: SQLite holds a row id in a signed 64-bit integer and raises rather than
#: answering "no such row" when asked about a larger one. A reader can put any
#: number in ``?client=``, so an id is bounded here, before it reaches a query.
_MAX_ROW_ID = 2**63 - 1


def _as_id(raw: str | None) -> int | None:
    """``raw`` as a row id the database can actually be asked about, or None.

    Neither the parse nor the range is taken on trust. ``str.isdigit()`` is
    true for characters ``int()`` refuses ('²', '①'), and a number can be
    longer than the column can hold; either way a stray value has to leave the
    crumb standing rather than raise, which is the rule the whole bar follows.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 < value <= _MAX_ROW_ID else None


def _mandate_id(request) -> int | None:
    """The mandate the page being rendered is about, or None.

    Two ways to be about one, and both count: a ``/client/{id}/...`` path, and
    ``/today?client={id}`` — which is where the sidebar's mandate row actually
    lands (the route redirects), and which carries the workspace strip for
    exactly that reason. Reading only the path would leave the day's own crumb
    saying "Heute" over a page whose tab strip names a mandate.
    """
    parts = request.url.path.split("/")
    if len(parts) > 2 and parts[1] == "client":
        return _as_id(parts[2])
    if request.url.path != "/today":
        return None
    return _as_id(request.query_params.get("client"))


def _area(path: str) -> tuple[str, str]:
    """The workspace a path belongs to: its label and the page it opens on.

    A mandate's own pages fall through to the portfolio, which is both where
    the mandate is listed and where the crumb above it leads.
    """
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
    #
    # Deliberately unfiltered, where ``clients_for_nav`` drops benchmarks: this
    # is the same lookup ``client_detail`` makes, so it names whatever company
    # the page under it is already about. The roster excludes a competitor
    # because a work list should not invite reading its coverage as work — not
    # because its name is a secret from the page showing it.
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
