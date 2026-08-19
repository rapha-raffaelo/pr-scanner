"""The contact book: the one place in the tool that holds contact details.

Reached from a pitch list — click a journalist's name and land either on what you
recorded about them last time, or on a prefilled form one edit away from having
it. That is the whole interaction the consultant asked for, and it is also the
only honest way to have contact details here at all: the tool proposes who to
approach from what the press actually published, and the person supplies how.

Since the outreach ledger, the book is also the relationship file (DEC-2): pick a
journalist (``?id=``) and read everything ever released at them, across all
mandates, with what came of it. Across mandates deliberately — a journalist is a
relationship the agency holds, and "have we already gone to her with something
this month" has no answer inside one client's workspace. The mandate is named on
every line instead.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import config, contacts, outreach
from ..app import get_db, templates
from .today import _fetch_last_run

router = APIRouter()

_SEE_OTHER = 303


def _days_since_last(history: list[outreach.HistoryEntry]) -> int | None:
    """Calendar days since the newest released letter, or ``None`` with none.

    Counted between *local calendar dates* rather than as elapsed hours, because
    the template turns 0 and 1 into the words "heute" and "gestern" and every date
    beside them is rendered in the reader's zone. Elapsed time would say "zuletzt
    angeschrieben heute" over a timeline entry dated yesterday for any letter
    released late in the evening. ``days_out`` on the letter card keeps the
    elapsed arithmetic: it says "seit 14 Tagen", never a calendar word.

    Computed here rather than in the template because it is arithmetic on a
    timestamp, and Jinja is the wrong place for that.
    """
    if not history:
        return None
    zone = config.local_zone()
    latest = history[0].letter.released_at.astimezone(zone).date()
    return max((dt.datetime.now(zone).date() - latest).days, 0)


def _page_context(
    session: Session,
    *,
    q: str = "",
    editing: contacts.Contact | None = None,
    selected: contacts.Contact | None = None,
    prefill_name: str = "",
    prefill_outlet: str = "",
    came_from_pitch: bool = False,
    error: str = "",
) -> dict:
    """Everything contacts.html renders, in one place — both the ordinary GET
    and the failed-save re-render go through it, so neither can miss a key the
    two-pane template needs."""
    history = (
        outreach.history_for_contact(session, selected.id) if selected else []
    )
    context = {
        "contacts": contacts.list_all(session, q),
        "search": q,
        "editing": editing,
        "selected": selected,
        "history": history,
        "timeline": outreach.timeline(history),
        "tallies": outreach.tally(history),
        "letter_counts": outreach.released_count_by_contact(session),
        "last_written_days": _days_since_last(history),
        "state_labels": outreach.STATE_LABELS,
        "prefill_name": prefill_name,
        "prefill_outlet": prefill_outlet,
        "came_from_pitch": came_from_pitch,
        "last_run": _fetch_last_run(session),
        "header_date": dt.datetime.now(config.local_zone()).date(),
    }
    if error:
        context["error"] = error
    return context


@router.get("/contacts", response_class=HTMLResponse)
def contact_book(
    request: Request,
    q: str = "",
    search: str = "",
    name: str = "",
    outlet: str = "",
    edit: int | None = None,
    # The URL keeps ``?id=`` — the alias — while the local name does not shadow
    # the builtin ``id`` for the rest of the function.
    contact: int | None = Query(None, alias="id"),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The book, the form for one entry, and the file of one journalist.

    ``?name=`` and ``?outlet=`` come from a pitch list. If that byline is already
    recorded the page opens on it; if not, the form is prefilled with what the
    feed knew, so recording a new contact is one field and a save.

    ``?id=`` selects a contact and opens their file: every released letter across
    all mandates, newest first, with the four tallies over it. An id that matches
    nothing — a deleted contact, a stale link — renders the plain book rather
    than an error, because there is nothing broken about the page itself.

    The roster filter arrives as ``?q=`` — what the search box submits — and is
    also read from ``?search=``, the name the book itself uses for the term
    (:func:`newspulse.contacts.list_all`) and the one a hand-written link tends
    to carry. ``?q=`` wins when both are present, because that one came from the
    form the reader actually typed into.
    """
    term = q or search
    existing = contacts.find(session, name, outlet) if name else None
    editing = session.get(contacts.Contact, edit) if edit else existing
    selected = session.get(contacts.Contact, contact) if contact else None

    return templates.TemplateResponse(
        request,
        "contacts.html",
        _page_context(
            session,
            q=term,
            editing=editing,
            selected=selected,
            # What the pitch list knew, for a contact that does not exist yet.
            prefill_name=name if editing is None else "",
            prefill_outlet=outlet if editing is None else "",
            came_from_pitch=bool(name),
        ),
    )


@router.post("/contacts")
def save_contact(
    request: Request,
    contact_id: str = Form(""),
    name: str = Form(...),
    outlet: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    beat: str = Form(""),
    notes: str = Form(""),
    redirect_to: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Create or update one entry, then go back where the reader came from."""
    back = (
        redirect_to
        if redirect_to.startswith("/") and "//" not in redirect_to
        else "/contacts"
    )
    try:
        contacts.save(
            session,
            contact_id=int(contact_id) if contact_id.strip().isdigit() else None,
            name=name,
            outlet=outlet,
            email=email,
            phone=phone,
            beat=beat,
            notes=notes,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "contacts.html",
            _page_context(
                session,
                prefill_name=name,
                prefill_outlet=outlet,
                error=str(exc),
            ),
        )
    return RedirectResponse(back, status_code=_SEE_OTHER)


@router.post("/contacts/{contact_id}/delete")
def delete_contact(
    contact_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    contacts.delete(session, contact_id)
    return RedirectResponse("/contacts", status_code=_SEE_OTHER)
