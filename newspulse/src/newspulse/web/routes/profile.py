"""The mandate's deep-dive profile: what we know, and where we know it from."""

from __future__ import annotations

import datetime as dt
import logging
import threading

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import onboarding
from ... import profile as profiles
from ... import profile_refresh, stakeholders
from ...db import get_session
from ...models import Client, ClientFact, OnboardingAnswer, ProfileProposal
from .. import spawn
from ..mandates import mandate_or_404
from ..app import get_db, templates
from ..redirects import local_target
from ..runlock import guard as _run_guard
from . import stakeholder_ui
from .today import _fetch_last_run, _local_tz

router = APIRouter()
_log = logging.getLogger(__name__)
_SEE_OTHER = 303

# Said back to a click that landed on nothing. The review buttons carry row ids,
# and the 06:10 sweep replaces rows: a tab left open overnight posts ids that no
# longer exist, which is right — nothing it never showed gets swept up — but
# redirecting in silence leaves the reader watching a button do nothing and
# clicking it again. A flag in the query string rather than a session: it
# describes the redirect it rode in on and must not survive the next reload.
_STALE_FLAG = "veraltet"


def _back(client_id: int, *, acted: bool) -> RedirectResponse:
    """Back to the profile page, saying so when the click reached no rows."""
    query = "" if acted else f"?{_STALE_FLAG}=1"
    return RedirectResponse(
        f"/client/{client_id}/profil{query}", status_code=_SEE_OTHER
    )

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


def _pending(
    session: Session,
    client_id: int,
    *,
    facts: dict[str, ClientFact] | None = None,
    stored: dict[str, OnboardingAnswer] | None = None,
) -> list[profiles.Proposal]:
    """Everything on offer for this profile: the web research, and the kick-off.

    Two sources, one list, one accept button — the consultant is answering the
    same question about every line ("does this belong on the profile?") and
    splitting it across two panels would only ask him to notice which machine
    produced it.

    They are filtered differently, and that difference is the whole of DEC-2. A
    researched value is dropped where a person has already filled the field: the
    machine may fill a blank and correct itself, never overrule the consultant.
    A kick-off answer is not dropped, because the client contradicting the web is
    the case worth surfacing — it is only dropped when it says what the field
    already says, which is not a contradiction but a duplicate.

    ``facts`` and ``stored`` are the two tables this reads, passed in by a caller
    that already holds them: rendering the page needs the facts for the form and
    the answers for the completeness line anyway, and fetching each of them twice
    per render is two round trips for rows already in hand.
    """
    facts = profiles.stored(session, client_id) if facts is None else facts
    from_kickoff = [
        p for p in onboarding.to_proposals(session, client_id, stored=stored)
        if p.key not in facts or facts[p.key].value.strip() != p.value.strip()
    ]
    # One proposal per field. Where both have something to say about the same
    # line, the answer displaces the guess before either is shown: two checkboxes
    # writing the same field would make "accept both" mean whichever ran last.
    answered = {p.key for p in from_kickoff}
    researched = [
        profiles.Proposal(
            key=row.key,
            value=row.value,
            source_url=row.source_url,
            source_title=row.source_title,
            row_id=row.id,
        )
        for row in profile_refresh.outstanding(session, client_id)
        if row.key not in answered
        # The same three rules the branch applies before drawing a row: no
        # source is a machine asserting what it cannot back up, a row the
        # profile has caught up with is a contradiction between Paris and
        # Paris, and a hand-filled field is never overruled, only contradicted.
        and row.source_url
        and profile_refresh.contradicts(facts, row)
        and profile_refresh.may_replace(facts, row.key)
    ]
    return from_kickoff + researched


@router.get("/client/{client_id}/profil", response_class=HTMLResponse)
def profile_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    client = mandate_or_404(session, client_id)
    # Both tables read once and handed on. The proposals need the facts to know
    # what they would displace and the answers to know what to offer; the
    # completeness line needs the same answers again.
    facts = profiles.stored(session, client_id)
    # Only the fields the research may actually write: a proposal identical to
    # what is on file never becomes a row at all, and a proposal against a field a
    # person filled in by hand is a contradiction rather than an offer, which is
    # what DEC-2 locks as: never replace, only contradict.
    #
    # A proposal with no source is not drawn in either pile. It is a machine
    # asserting something it cannot back up, and a value the reader cannot check
    # is not a decision anyone should be asked to make. The refresh no longer
    # stores one (``profile_refresh._sourced``); this is the render side of the
    # same rule, for rows written before it existed.
    #
    # Nor is a row the profile has caught up with. The refresh files no proposal
    # that agrees with the profile, but the profile moves under a row that is
    # already on the pile: the consultant reads what the web says and types it in
    # himself, and the row would then be drawn as a contradiction between Paris
    # and Paris, with a button that records a refusal against the value he just
    # entered (see ``profile_refresh.contradicts``).
    proposed = [
        p
        for p in profile_refresh.outstanding(session, client_id)
        if p.source_url and profile_refresh.contradicts(facts, p)
    ]
    stored = onboarding.answers(session, client_id)
    pending = _pending(session, client_id, facts=facts, stored=stored)
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
            # How much of this mandate's own foundation exists. On the profile
            # because this is the page that reads as the mandate's file: a thin
            # profile beside a full questionnaire is a different problem from a
            # thin profile beside twenty unasked questions.
            "kickoff": onboarding.completeness(session, client_id, stored=stored),
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
            # The last click found none of the rows it named. The sweep had
            # replaced them, which is the rule working — nothing the reader never
            # saw was touched — but a button that appears to do nothing teaches
            # the reader that the page is broken.
            "stale_click": bool(request.query_params.get(_STALE_FLAG)),
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
    field by field: the profile is a list of keys in one module, and repeating
    every one of them in a signature would mean a new field is two edits, one of
    which is easy to forget — as the three this story added would have been.
    """
    client = mandate_or_404(session, client_id)
    form = await request.form()
    facts = profiles.stored(session, client_id)
    # The research and kick-off offers, which the page now renders *into* the
    # fields they are offers for rather than as a separate list above the form.
    # Not filtered again here: ``_pending`` has already applied DEC-2 — a
    # researched value against a field a person filled never becomes an offer,
    # it becomes a contradiction — so what is left may stand in the box, over an
    # empty field or over the machine's own earlier answer.
    offers = {p.key: p for p in _pending(session, client_id)}
    accepted: list[profiles.Proposal] = []
    for field in profiles.FIELDS:
        if field.key not in form:
            continue
        stored = facts.get(field.key)
        value = str(form[field.key])
        # Untouched machine answers keep their source; a changed one becomes
        # the consultant's, because that is what it now is.
        unchanged = stored is not None and stored.value == value.strip()
        # An offer that comes back exactly as proposed keeps the citation it was
        # proposed with. One that was typed over is the consultant's own — the
        # value is no longer what the source says, so the source must not follow
        # it. One that was emptied is not saved at all and stays on offer.
        offer = offers.get(field.key)
        took_offer = bool(
            offer is not None
            and value.strip()
            and offer.value.strip() == value.strip()
        )
        if took_offer and offer is not None:
            accepted.append(offer)
        profiles.save(
            session, client, field.key, value,
            source_url=(
                stored.source_url if unchanged and stored
                else offer.source_url if took_offer and offer else ""
            ),
            source_title=(
                stored.source_title if unchanged and stored
                else offer.source_title if took_offer and offer else ""
            ),
            filled_by=(
                stored.filled_by if unchanged and stored
                else (offer.filled_by or profiles.BY_HAND)
                if took_offer and offer
                else profiles.BY_HAND
            ),
            supersede=bool(offer.supersedes) if took_offer and offer else False,
        )
    # The researched rows that were taken are answered and must stop being
    # offered. The kick-off half has no row to clear: it is derived from the
    # questionnaire on every render and an accepted one simply stops matching.
    profile_refresh.clear(
        session, client_id, [o.row_id for o in accepted if o.row_id is not None]
    )
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/profil/fill")
def fill_profile(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Ask the web what it knows. Proposes; writes nothing."""
    mandate_or_404(session, client_id)
    if _researching.acquire(blocking=False):
        spawn.start_or_release(
            _run_research,
            args=(client_id,),
            name=f"newspulse-profile-{client_id}",
            release=_researching.release,
        )
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

    Sourceless rows are filtered here and not only at render, for the same reason
    the hand-filled rule is enforced twice: the form body is not the page. The
    refresh stores no such row any more and migration 0023 deleted the ones PRF-01
    left behind, so this is the boundary rather than the cleanup — a value nobody
    can check is not something a posted id gets to turn into a fact.
    """
    wanted = set(pid)
    return [
        p
        for p in profile_refresh.outstanding(session, client_id)
        if p.id in wanted and p.source_url
    ]


@router.post("/client/{client_id}/profil/accept")
async def accept_proposals(
    request: Request,
    client_id: int,
    pid: list[int] = Form(default_factory=list),
    key: list[str] = Form(default_factory=list),
    session: Session = Depends(get_db),
) -> Response:
    """Take what the consultant ticked, sources and all, as his own answer.

    Two kinds of proposal arrive here and they are named differently, for the
    reason each was built. A researched row is named by ``pid``: the 06:10 sweep
    can replace it between the page being drawn and the button being pressed, and
    a field name would then accept a value nobody read. A kick-off answer is
    named by ``key``, because it is derived from the stored answer on every
    render and only a person editing the questionnaire can change what it says.

    Both are stamped :data:`newspulse.profile.BY_HAND`. The model proposed and
    the client answered; in each case a person decided, and it is the decision
    that is worth recording — a fact somebody vouched for must not be proposed
    over by the next refresh, which is exactly what the human stamp buys. The
    source travels with it, so the page still shows where the value came from.

    Only what was named goes: the rest stay on offer, because a decision not
    made is not a decision to discard.

    A researched row against a hand-filled fact is refused here and not only
    hidden upstream. The page draws no accept button for one, but the form body
    is not the page: a tab left open while the field was typed into elsewhere
    posts a row the consultant never chose. That is DEC-2, enforced at the write
    boundary.

    A kick-off answer landing on a field the web already answered supersedes it
    rather than erasing it (DEC-2 option A): the answer wins, and what the web
    said stays underneath with its own citation until somebody drops it. That is
    not a conflict with the rule above — it displaces a researched value, never
    one a person typed.
    """
    client = mandate_or_404(session, client_id)
    form = await request.form()

    def _edited(proposal, name: str) -> str:
        """The text the reader left in the field, or the value as proposed.

        Only the *text* comes from the form. Which field it lands on still comes
        from the row named by id, so an edited value cannot become a way to write
        a field the 06:10 sweep replaced between the page being drawn and the
        button being pressed — the property the id naming was built for.

        A field emptied on purpose is a "not this one": nothing is written and
        the row stays on offer, because a decision not made is not a decision to
        discard. A row nobody touched arrives unchanged and is simply accepted.
        """
        typed = form.get(name)
        if typed is None:
            return proposal.value
        return str(typed).strip()

    facts = profiles.stored(session, client_id)
    taken = [
        p for p in _chosen(session, client_id, pid)
        if profile_refresh.may_replace(facts, p.key)
    ]
    written = []
    for proposal in taken:
        value = _edited(proposal, f"v{proposal.id}")
        if not value:
            continue
        written.append(proposal)
        profiles.save(
            session, client, proposal.key, value,
            source_url=proposal.source_url,
            source_title=proposal.source_title,
            filled_by=profiles.BY_HAND,
        )
    profile_refresh.clear(session, client_id, [p.id for p in written])

    # The kick-off half. No bookkeeping to do afterwards: these are derived from
    # the answers on every render, and an accepted one stops matching.
    wanted = set(key)
    kickoff_taken = [
        p for p in _pending(session, client_id) if p.key in wanted and p.from_person
    ]
    kickoff_written = []
    for proposal in kickoff_taken:
        value = _edited(proposal, f"k{proposal.key}")
        if not value:
            continue
        kickoff_written.append(proposal)
        profiles.save(
            session, client, proposal.key, value,
            source_url=proposal.source_url,
            source_title=proposal.source_title,
            # Its own author, not BY_HAND: "Kickoff-Fragebogen" says the client
            # answered this, which is a stronger claim than "the consultant
            # accepted it" and the one the page is built to print. Protected
            # from the sweep by profile_refresh.may_replace all the same.
            filled_by=proposal.filled_by or profiles.BY_HAND,
            supersede=proposal.supersedes,
        )
    return _back(client_id, acted=bool(written or kickoff_written) or not (pid or key))


@router.post("/client/{client_id}/profil/{key}/forget")
def forget_superseded(
    client_id: int, key: str, session: Session = Depends(get_db)
) -> Response:
    """Drop the older value standing beside a field, ending the disagreement.

    The way out: a superseded value is kept so the reader can see that the web
    said something else, not so it stays on the page forever.
    """
    mandate_or_404(session, client_id)
    profiles.forget_superseded(session, client_id, key)
    return RedirectResponse(f"/client/{client_id}/profil", status_code=_SEE_OTHER)


# --- The standing stakeholder map (RIS-03) -----------------------------------------
#
# The map's *routes* live here because the map is part of the mandate's file —
# it hangs on the client the way the profile does, and it is proposed from the
# profile's own lines. The map *renders* on the register page (the partial),
# because that is where it is worked with: beside the issues its selections
# hang on.

def _person(request: Request) -> str:
    """The signed-in person, or the token that says a person pressed the button.

    Every map row carries who set it; where sign-in is not configured the tool
    still knows a human submitted the form, so it writes the ``"mensch"`` token
    rather than inventing a name nobody typed.
    """
    return str(request.scope.get("user_email") or profiles.BY_HAND)


@router.post("/client/{client_id}/stakeholder/vorschlagen")
def propose_stakeholders(
    client_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Propose the map from the profile. Adds under the model's token, never
    overwrites a standing row, and never invents a contact.

    A mandate without profile entries gets no invented map: the note says what
    is missing, and the page carries the link to where it is filled in.
    """
    client = mandate_or_404(session, client_id)
    try:
        added = stakeholders.propose_card(session, client)
    except Exception:  # noqa: BLE001 — a button must answer, not 500
        # A fixed sentence, and the cause in the log: an interpolated exception
        # cannot stand in the i18n table, so it would render German on an
        # English page — and a ParseError's text is the model's malformed
        # answer, which is nothing a reader can act on.
        _log.exception("stakeholder proposal for client %s failed", client_id)
        stakeholder_ui.note(client_id, stakeholder_ui.PROPOSAL_FAILED)
    else:
        if added is None:
            stakeholder_ui.note(client_id, stakeholder_ui.NO_PROFILE)
        elif not added:
            stakeholder_ui.note(client_id, stakeholder_ui.NO_NEW_GROUPS)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/client/{client_id}/stakeholder/save")
def save_stakeholder(
    request: Request,
    client_id: int,
    group: str = Form(""),
    betroffenheit: str = Form(""),
    einfluss: str = Form("mittel"),
    contact: str = Form(""),
    channel: str = Form(""),
    row_id: int | None = Form(None),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """A person writes one row of the map; the row records who.

    An out-of-set Einfluss is refused without a trace of a write: the form
    only offers the three levels, so anything else was submitted around it,
    and a defaulted level would store a value nobody chose, under a name.
    """
    client = mandate_or_404(session, client_id)
    try:
        row = stakeholders.save_row(
            session,
            client,
            group=group,
            betroffenheit=betroffenheit,
            einfluss=einfluss,
            contact=contact,
            channel=channel,
            by=_person(request),
            row_id=row_id,
        )
    except ValueError:
        _log.info("out-of-set stakeholder level for client %s refused", client_id)
        row = None
    if row is None:
        stakeholder_ui.note(client_id, stakeholder_ui.ROW_NOT_SAVED)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/client/{client_id}/stakeholder/{row_id}/delete")
def delete_stakeholder(
    client_id: int,
    row_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Remove one row of the map. A stale id costs nothing rather than a 500."""
    client = mandate_or_404(session, client_id)
    stakeholders.delete_row(session, client, row_id)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


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
    mandate_or_404(session, client_id)
    refused = profile_refresh.discard(
        session, client_id, pid, now=dt.datetime.now(dt.UTC)
    )
    _log.info("profile proposals for client %s: %d discarded", client_id, refused)
    return _back(client_id, acted=bool(refused) or not pid)
