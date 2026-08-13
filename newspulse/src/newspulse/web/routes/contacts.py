"""The contact book: the one place in the tool that holds contact details.

Reached from a pitch list — click a journalist's name and land either on what you
recorded about them last time, or on a prefilled form one edit away from having
it. That is the whole interaction the consultant asked for, and it is also the
only honest way to have contact details here at all: the tool proposes who to
approach from what the press actually published, and the person supplies how.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import config, contacts
from ..app import get_db, templates
from .today import _fetch_last_run

router = APIRouter()

_SEE_OTHER = 303


@router.get("/contacts", response_class=HTMLResponse)
def contact_book(
    request: Request,
    q: str = "",
    name: str = "",
    outlet: str = "",
    edit: int | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The book, and the form for one entry.

    ``?name=`` and ``?outlet=`` come from a pitch list. If that byline is already
    recorded the page opens on it; if not, the form is prefilled with what the
    feed knew, so recording a new contact is one field and a save.
    """
    existing = contacts.find(session, name, outlet) if name else None
    editing = session.get(contacts.Contact, edit) if edit else existing

    return templates.TemplateResponse(
        request,
        "contacts.html",
        {
            "contacts": contacts.list_all(session, q),
            "search": q,
            "editing": editing,
            # What the pitch list knew, for a contact that does not exist yet.
            "prefill_name": name if editing is None else "",
            "prefill_outlet": outlet if editing is None else "",
            "came_from_pitch": bool(name),
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(config.local_zone()).date(),
        },
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
            {
                "contacts": contacts.list_all(session),
                "search": "",
                "editing": None,
                "prefill_name": name,
                "prefill_outlet": outlet,
                "came_from_pitch": False,
                "error": str(exc),
                "last_run": _fetch_last_run(session),
                "header_date": dt.datetime.now(config.local_zone()).date(),
            },
        )
    return RedirectResponse(back, status_code=_SEE_OTHER)


@router.post("/contacts/{contact_id}/delete")
def delete_contact(
    contact_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    contacts.delete(session, contact_id)
    return RedirectResponse("/contacts", status_code=_SEE_OTHER)
