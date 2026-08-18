"""The competitor landscape: who else is being written about, and where.

Scattered across three pages before this: the comparison set sat under the
archive, share of voice sat beside it, the outlet gaps lived in the Coverage Map,
and the suggestions were on the settings screen. Four places, one question — *how
does this mandate stand against the field it competes in* — so they are one tab.

Nothing here is new machinery. It is the existing reporting assembled where the
question is actually asked.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import coverage_map, reporting
from ...models import Analysis, Article, Client, visible_coverage
from .. import themework
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

#: The window every number on this page shares. One window, stated once: two
#: figures on the same screen measured over different periods is the fastest way
#: to make a page untrustworthy.
WINDOW_DAYS = 30

#: Headlines shown per competitor. Enough to see what they are being written
#: about, few enough that the page stays a comparison rather than a feed.
_PER_RIVAL = 3


def _recent_for(session: Session, client_id: int, since: dt.datetime, limit: int):
    return session.scalars(
        select(Article)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
            Article.published_at >= since,
        )
        .order_by(Article.published_at.desc())
        .limit(limit)
    ).all()


@router.get("/client/{client_id}/wettbewerb", response_class=HTMLResponse)
def rivals_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=WINDOW_DAYS)
    voice = reporting.share_of_voice(session, client, days=WINDOW_DAYS)
    grid = coverage_map.build(session, client, days=90)

    # One row per competitor: what it is called, how loud it was, and the last
    # things written about it. The mandate's own row is the yardstick and stays
    # at the top of the table rather than in this list.
    rivals = [
        {
            "client": rival,
            "mentions": next((v.mentions for v in voice if v.client_id == rival.id), 0),
            "recent": _recent_for(session, rival.id, since, _PER_RIVAL),
        }
        for rival in client.competitors
    ]
    rivals.sort(key=lambda r: -r["mentions"])

    # Who is on the field and could be compared against, minus the ones already
    # linked. Same-field only: a fashion brand offered to a finance platform was
    # reported twice and is not coming back.
    linked = {c.id for c in client.competitors} | {client.id}
    candidates = [
        other
        for other in session.scalars(
            select(Client).where(Client.is_competitor.is_(True)).order_by(Client.name)
        ).all()
        if other.id not in linked
        and client.industry
        and (other.industry or "").casefold() == (client.industry or "").casefold()
    ]

    return templates.TemplateResponse(
        request,
        "client_rivals.html",
        {
            "client": client,
            "voice": voice,
            "rivals": rivals,
            "candidates": candidates,
            "gaps": grid.gaps[:8],
            "gap_total": len(grid.gaps),
            "window_days": WINDOW_DAYS,
            "rival_work": themework.rivals_job.state.get(client_id)
            if hasattr(themework, "rivals_job") else None,
            "own_mentions": next((v.mentions for v in voice if v.client_id == client.id), 0),
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )
