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
from ...models import Client, ProfileProposal
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
    """Read the web for one mandate on a worker thread; always release the lock.

    Routed through :func:`profile_refresh.refresh` rather than calling the
    research directly, so a click and the 06:10 sweep produce the same rows by
    the same rules. One consequence is deliberate and worth stating: a click now
    stamps ``profile_checked_at`` too, which takes the mandate out of the sweep's
    age rotation for the next sixty days. That is the honest record — the profile
    really was re-read this morning, by a person — and re-reading it again
    unattended a day later would spend the daily budget on the answer we already
    have. A click that *fails* leaves its note, which keeps the mandate due.
    """
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
    # person filled in by hand is a contradiction rather than an offer, which is
    # what DEC-2 locks as: never replace, only contradict.
    #
    # A proposal with no source is not drawn in either pile. It is a machine
    # asserting something it cannot back up, and a value the reader cannot check
    # is not a decision anyone should be asked to make. The refresh no longer
    # stores one (``profile_refresh._unrefused``); this is the render side of the
    # same rule, for rows written before it existed.
    proposed = [p for p in profile_refresh.outstanding(session, client_id) if p.source_url]
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
            # Held back from the list above, and still on file. Handed over as
            # rows rather than a count so the page can name them in its own
            # discard form: a row nobody can see and nobody can clear sits there
            # until the next refresh overwrites it, which is the sort of
            # invisible state this feature exists to end.
            "contradictions": [
                p for p in proposed if not profile_refresh.may_replace(facts, p.key)
            ],
            "researching": _researching.locked(),
            # The click's own answer if there was one in this process, otherwise
            # what the last check recorded — which is the usual case, since the
            # sweep researches at 06:10 and the page is opened at nine. Without
            # the fallback a failure from the sweep is invisible: the page shows
            # a profile that was "checked" with no reason and no way to find one.
            # Exactly what ``advisory.py`` does with ``impulse_note``.
            "research_error": _errors.get(client_id) or client.profile_note,
            # The same value object the portfolio prints, so "never checked" and
            # "checked 84 days ago" read identically on both pages.
            "checked": profiles.checked(
                client.profile_checked_at, now=dt.datetime.now(dt.UTC)
            ),
            # Compared against, never printed: the page says "Ihre Angabe" where
            # the column says "mensch". Passed rather than written into the
            # template so the authority level has one definition.
            "by_hand": profiles.BY_HAND,
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


def _chosen(
    session: Session, client_id: int, pid: list[int]
) -> list[ProfileProposal]:
    """The client's outstanding proposals the form actually named.

    Rows are named by id and never by field. A field name means "whatever is
    proposed for the CEO right now", which is not what the reader decided on: the
    06:10 sweep can replace that row between the page being drawn and the button
    being pressed, and accept-all would then take a value nobody has read. An id
    is the row that was on the screen, and a row that arrived after it was drawn
    is simply not in the list.

    Scoped to ``client_id`` as well as to the ids, so a posted id belonging to
    another mandate selects nothing rather than reaching across.
    """
    wanted = set(pid)
    return [p for p in profile_refresh.outstanding(session, client_id) if p.id in wanted]


@router.post("/client/{client_id}/profil/accept")
def accept_proposals(
    client_id: int,
    pid: list[int] = Form(default_factory=list),
    session: Session = Depends(get_db),
) -> Response:
    """Take the named proposals, sources and all, as the consultant's own answer.

    The accepted value is stamped :data:`newspulse.profile.BY_HAND` rather than
    with the model that read it. The model proposed; the person decided, and it is
    the decision that is worth recording — a fact he has vouched for must not be
    proposed over by the next refresh, which is exactly what the human stamp
    buys. The source travels with it, so the page can still show where the value
    came from even though a person put it there.

    Only the named rows go: the rest stay on offer rather than vanishing with the
    click, because a decision not made is not a decision to discard.

    A row against a hand-filled fact is refused here and not only hidden upstream.
    The page draws no accept button for one, but the form body is not the page: a
    tab left open while the field was typed into elsewhere posts a row the
    consultant never chose, and honouring it would replace what he wrote with what
    a model read. That is the DEC-2 rule, enforced at the write boundary.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    facts = profiles.stored(session, client_id)
    taken = [
        p for p in _chosen(session, client_id, pid)
        if profile_refresh.may_replace(facts, p.key)
    ]
    for proposal in taken:
        profiles.save(
            session, client, proposal.key, proposal.value,
            source_url=proposal.source_url,
            source_title=proposal.source_title,
            filled_by=profiles.BY_HAND,
        )
    profile_refresh.clear(session, client_id, [p.id for p in taken])
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/profil/discard")
def discard_proposals(
    client_id: int,
    pid: list[int] = Form(default_factory=list),
    session: Session = Depends(get_db),
) -> Response:
    """Refuse the named proposals, one row or the whole visible pile.

    Every button on the page — the per-row Verwerfen, "Alle verwerfen", and the
    one under the contradictions — posts the ids it was drawn with, so each acts
    on precisely what its reader saw. There is deliberately no "no ids means
    everything" fallback: that used to be the discard-all, and it swept up
    whatever the sweep had added since the page was rendered.

    The rows are stamped rather than deleted, so the next refresh knows not to
    offer the same value again.
    """
    if session.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    refused = profile_refresh.discard(
        session, client_id, pid, now=dt.datetime.now(dt.UTC)
    )
    _log.info("profile proposals for client %s: %d discarded", client_id, refused)
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)
