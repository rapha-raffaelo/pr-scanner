"""Declaring a crisis, and standing it down. The two buttons DEC-1 locked.

DEC-1 chose option A: the tool proposes, a person declares. Everything else in
:mod:`newspulse.crisis` is arithmetic over stored rows and could have run itself;
these two routes are the part that may not. Above the threshold Heute shows an
offer and nothing changes — no tighter cadence, no text, no extra notification —
until somebody presses ``Krise erklären`` here. A false alarm then costs one
click rather than a morning in emergency mode, which is the whole reason the
decision was locked that way.

Both endpoints are POST-and-redirect for the same reason the triage buttons are:
they are one-click actions inside a list that may have been open in another tab
since before a sweep, and a stale id must cost nothing rather than the page.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import crisis
from ...models import Article, Client, Crisis
from ..app import get_db
from ..redirects import local_target

router = APIRouter()

_log = logging.getLogger(__name__)

_SEE_OTHER = 303


def _who(request: Request) -> str:
    """The signed-in person, or the token that says a person pressed the button.

    ``declared_by`` exists to record that a *human* decided. Where sign-in is not
    configured the tool still knows that much — the route is only reachable from
    a form somebody submitted — so it writes :data:`newspulse.crisis.DECLARED_BY_DEFAULT`
    rather than inventing a name nobody typed.
    """
    return str(request.scope.get("user_email") or crisis.DECLARED_BY_DEFAULT)


@router.post("/crisis/declare")
def declare_crisis(
    request: Request,
    client_id: int = Form(...),
    article_id: int = Form(...),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Turn the offer on Heute into a declared crisis.

    A missing mandate or article is a no-op: the offer is rendered from rows that
    a dismissal or a re-match can remove while the page sits open, and losing the
    dashboard over that would be worse than doing nothing.

    So is a mandate that is not on the roster the offer came from — a competitor,
    or one deactivated while the page sat open. Heute only ever offers for active
    non-competitor mandates, and a crisis declared past that filter would be a
    trap: the cadence would fetch and analyse it every hour while no page renders
    it and no button can close it.

    A second submission is not a second crisis — :func:`newspulse.crisis.declare`
    hands back the standing one — so a double click, a second tab and a reload of
    the POST all land on the same row.
    """
    client = session.get(Client, client_id)
    article = session.get(Article, article_id)
    if (
        client is not None
        and article is not None
        and client.active
        and not client.is_competitor
    ):
        declared = crisis.declare(session, client, article, by=_who(request))
        _log.info(
            "crisis %d declared for %r from the dashboard", declared.id, client.name
        )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/crisis/{crisis_id}/close")
def close_crisis(
    crisis_id: int,
    reason: str = Form(""),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Stand a crisis down. The reason is required, and it is required here.

    An empty reason returns to the page without closing anything rather than
    raising: the field is marked ``required`` in the form, so an empty one
    reaching this point means the form was submitted around the browser, and a
    500 is not the answer to that. ``close`` itself still refuses it, so the
    invariant lives in one place.
    """
    standing = session.get(Crisis, crisis_id)
    if standing is not None and reason.strip():
        crisis.close(session, standing, reason=reason)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)
