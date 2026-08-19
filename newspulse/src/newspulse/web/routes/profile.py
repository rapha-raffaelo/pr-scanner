"""The mandate's deep-dive profile: what we know, and where we know it from."""

from __future__ import annotations

import datetime as dt
import logging
import threading

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import profile as profiles
from ... import profile_refresh
from ...db import get_session
from ...models import Client
from ..app import get_db, templates
from ..runlock import guard as _run_guard
from .today import _fetch_last_run, _local_tz

router = APIRouter()
_log = logging.getLogger(__name__)
_SEE_OTHER = 303

# One research run at a time, process-wide: it is a model call with a web search
# behind it, and a second click while one is running would spend another.
_researching = threading.Lock()

# Why the last research attempt produced nothing, per client. Still in memory,
# and only this: an error message is about the click that just happened, so a
# restart losing it costs nothing. The findings themselves are in the database
# (``profile_proposals``) because they are not — the nightly sweep produces them
# unattended, and a deploy dropping a pile of them silently is how a tool ends up
# having found something nobody ever saw.
_errors: dict[int, str] = {}


def _run_research(client_id: int) -> None:
    """Read the web for one mandate on a worker thread; always release the lock."""
    try:
        with _run_guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:
                    return
                _errors.pop(client_id, None)
                found = profile_refresh.refresh(
                    session, client, now=dt.datetime.now(dt.UTC)
                )
                _log.info("profile research for %r: %d proposal(s)", client.name, found)
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        _errors[client_id] = f"Die Recherche ist abgebrochen: {exc}"
        _log.exception("profile research failed")
    finally:
        _researching.release()


@router.get("/client/{client_id}/profil", response_class=HTMLResponse)
def profile_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    facts = profiles.stored(session, client_id)
    # Only the fields the research may actually write: a proposal identical to
    # what is on file never becomes a row at all, and a proposal against a field a
    # person filled in by hand is a contradiction rather than an offer — PRF-02
    # renders those under whatever DEC-2 locks as.
    proposed = profile_refresh.outstanding(session, client_id)
    pending = [p for p in proposed if profile_refresh.may_replace(facts, p.key)]
    return templates.TemplateResponse(
        request,
        "client_profile.html",
        {
            "client": client,
            "fields": profiles.FIELDS,
            "facts": facts,
            "filled": len(facts),
            "fillable": profiles.FILLABLE,
            "proposals": pending,
            # Held back from the list above, and still on file. Counted so the
            # page can offer to clear them: a row nobody can see and nobody can
            # discard is a row that sits there until the next refresh overwrites
            # it, which is the sort of invisible state this feature exists to end.
            "contradictions": len(proposed) - len(pending),
            "researching": _researching.locked(),
            "research_error": _errors.get(client_id, ""),
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.post("/client/{client_id}/profil")
async def save_profile(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> Response:
    """Save the whole form. Whatever a person typed here outranks the machine.

    Async because the form is read off the request body rather than declared
    field by field: the profile is a list of keys in one module, and repeating all
    fourteen of them in a signature would mean a new field is two edits, one of
    which is easy to forget.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    form = await request.form()
    facts = profiles.stored(session, client_id)
    for field in profiles.FIELDS:
        if field.key in form:
            stored = facts.get(field.key)
            value = str(form[field.key])
            # Untouched machine answers keep their source; a changed one becomes
            # the consultant's, because that is what it now is.
            unchanged = stored is not None and stored.value == value.strip()
            profiles.save(
                session, client, field.key, value,
                source_url=stored.source_url if unchanged and stored else "",
                source_title=stored.source_title if unchanged and stored else "",
                filled_by=(stored.filled_by if unchanged and stored else "mensch"),
            )
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/profil/fill")
def fill_profile(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Ask the web what it knows. Proposes; writes nothing."""
    if session.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if _researching.acquire(blocking=False):
        threading.Thread(
            target=_run_research, args=(client_id,), daemon=True,
            name=f"newspulse-profile-{client_id}",
        ).start()
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/profil/accept")
def accept_proposals(
    client_id: int,
    key: list[str] = Form(default_factory=list),
    session: Session = Depends(get_db),
) -> Response:
    """Take the proposed values for the named fields, sources and all.

    Only the ticked ones go: the rest stay on offer rather than vanishing with the
    click, because a decision not made is not a decision to discard.

    A key naming a hand-filled fact is refused here and not only hidden upstream.
    The page does not draw a checkbox for one, but the form body is not the page:
    a tab left open while the field was typed into elsewhere posts a key the
    consultant never ticked, and honouring it would replace what he wrote with
    what a model read and stamp the model's name on it.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    wanted = set(key)
    facts = profiles.stored(session, client_id)
    taken = [
        p for p in profile_refresh.outstanding(session, client_id)
        if p.key in wanted and profile_refresh.may_replace(facts, p.key)
    ]
    for proposal in taken:
        profiles.save(
            session, client, proposal.key, proposal.value,
            source_url=proposal.source_url,
            source_title=proposal.source_title,
            filled_by=proposal.proposed_by or profiles.config.review_model(),
        )
    if taken:
        profile_refresh.discard(session, client_id, [p.key for p in taken])
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/profil/discard")
def discard_proposals(client_id: int, session: Session = Depends(get_db)) -> Response:
    profile_refresh.discard(session, client_id)
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)
