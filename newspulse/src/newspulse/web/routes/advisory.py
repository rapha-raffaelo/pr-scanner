"""The Impulse view: what one client should be saying, and to whom.

The page held two panels for a while — a positioning draft from the market and a
"recommendation" from the client's own press — and the difference between them
was never legible from the outside: *"das ist wirklich nicht ganz klar wo der
unterschied liegt"*. Only one of them was a thing you can do. So the
recommendations panel is gone and its substance moved onto the impulse as a
button: :mod:`newspulse.outreach` turns a position into a message at a named
journalist, using the mandate's own coverage — the recommendation half's
material — as what makes the pitch personal.

Generation stays explicit: a button, not a side effect of the daily run. It costs
a model call per press, and a text that rewrote itself between opening the page
and reading it would be worse than none.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import angles, contacts, gmail_link, job, outreach, pitch
from ...db import get_session
from ..runlock import guard as _run_guard
from .. import themework
from ...models import Angle, Article, Client, Contact, Outreach, TopicHit
from ..app import get_db, templates
from . import assets_view
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_log = logging.getLogger(__name__)
_SEE_OTHER = 303


def _backing(
    messages: dict[int, list[Outreach]], targets: dict[int, list[pitch.PitchTarget]]
) -> dict[int, tuple[str, ...]]:
    """Headlines per stored message, matched on (journalist, outlet).

    Empty when the recipient was typed by hand or the radar has since moved on —
    which is honest: there is then nothing on file that the letter's opening line
    can be checked against.
    """
    out: dict[int, tuple[str, ...]] = {}
    for angle_id, rows in messages.items():
        by_who = {
            ((t.journalist or "").casefold(), t.outlet.casefold()): t.evidence
            for t in targets.get(angle_id, [])
        }
        for row in rows:
            found = by_who.get((row.journalist.casefold(), row.outlet.casefold()))
            if found:
                out[row.id] = found
    return out


def _silence(messages: dict[int, list[Outreach]]) -> dict[int, int]:
    """Days out, per letter that has gone quiet for too long.

    Only the silent ones are in the map, so the template asks one question
    ("is this letter in here?") instead of repeating the threshold. The value is
    the day count, because "seit 21 Tagen still" is actionable where "still"
    alone is a mood.
    """
    now = dt.datetime.now(dt.UTC)
    return {
        row.id: outreach.days_out(row, now=now)
        for rows in messages.values()
        for row in rows
        if outreach.is_silent(row, now=now)
    }


# --- Who a letter can actually be sent to ---------------------------------------
#
# The contact book is the only source of an address in this tool, and it is filled
# by hand. Nothing here derives one: a "vorname.nachname@medium.de" built from a
# byline is plausible, gets used, and reaches a stranger — which under DEC-4
# option C would mean RauteOS itself sent a client's pitch to the wrong person.
# So a missing address disables the action and says which of the three reasons it
# is, because they need three different answers from the reader.

#: No byline at all. The letter is addressed to a desk, and a desk has no entry.
NO_NAME = "Kein Name zu diesem Anschreiben — nur die Redaktion."
#: A name the book has never seen. One click away from being fixed.
NOT_IN_BOOK = "Nicht im Kontaktbuch. RauteOS leitet keine Adresse aus Name oder Medium ab."
#: In the book, but the address field was left empty.
NO_ADDRESS = "Im Kontaktbuch, aber ohne E-Mail-Adresse."
#: Not about the recipient at all: the mailbox is connected without the
#: permission that lets RauteOS compose and send. Every connection made before
#: DEC-4's send path is in this state, because Google fixes the granted scopes at
#: consent time and no request widens them afterwards.
NO_SEND_PERMISSION = "Dieses Postfach ist nur zum Lesen verbunden."


@dataclass(frozen=True, slots=True)
class Recipient:
    """The address one letter would go to, or why there is none.

    ``email`` is empty exactly when the letter cannot be sent, and ``reason`` is
    then never empty: a disabled button with no explanation is the thing this
    dataclass exists to prevent.
    """

    email: str = ""
    reason: str = ""
    contact_id: int | None = None

    @property
    def is_reachable(self) -> bool:
        return bool(self.email)


def _recipient(session: Session, row: Outreach) -> Recipient:
    """The contact book's answer for one letter's recipient.

    Prefers the contact the ledger already resolved at release
    (``Outreach.contact_id``) over a fresh name match: that link was made when a
    person released the letter, and it is the stronger fact — a journalist who
    has since moved masthead would otherwise match a different entry, or none.
    """
    known: Contact | None = None
    if row.contact_id is not None:
        known = session.get(Contact, row.contact_id)
    if known is None:
        if not row.journalist:
            return Recipient(reason=NO_NAME)
        known = contacts.find(session, row.journalist, row.outlet)
    if known is None:
        return Recipient(reason=NOT_IN_BOOK)
    if not known.email:
        return Recipient(reason=NO_ADDRESS, contact_id=known.id)
    return Recipient(email=known.email, contact_id=known.id)


def _recipients(
    session: Session, messages: dict[int, list[Outreach]]
) -> dict[int, Recipient]:
    """The address question answered once per letter, for the whole page."""
    return {
        row.id: _recipient(session, row)
        for rows in messages.values()
        for row in rows
    }


def _thread_links(
    messages: dict[int, list[Outreach]], link: gmail_link.Link | None
) -> dict[int, str]:
    """Where to open each letter's conversation in Gmail, for the ones that have
    one. Absent rather than empty for the rest, so the template asks one question
    instead of testing a string.

    The connected address goes into the link so it opens in *this* mailbox: a
    browser signed into a personal account as well has RauteOS's at ``/u/1/``,
    and an account-indexed link lands on "conversation not found".
    """
    account = link.email if link is not None else ""
    return {
        row.id: gmail_link.thread_url(row.gmail_thread_id, account=account)
        for rows in messages.values()
        for row in rows
        if row.gmail_thread_id
    }


def _advice_context(session: Session, client: Client) -> dict:
    """Everything advice.html renders for one mandate, in one place."""
    drafts = angles.for_client(session, client.id)
    targets = {a.id: pitch.targets_for(session, client, a) for a in drafts}
    messages = outreach.by_angle(session, [a.id for a in drafts])
    mailbox = gmail_link.connected()
    return {
        "client": client,
        "drafting": _drafting.locked(),
        "writing": _writing.locked(),
        # Why the last click came back empty, if it did. Shown instead of the
        # generic "the radar has collected nothing", which was wrong as often
        # as it was right.
        # The click's own answer if there was one this session, otherwise
        # what the last sweep recorded — which is the usual case, since the
        # sweep runs at 06:10 and the page is opened at nine.
        "impulse_refusal": _last_refusal.get(client.id) or client.impulse_note,
        "impulse_checked_at": client.impulse_checked_at,
        # The remedy for the commonest refusal, offered where the refusal is
        # read rather than on a settings screen the reader has never opened.
        "theme_work": themework.state.get(client.id),
        "angles": drafts,
        # The stories each draft rests on, keyed by angle id. The page's own
        # lead promises that "jede Aussage nennt die Meldungen, auf die sie
        # sich stützt" — and this card, the detailed view of a draft the Today
        # column already shows in full, was the one place that named none of
        # them. It showed strictly less than the overview it is reached from.
        "sources": {
            a.id: session.scalars(
                select(Article).where(Article.id.in_(a.article_ids or [-1]))
            ).all()
            for a in drafts
        },
        # Who to send each draft to, keyed by angle id. Computed per draft
        # because the strongest signal is specific to it: the bylines on the
        # very stories it answers.
        "pitch_targets": targets,
        # The messages already written off each impulse, keyed the same way.
        "messages": messages,
        # And, per message, the recipient's own headlines — the ones the
        # letter claims to have read. A pitch that says "Sie haben über X
        # geschrieben" has to be checkable where it is read, not two scrolls
        # down in the pitch list.
        "evidence": _backing(messages, targets),
        # The one letter state nobody enters, computed where the page is
        # built rather than in the template: it is a judgement about time,
        # and Jinja is the wrong place to do arithmetic on a timestamp.
        "silent": _silence(messages),
        "state_labels": outreach.STATE_LABELS,
        "outcomes": outreach.OUTCOMES,
        "message_error": _last_message_error.get(client.id, ""),
        # The mailbox, and what it makes possible on each card. ``gmail`` is
        # None or an unconnected Link when no mailbox is attached, and the card
        # then offers no send action at all — the Kopieren path is the whole
        # answer, exactly as it was before this feature existed. A connected
        # mailbox that was never granted the send permission (``may_send``)
        # renders the action disabled with ``gmail_scope_reason`` beside it,
        # because a button that 403s after the confirmation is worse than none.
        "gmail": mailbox,
        "gmail_scope_reason": NO_SEND_PERMISSION,
        "recipients": _recipients(session, messages),
        "gmail_threads": _thread_links(messages, mailbox),
        "gmail_error": _last_gmail_error.get(client.id, ""),
        # Whether a radar is possible at all, which is a question about the
        # client's themes — not about whether it has found anything yet. Read
        # off the hit count, a mandate with twenty-five themes and a radar that
        # has simply not run yet was told it had no radar.
        "has_themes": bool(client.keywords or client.alert_topics),
        # Rendered rather than written into the template, so the sentence and
        # the window can never disagree.
        "impulse_days": job.IMPULSE_LOOKBACK.days,
        "pitch_days": pitch.LOOKBACK_DAYS,
        # Two different numbers, and conflating them is why a page could say
        # "the radar collected 2 items and made nothing of them" about a
        # mandate that had no usable material at all. What the radar found is
        # ``market_seen``; what an impulse can be built from is
        # ``market_usable`` — inside the window, and not coverage of the
        # mandate itself, which is what the draft actually reads.
        "market_seen": session.scalar(
            select(func.count()).select_from(TopicHit).where(
                TopicHit.client_id == client.id
            )
        ) or 0,
        "market_usable": len(
            job.market_material(
                session,
                client,
                dt.datetime.now(dt.UTC) - job.IMPULSE_LOOKBACK,
            )
        ),
        "last_run": _fetch_last_run(session),
        "header_date": dt.datetime.now(_local_tz()).date(),
        # DEC-1: one occasion, one package. Every format for each impulse with
        # the state it is in, built from the registry crossed with what is
        # stored, so a seventh format appears in the strip without this route
        # learning its name. Handed the recipients and letters this function
        # already looked up rather than fetching them again.
        **assets_view.page_context(session, client, drafts, targets, messages),
    }


@router.get("/client/{client_id}/advice", response_class=HTMLResponse)
def advice_view(
    request: Request,
    client_id: int,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """This client's impulses, the messages written off them, and why not."""
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return templates.TemplateResponse(
        request, "advice.html", _advice_context(session, client)
    )


# One impulse at a time, process-wide: the draft shells out to `claude` and a
# second click would spend a second call on the same question.
_drafting = threading.Lock()

# Why the last click produced nothing, per client. The draft runs on a worker
# thread and the page is a later request, so the reason needs somewhere to wait —
# and "nothing happened" with no explanation is the single thing that made this
# button look broken. Deliberately in memory and deliberately not a schema
# change: it describes one click, not the mandate, and going stale on restart is
# the correct behaviour for a "what just happened" message.
_last_refusal: dict[int, str] = {}


def _run_impulse(client_id: int) -> None:
    """Draft one impulse on a worker thread; always release the guard.

    Holds the sweep's guard as well, so the header's wheel covers the wait and a
    sweep cannot start mid-draft and race it on the same articles.
    """
    try:
        with _run_guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:
                    return
                _last_refusal.pop(client_id, None)

                def _note(reason: str, *, target: Client = client) -> None:
                    """Both places: the dict answers *this* click, the column
                    survives the restart.

                    Memory alone meant a refusal vanished when the process
                    bounced, and the page fell back to a generic sentence about
                    the radar — the very sentence that keeps producing "es
                    funktioniert immer noch nicht" over a button that worked and
                    said so an hour earlier.
                    """
                    _last_refusal[client_id] = reason
                    target.impulse_note = reason
                    target.impulse_checked_at = dt.datetime.now(dt.UTC)
                    session.commit()

                drafted = job.draft_impulse(session, client, note=_note)
                if drafted:
                    _last_refusal.pop(client_id, None)
                    client.impulse_note = ""
                    client.impulse_checked_at = dt.datetime.now(dt.UTC)
                    session.commit()
                _log.info(
                    "impulse request for %r: %s",
                    client.name,
                    "drafted" if drafted else "no opening found",
                )
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # A crash must not read as "nothing worth saying": the reader would go on
        # believing the market was quiet.
        _last_refusal[client_id] = (
            f"Der Entwurf ist mit einem Fehler abgebrochen: {exc}. "
            "Details stehen im Log."
        )
        _log.exception("impulse request failed")
    finally:
        _drafting.release()


@router.post("/client/{client_id}/themes")
def suggest_themes_here(
    client_id: int, session: Session = Depends(get_db)
) -> Response:
    """Propose themes for this client, from the page where their absence hurts.

    The refusal above names the cause — "the themes are written too close to the
    company" — and a message that names a cause has to carry its remedy. Sending
    the reader to the settings screen to find a button they have never seen is how
    a fix goes unused: the same report came back three times while the remedy sat
    one page away.
    """
    if session.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    themework.start(session, client_id)
    return RedirectResponse(f"/client/{client_id}/advice", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/impulse")
def request_impulse(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Draft an impulse now, from this client's themes.

    The sweep only drafts from material that arrived that morning, which leaves a
    mandate with nothing to show on a quiet day even though its field may have
    plenty worth saying. This asks the question directly, over a wider window.
    """
    if session.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if _drafting.acquire(blocking=False):
        threading.Thread(
            target=_run_impulse,
            args=(client_id,),
            daemon=True,
            name=f"newspulse-impulse-{client_id}",
        ).start()
    return RedirectResponse(f"/client/{client_id}/advice", status_code=_SEE_OTHER)


# One personalised message at a time, for the same reason as the impulse above.
_writing = threading.Lock()

# Why the last "write me the message" click produced nothing, per client. Only
# failures land here: unlike an impulse, this one has no honest empty answer — the
# judgement of whether there is something to say was already made upstream.
_last_message_error: dict[int, str] = {}


def _target_for(
    session: Session, client: Client, angle: Angle, journalist: str, outlet: str
) -> pitch.PitchTarget | None:
    """Recover the full recipient from the name the form posted.

    The form carries a name and an outlet, which is all a link can carry. The
    prompt needs more than that — what this person has actually written — so the
    posted pair is matched back against the draft's own pitch list rather than
    trusted as the whole truth. An unmatched pair still yields a usable target:
    the consultant may know a desk the radar has never seen.
    """
    if not (journalist or outlet):
        return None
    wanted = ((journalist or "").casefold(), (outlet or "").casefold())
    for candidate in pitch.targets_for(session, client, angle):
        if ((candidate.journalist or "").casefold(), candidate.outlet.casefold()) == wanted:
            return candidate
    return pitch.PitchTarget(
        outlet=outlet,
        journalist=journalist or None,
        reason="",
        evidence=(),
        about_client=0,
    )


def _run_outreach(client_id: int, angle_id: int, journalist: str, outlet: str) -> None:
    """Write one personalised message on a worker thread; always release the lock."""
    try:
        with _run_guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                angle = session.get(Angle, angle_id)
                if client is None or angle is None or angle.client_id != client_id:
                    return
                _last_message_error.pop(client_id, None)
                target = _target_for(session, client, angle, journalist, outlet)
                message = outreach.draft(session, client, angle, target)
                # A second model reads it before a human does. Fault-isolated on
                # purpose: a missing key or an unreachable provider must not lose
                # the letter that was just written — it costs a model call, and
                # the page says plainly that it went unchecked.
                review = reviewed_by = None
                try:
                    review, reviewed_by = outreach.crosscheck(
                        session, client, angle, message, target
                    )
                except Exception as exc:  # noqa: BLE001
                    _last_message_error[client_id] = (
                        f"Die Nachricht steht, aber das Zweitmodell hat sie nicht "
                        f"gegengelesen: {exc}"
                    )
                    _log.warning("crosscheck skipped: %s", exc)
                outreach.store(
                    session, client, angle, message, target,
                    review=review, reviewed_by=reviewed_by or "",
                )
                _log.info(
                    "outreach written for %r → %s",
                    client.name,
                    target.outlet if target else "(kein Empfänger)",
                )
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        _last_message_error[client_id] = (
            f"Die Nachricht konnte nicht geschrieben werden: {exc}. "
            "Details stehen im Log."
        )
        _log.exception("outreach generation failed")
    finally:
        _writing.release()


@router.post("/client/{client_id}/impulse/{angle_id}/message")
def write_message(
    client_id: int,
    angle_id: int,
    journalist: str = Form(""),
    outlet: str = Form(""),
    target: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Turn this impulse into a message someone can actually send.

    This is what the "Empfehlungen" panel became. That panel described work —
    "react to the coverage" — and left the writing to the reader; the difference
    between it and the impulse beside it was never legible. One button on the
    position itself, and the mandate's own coverage does its work inside the text
    rather than in a second column.
    """
    client = session.get(Client, client_id)
    angle = session.get(Angle, angle_id)
    if client is None or angle is None or angle.client_id != client_id:
        raise HTTPException(status_code=404, detail="Impulse not found")
    # The name the browser sent wins. The index behind it is the fallback for a
    # reader with no JavaScript, resolved against the same list in the same order —
    # and deliberately not the primary path, because a list rebuilt a second later
    # could have shifted under it and addressed the letter to the wrong desk.
    if not (journalist or outlet) and target.strip().isdigit():
        options = pitch.targets_for(session, client, angle)
        index = int(target)
        if 0 <= index < len(options):
            journalist = options[index].journalist or ""
            outlet = options[index].outlet
    if _writing.acquire(blocking=False):
        threading.Thread(
            target=_run_outreach,
            args=(client_id, angle_id, journalist.strip(), outlet.strip()),
            daemon=True,
            name=f"newspulse-outreach-{angle_id}",
        ).start()
    return RedirectResponse(
        f"/client/{client_id}/advice#impulse-{angle_id}", status_code=_SEE_OTHER
    )


# --- The ledger: the human act at the end of the pipeline ------------------------


def _refuse_foreign_origin(request: Request) -> None:
    """Refuse a browser POST that arrived from another site's page.

    These two routes mint the audit record — "a human released this" — and the
    app has no CSRF token yet, so any open web page could auto-submit a form at
    the loopback bind and the browser would attach the cached Basic-auth
    credentials. A browser names the submitting page in ``Origin`` (or at least
    ``Referer``); when that name is not this app, the request was made *by* the
    consultant's browser but not *from* this tool, and writing the ledger off it
    would let any website forge the very claim the ledger exists to make.

    A request with neither header passes: that is not a browser (curl, a test
    client), and a non-browser carries no ambient credentials for a foreign page
    to ride on. An app-wide same-site token remains the real fix; this closes
    the hole where it costs the most first.
    """
    named = request.headers.get("origin") or request.headers.get("referer") or ""
    if not named:
        return
    # ``Origin: null`` (sandboxed frames, data: pages) has an empty netloc and
    # fails the comparison too, which is the right answer for it.
    if urlsplit(named).netloc != request.url.netloc:
        raise HTTPException(
            status_code=403,
            detail="Anfrage von einer fremden Seite — nicht eingetragen.",
        )


def _letter(session: Session, client_id: int, outreach_id: int) -> Outreach:
    """One letter of this mandate, or a 404.

    The mandate is checked as well as the id: a letter id is a small integer in a
    URL, and without this a guessed one would release another client's pitch.
    """
    row = session.get(Outreach, outreach_id)
    if row is None or row.client_id != client_id:
        raise HTTPException(status_code=404, detail="Outreach not found")
    return row


@router.post("/client/{client_id}/outreach/{outreach_id}/release")
def release_letter(
    request: Request,
    client_id: int,
    outreach_id: int,
    released_by: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Record that a person read this letter, released it and sent it.

    Nothing leaves the house here — this build has no mailbox — and the card says
    so in as many words. What the button produces is the record that was missing:
    the product's own L5 claim is that a human reads, edits and releases, and
    until now that act left no trace anywhere in the tool.

    ``released_by`` is a form field with no control behind it yet, because there
    are no user accounts; it defaults to "mensch", the same answer a hand-filled
    profile fact gives.
    """
    _refuse_foreign_origin(request)
    row = _letter(session, client_id, outreach_id)
    outreach.release(session, row, released_by=released_by)
    _log.info("outreach %d released by %r", row.id, row.released_by)
    return RedirectResponse(
        f"/client/{client_id}/advice#impulse-{row.angle_id}", status_code=_SEE_OTHER
    )


# Why the last push to Gmail failed, per client. Same shape and the same reason
# as the two dicts above: it describes one click, not the mandate, and going
# stale on restart is the right behaviour for a "what just happened" line. It
# matters more here than anywhere else in this file, because the reader has to
# know whether a letter went out — silence after pressing send is unbearable.
_last_gmail_error: dict[int, str] = {}


def _refuse_unsendable(session: Session, row: Outreach) -> Recipient:
    """Every reason this letter must not go out through Gmail, in one place.

    Five reasons, all of them a 400 and all of them checked before anything is
    composed: no mailbox, a mailbox without the send permission, a letter this
    tool already sent, a letter a person already released, and a recipient with
    no address in the contact book. The card renders none of them as an
    available action, so a request that hits one did not come from the card — it
    came from a tab opened before the letter's state changed, which is exactly
    the case the route has to catch rather than trust.

    The release check is the one that matters most. OUT-01's button says
    "Freigegeben und verschickt": a hand release *is* a send, performed by the
    consultant in their own client. Pushing that letter to Gmail as well would
    put a second copy of the same pitch in the journalist's inbox — the one
    irreversible harm the two-click confirmation exists to prevent.
    """
    link = gmail_link.connected()
    if link is None or not link.is_connected:
        raise HTTPException(status_code=400, detail="Kein Postfach verbunden.")
    if not link.may_send:
        raise HTTPException(status_code=400, detail=NO_SEND_PERMISSION)
    if row.sent_through_gmail:
        raise HTTPException(
            status_code=400, detail="Dieses Anschreiben ist bereits verschickt."
        )
    if row.released_at is not None:
        raise HTTPException(
            status_code=400,
            detail="Dieses Anschreiben wurde bereits freigegeben und verschickt.",
        )
    recipient = _recipient(session, row)
    if not recipient.is_reachable:
        raise HTTPException(status_code=400, detail=recipient.reason)
    return recipient


def _already_gone(row: Outreach) -> gmail_link.Sent | None:
    """The send this row never got to record, if Gmail says it happened anyway.

    Only ever asked on a retry — a first push has no thread yet — and it closes
    the two-step's one hole: a ``drafts.send`` whose answer was lost sent the
    letter *and* consumed the draft, so composing again would write a second
    letter and updating the old draft id would 404 forever. Reading the thread
    turns that unanswerable state back into a fact.
    """
    if not row.gmail_thread_id:
        return None
    return gmail_link.sent_in_thread(row.gmail_thread_id)


@router.post("/client/{client_id}/outreach/{outreach_id}/gmail-draft")
def send_through_gmail(
    request: Request,
    client_id: int,
    outreach_id: int,
    released_by: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Release this letter and send it from the connected mailbox.

    DEC-4 locked option C, and this is the sentence it changes: a text leaves the
    house because a button in RauteOS was pressed. The card asks twice before it
    gets here, and the confirmation names the address, because the first click in
    a full week happens quickly and this one cannot be taken back.

    In two Gmail calls, not one. The draft is composed first — repeating that is
    harmless, and a repeat updates the draft already there rather than leaving a
    second copy in the mailbox — and only then is it sent. A send that fails
    after Gmail accepted the draft therefore leaves a draft the consultant can
    see and this route can re-send, instead of an unanswerable question about
    whether a journalist has the letter.

    What must not happen twice is guarded twice: :func:`_refuse_unsendable`
    before anything is composed, and :func:`_already_gone` for the send whose
    answer never came back.
    """
    _refuse_foreign_origin(request)
    row = _letter(session, client_id, outreach_id)
    _last_gmail_error.pop(client_id, None)
    recipient = _refuse_unsendable(session, row)

    try:
        gone = _already_gone(row)
        if gone is not None:
            # It left after all. Recorded, not re-sent.
            outreach.record_sent(session, row, gone, released_by=released_by)
            _log.info("outreach %d was already sent (thread %s)", row.id, gone.thread_id)
            return RedirectResponse(
                f"/client/{client_id}/advice#impulse-{row.angle_id}",
                status_code=_SEE_OTHER,
            )
        draft = gmail_link.create_draft(
            recipient.email,
            row.subject,
            row.message,
            draft_id=row.gmail_draft_id,
            thread_id=row.gmail_thread_id,
        )
        outreach.record_draft(session, row, draft)
        sent = gmail_link.send(draft.draft_id)
    except gmail_link.GmailError as exc:
        # Never swallowed and never fatal to the page: the letter is untouched,
        # the draft id (if one was composed) is stored, and the card says what
        # happened so the reader knows the message did not go.
        _log.error("Gmail send failed for outreach %d: %s", row.id, exc)
        _last_gmail_error[client_id] = (
            f"Die Nachricht wurde nicht verschickt: {exc}"
        )
        return RedirectResponse(
            f"/client/{client_id}/advice#impulse-{row.angle_id}", status_code=_SEE_OTHER
        )

    outreach.record_sent(session, row, sent, draft_id=draft.draft_id, released_by=released_by)
    # The thread, never the text: what this line has to prove later is which
    # conversation the letter belongs to.
    _log.info(
        "outreach %d sent through Gmail (thread %s)", row.id, row.gmail_thread_id
    )
    return RedirectResponse(
        f"/client/{client_id}/advice#impulse-{row.angle_id}", status_code=_SEE_OTHER
    )


@router.post("/client/{client_id}/outreach/{outreach_id}/outcome")
def record_letter_outcome(
    request: Request,
    client_id: int,
    outreach_id: int,
    state: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Record what came back on a letter that went out.

    A 400 rather than a redirect when the letter was never released or the state
    is not an outcome: this is a form the page only renders on a released letter,
    so a request that fails either test did not come from the card, and answering
    it with a cheerful redirect would hide that.
    """
    _refuse_foreign_origin(request)
    row = _letter(session, client_id, outreach_id)
    try:
        outreach.record_outcome(session, row, state, note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/client/{client_id}/advice#impulse-{row.angle_id}", status_code=_SEE_OTHER
    )
