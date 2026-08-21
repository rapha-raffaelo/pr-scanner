"""The kick-off questionnaire: twenty questions, answered one at a time.

DEC-1 option A — the consultant fills this in the tool, during or right after the
call. It needs no public URL and no second authentication surface, and the person
who heard the answer is the one typing it.

Everything about these routes follows from one promise the page makes: an answer
is stored the moment it is given. So there is a route per answer rather than one
form submit at the end, and an htmx save swaps back only the question it touched
plus the progress rail. Re-rendering the whole page would be a third of the code
and would wipe every other half-typed answer on screen, which is the exact loss
the questionnaire promises not to cause.

Without JavaScript the same routes still work: each question is a real form with
a real submit button, and a non-htmx post answers with a redirect back to the
question's own anchor.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import onboarding
from ...models import Client
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_SEE_OTHER = 303


def _client_or_404(session: Session, client_id: int) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def _question_or_404(key: str) -> onboarding.Question:
    question = onboarding.QUESTIONS_BY_KEY.get(key)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


def _shared(session: Session, client: Client) -> dict:
    """Context every render on this page needs, including the shared header's."""
    return {
        "client": client,
        "progress": onboarding.completeness(session, client.id),
        "last_run": _fetch_last_run(session),
        "header_date": dt.datetime.now(_local_tz()).date(),
    }


def _saved(
    request: Request,
    session: Session,
    client: Client,
    question: onboarding.Question,
) -> Response:
    """Answer one save: the question that changed, and the rail that counts it.

    The rail comes back as an out-of-band swap rather than as part of the target,
    because it is the one thing outside the question whose value just changed —
    a figure that still said 12/20 after the thirteenth answer would make the
    page look like it had not saved.
    """
    if not request.headers.get("hx-request"):
        return RedirectResponse(
            f"/client/{client.id}/kickoff#q-{question.key}", status_code=_SEE_OTHER
        )
    return templates.TemplateResponse(
        request,
        "partials/kickoff_saved.html",
        {
            **_shared(session, client),
            "question": question,
            "answer": onboarding.answers(session, client.id).get(question.key),
        },
    )


@router.get("/client/{client_id}/kickoff", response_class=HTMLResponse)
def kickoff_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The questionnaire as it stands: what is answered, skipped and still open."""
    client = _client_or_404(session, client_id)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            **_shared(session, client),
            "groups": onboarding.by_section(),
            "answers": onboarding.answers(session, client.id),
        },
    )


@router.post("/client/{client_id}/kickoff/{key}")
def save_answer(
    request: Request,
    client_id: int,
    key: str,
    value: str = Form(default=""),
    session: Session = Depends(get_db),
) -> Response:
    """Store one answer. A list question gets one more entry; the rest overwrite.

    An emptied field is not a delete. This route is what the ``change`` trigger
    fires on blur, so treating a blank value as "remove the row" would mean a
    consultant who selects a stored answer to retype it and is interrupted comes
    back to nothing — the client's own words from a call, gone with no undo. The
    deliberate way back to unanswered is ``/clear``, which the page offers next to
    every answered question.
    """
    client = _client_or_404(session, client_id)
    question = _question_or_404(key)
    if question.is_list:
        onboarding.add_entry(session, client, key, value)
    elif value.strip():
        onboarding.save_answer(session, client, key, value)
    return _saved(request, session, client, question)


@router.post("/client/{client_id}/kickoff/{key}/remove")
def remove_entry(
    request: Request,
    client_id: int,
    key: str,
    index: int = Form(...),
    session: Session = Depends(get_db),
) -> Response:
    """Drop one entry from a list answer, leaving its siblings alone."""
    client = _client_or_404(session, client_id)
    question = _question_or_404(key)
    onboarding.remove_entry(session, client, key, index)
    return _saved(request, session, client, question)


@router.post("/client/{client_id}/kickoff/{key}/skip")
def skip_question(
    request: Request, client_id: int, key: str, session: Session = Depends(get_db)
) -> Response:
    """Pass over one question deliberately, which is not the same as ignoring it."""
    client = _client_or_404(session, client_id)
    question = _question_or_404(key)
    onboarding.skip(session, client, key)
    return _saved(request, session, client, question)


@router.post("/client/{client_id}/kickoff/{key}/clear")
def clear_answer(
    request: Request, client_id: int, key: str, session: Session = Depends(get_db)
) -> Response:
    """Put a question back to unanswered.

    The way out of an accidental skip, and the only way to delete a stored
    answer: emptying the field does nothing, so losing a transcribed answer takes
    a deliberate click rather than a stray blur.
    """
    client = _client_or_404(session, client_id)
    question = _question_or_404(key)
    onboarding.save_answer(session, client, key, "")
    return _saved(request, session, client, question)
