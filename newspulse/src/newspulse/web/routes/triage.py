"""Workflow actions on a piece of coverage: read, handled, flagged.

Without these the dashboard is a feed — identical every time it is opened, so on
a busy day the operator re-reads yesterday to find what is new. State is stored
per ``(article, client)`` analysis rather than per article: one story can be
handled for one mandate and still be open for another.

Deliberately not a soft delete. ``erledigt`` means "dealt with", not "hidden
forever": the archive and every report still count it, because coverage that
happened is coverage that happened. Only the operator's *working list* respects
the state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from ...models import Analysis, TriageState
from ..app import get_db

router = APIRouter()

_SEE_OTHER = 303


@router.post("/triage/{analysis_id}")
def set_triage_state(
    analysis_id: int,
    request: Request,
    state: str = Form(...),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Set one analysis's workflow state and return to where the click came from.

    An unknown state or a missing analysis is a no-op rather than an error: this
    is a one-click action in a list that may have been open in another tab since
    before a re-run, and losing the page over a stale id would be worse than
    quietly doing nothing.
    """
    try:
        new_state = TriageState(state)
    except ValueError:
        new_state = None

    if new_state is not None:
        analysis = session.get(Analysis, analysis_id)
        if analysis is not None:
            # Clicking the state an item already has clears it back to NEU, so the
            # same button both marks and un-marks.
            analysis.triage_state = (
                TriageState.NEU if analysis.triage_state is new_state else new_state
            )
            session.commit()

    # Only ever redirect within this app: the target comes from a form field, so
    # an absolute URL here would make the endpoint an open redirect.
    target = redirect_to if redirect_to.startswith("/") else "/"
    return RedirectResponse(target, status_code=_SEE_OTHER)
