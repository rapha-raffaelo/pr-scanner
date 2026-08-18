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
