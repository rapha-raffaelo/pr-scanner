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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import angles, job, outreach, pitch
from ...db import get_session
from ..runlock import guard as _run_guard
from .. import themework
from ...models import Angle, Article, Client, TopicHit
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_log = logging.getLogger(__name__)
_SEE_OTHER = 303


def _backing(
    messages: dict[int, list], targets: dict[int, list[pitch.PitchTarget]]
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

    drafts = angles.for_client(session, client_id)
    targets = {a.id: pitch.targets_for(session, client, a) for a in drafts}
    messages = outreach.by_angle(session, [a.id for a in drafts])
    return templates.TemplateResponse(
        request,
        "advice.html",
        {
            "client": client,
            "drafting": _drafting.locked(),
            "writing": _writing.locked(),
            # Why the last click came back empty, if it did. Shown instead of the
            # generic "the radar has collected nothing", which was wrong as often
            # as it was right.
            # The click's own answer if there was one this session, otherwise
            # what the last sweep recorded — which is the usual case, since the
            # sweep runs at 06:10 and the page is opened at nine.
            "impulse_refusal": _last_refusal.get(client_id) or client.impulse_note,
            "impulse_checked_at": client.impulse_checked_at,
            # The remedy for the commonest refusal, offered where the refusal is
            # read rather than on a settings screen the reader has never opened.
            "theme_work": themework.state.get(client_id),
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
            "message_error": _last_message_error.get(client_id, ""),
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
                    TopicHit.client_id == client_id
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
        },
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
