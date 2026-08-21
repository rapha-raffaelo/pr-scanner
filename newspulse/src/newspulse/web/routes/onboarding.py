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
from ...models import Client, OnboardingAnswer
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


def _shared(
    session: Session, client: Client, stored: dict[str, OnboardingAnswer]
) -> dict:
    """Context both renders on this page need: the mandate and the progress figure.

    The answers are handed in rather than read here: both renders already hold
    them, and the progress figure is a count of exactly those rows.

    The shared header's context is *not* here. A partial swaps back a fragment
    that never extends ``base.html``, so building ``last_run`` for it would run a
    query over ``runs`` and a count over ``articles`` on every blur, chip and
    skip, to render nothing.
    """
    return {
        "client": client,
        "progress": onboarding.completeness(session, client.id, stored=stored),
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
    stored = onboarding.answers(session, client.id)
    return templates.TemplateResponse(
        request,
        "partials/kickoff_saved.html",
        {
            **_shared(session, client, stored),
            "question": question,
            "answer": stored.get(question.key),
        },
    )


@router.get("/client/{client_id}/kickoff", response_class=HTMLResponse)
def kickoff_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The questionnaire as it stands: what is answered, skipped and still open."""
    client = _client_or_404(session, client_id)
    stored = onboarding.answers(session, client.id)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            **_shared(session, client, stored),
            # Only the full page extends ``base.html``, so only the full page
            # needs what the shared header renders.
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
            "groups": onboarding.by_section(),
            "answers": stored,
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


def _entry_index(raw: str) -> int | None:
    """The index of the chip to drop, or ``None`` if the form did not name one.

    Taken as text and parsed here rather than declared ``int`` on the route: a
    missing or malformed ``index`` would then be answered with FastAPI's raw 422
    JSON body, in the middle of a page where every other failure is a 404 or a
    no-op and every other response is HTML.
    """
    try:
        return int(raw)
    except ValueError:
        return None


@router.post("/client/{client_id}/kickoff/{key}/remove")
def remove_entry(
    request: Request,
    client_id: int,
    key: str,
    index: str = Form(default=""),
    session: Session = Depends(get_db),
) -> Response:
    """Drop one entry from a list answer, leaving its siblings alone.

    An index that names no chip changes nothing and re-renders the question as it
    stands, which is also what a stale delete button does after someone else's
    tab already removed that entry.
    """
    client = _client_or_404(session, client_id)
    question = _question_or_404(key)
    position = _entry_index(index)
    if position is not None:
        onboarding.remove_entry(session, client, key, position)
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
