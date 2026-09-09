"""The Desk: the agency's own morning, before any single mandate is opened.

"Wenn wir morgens ins System gehen, brauchen wir keinen Newsfeed oder eine
Artikelliste, sondern ein echtes Arbeitsdashboard."

Two routes and no logic: :mod:`newspulse.desk` assembles the page and this hands
it to a template. The second route is the one thing the Desk writes — an action
that happened outside the tool, logged so the contractual count is not
systematically short.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import desk
from ...models import Client, ClientAction
from .. import redirects
from ..app import get_db, templates
from ..mandates import mandate_or_404
from .today import _fetch_last_run, _local_tz

router = APIRouter()
_log = logging.getLogger(__name__)
_SEE_OTHER = 303


@router.get("/desk", response_class=HTMLResponse)
def desk_view(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """The morning page. Every figure on it is read from rows written elsewhere."""
    board = desk.build(session)
    return templates.TemplateResponse(
        request,
        "desk.html",
        {
            "board": board,
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.post("/desk/action")
def log_action(
    client_id: int = Form(...),
    title: str = Form(...),
    happened_on: str = Form(""),
    note: str = Form(""),
    redirect_to: str = Form("/desk"),
    session: Session = Depends(get_db),
) -> Response:
    """Record work that happened outside RauteOS, so the count is not short.

    A background call, a briefing, a visit to an editorial office: none of it
    touches this tool, and a quarter counted from released artefacts alone shows
    the agency doing less than it did — against a contractual figure, which is
    the one number a client conversation starts from.

    The mandate comes from the form rather than the path so the control can be
    one row under the table with a picker in it, and so it works with scripting
    off — a form whose action is rewritten by JavaScript posts to whichever
    mandate happened to sort first.

    ``happened_on`` is a date rather than a timestamp because that is what
    anybody remembers, and it defaults to today. An unparseable or absent date
    becomes today rather than rejecting the entry: the point of this form is
    that it gets used.
    """
    client = mandate_or_404(session, client_id)
    back = redirects.local_target(redirect_to, "/desk")
    what = (title or "").strip()
    if not what:
        # Nothing to record and nothing to say about it that the empty form does
        # not already say.
        return RedirectResponse(back, status_code=_SEE_OTHER)
    now = dt.datetime.now(dt.UTC)
    session.add(
        ClientAction(
            client_id=client.id,
            title=what[:200],
            happened_at=_when(happened_on, now),
            note=(note or "").strip(),
            logged_at=now,
        )
    )
    session.commit()
    _log.info("logged an off-tool action for %r: %s", client.name, what[:60])
    return RedirectResponse(back, status_code=_SEE_OTHER)


def _when(raw: str, now: dt.datetime) -> dt.datetime:
    """A ``YYYY-MM-DD`` from the form as tz-aware UTC, or now.

    Midday UTC rather than midnight, and the reason is display rather than
    arithmetic: the quarter comparison is done in UTC and midnight would be
    correct for it, but every timestamp in this tool is *rendered* in the local
    zone, and midnight UTC renders as the previous evening anywhere west of
    Greenwich. Midday is the same calendar day in every zone the tool can
    plausibly be read in, so the date somebody typed is the date they see.
    """
    text = (raw or "").strip()
    if not text:
        return now
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return now
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.UTC)
