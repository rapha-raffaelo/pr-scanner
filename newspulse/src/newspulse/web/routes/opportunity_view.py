"""Das Dossier zur Gelegenheit: the page, and the one route behind it.

One ``GET`` and nothing else. Everything a consultant can *do* from here already
has an endpoint — "Text schreiben" is
:func:`newspulse.web.routes.assets_view.write_from_opportunity`, "Verwerfen" is
:func:`newspulse.web.routes.today.dismiss_opportunity` — and the dossier posts to
those rather than growing its own. That is not tidiness: two endpoints opening
an occasion would be two places for the ``ux_angles_newsjack`` race to be
settled differently.

The route is a read all the way down. It asks :mod:`newspulse.opportunity` for
the page's pieces, hands them to the template, and returns. No ``session.add``,
no commit, no model call, no fetch — which is what makes it a page somebody
opens four times on a closing morning instead of one that charges for looking.

Who gets a 404, and why each one is a 404 rather than something softer:

* an id that is not this mandate's — a URL from another workspace, or a guess;
* a ``duenn`` or ``keins`` row — those are the audit record of a *refusal*.
  There is no opportunity to write a dossier about, and rendering one would
  present a rejection as a case;
* a benchmark — :func:`newspulse.web.mandates.mandate_or_404`'s rule, applied
  here for the same reason it applies everywhere: nobody reports to a yardstick.

An expired or waved-off opportunity is deliberately *not* a 404. The window
closing is what the window is for, and the question afterwards is always what
was known at the time. So the page keeps rendering, loses its buttons, and
carries the line saying when and on what it ended.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ... import opportunity as dossier
from ...models import Client, NewsjackOpportunity, Standing
from ..app import get_db, templates
from ..mandates import mandate_or_404
from .today import _fetch_last_run, _local_tz

router = APIRouter()


@dataclass(frozen=True, slots=True)
class Tab:
    """One entry of the strip on the page.

    A tab exists here only when it has somewhere to go. The mock draws eight;
    two of them — Performance and Historie — have nothing behind them in this
    tool, and a tab that leads nowhere is worse than a missing one, because it
    is a promise the reader spends a click discovering was empty.
    """

    label: str
    href: str
    #: True for the page the reader is on, which is the only tab without a link.
    here: bool = False


def page_tabs(client_id: int, opportunity_id: int) -> list[Tab]:
    """The strip: this page, then the mandate pages the mock's tabs land on.

    Every href is a route this app serves — the test walks them and refuses a
    404. Performance and Historie are left out on purpose: there is no
    per-opportunity measurement to put behind the first, and the second is the
    trail at the bottom of this very page.
    """
    return [
        Tab(
            label="Situation",
            href=f"/client/{client_id}/gelegenheit/{opportunity_id}",
            here=True,
        ),
        Tab(label="Positionierung", href=f"/client/{client_id}/profil"),
        Tab(label="Medien", href=f"/client/{client_id}/map"),
        Tab(label="Assets", href=f"/client/{client_id}/advice"),
        Tab(label="Quality", href=f"/client/{client_id}/guide"),
        Tab(label="GEO & AI Visibility", href=f"/client/{client_id}/ki"),
    ]


def _opportunity_or_404(
    session: Session, client: Client, opportunity_id: int
) -> NewsjackOpportunity:
    """This mandate's ``belegt`` opportunity, or 404.

    The standing gate is the same one ``write_from_opportunity`` keeps, and for
    the same reason: ``duenn`` and ``keins`` are stored refusals. They are worth
    keeping — they answer "warum schlägt das Werkzeug hier nichts vor" and they
    stop the next scan paying for the verdict again — and they are not cases.
    """
    row = session.get(NewsjackOpportunity, opportunity_id)
    if (
        row is None
        or row.client_id != client.id
        or row.standing is not Standing.BELEGT
    ):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return row


def page_context(
    session: Session,
    client: Client,
    row: NewsjackOpportunity,
    *,
    now: dt.datetime,
) -> dict:
    """Everything the template renders, gathered in one place.

    Split out from the route so the assembly can be exercised without a request,
    and so the "this page writes nothing" property is checkable against one
    function rather than against a rendered page.

    The order below is the order the reads depend on each other in: the occasion
    decides the status and the texts, the texts and the letters decide the next
    step, and the story's bylines decide which recipients wrote it.
    """
    occasion = dossier.occasion(session, row)
    story = dossier.sources(session, row)
    stored_texts = dossier.texts(session, occasion)
    stored_letters = dossier.letters(session, occasion)
    score = dossier.urgency(row, now=now)
    state = dossier.status(row, occasion, now=now)
    bylines = {source.author.casefold() for source in story if source.author}
    return {
        "client": client,
        "opp": row,
        "tabs": page_tabs(client.id, row.id),
        "occasion": occasion,
        "status": state,
        # The four segments as (state, word) pairs off the enum. The template
        # renders what it is given rather than re-typing the four names as
        # string literals, which is a set of values it cannot be kept in step
        # with — a renamed member would leave every segment silently unmarked.
        "states": dossier.STATUS_LABELS,
        "concluded": dossier.is_concluded(state),
        "score": score,
        "provenance": dossier.provenance(row, score, len(story)),
        "sources": story,
        "audiences": dossier.audiences(session, client),
        "outlook": dossier.outlook(row),
        "texts": stored_texts,
        # ``stored_letters`` deliberately does not reach the template. The
        # letters are on the page through ``steps`` and ``trail``; handing the
        # rendering layer the live ``Outreach`` rows as well would put a
        # recipient's address one attribute away on a page whose rule is that a
        # derived address is never shown.
        "steps": dossier.steps(occasion, stored_texts, stored_letters),
        "recipients": dossier.recipients(
            session, client, occasion, bylines, now=now
        ),
        "trail": dossier.trail(
            session, row, story, occasion, stored_texts, stored_letters
        ),
        # The one sentence the model wrote about this mandate's standing. Data,
        # so it stays in the language it was written in.
        "standing_reason": row.reason.strip(),
        # What the shared header renders on every page: the reader's day and the
        # last sweep. Two reads, and neither of them writes.
        "last_run": _fetch_last_run(session),
        "header_date": now.astimezone(_local_tz()).date(),
    }


@router.get(
    "/client/{client_id}/gelegenheit/{opportunity_id}", response_class=HTMLResponse
)
def opportunity_dossier(
    request: Request,
    client_id: int,
    opportunity_id: int,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The dossier for one opportunity. Reads; writes nothing; calls no model."""
    client = mandate_or_404(session, client_id)
    row = _opportunity_or_404(session, client, opportunity_id)
    return templates.TemplateResponse(
        request,
        "opportunity.html",
        page_context(session, client, row, now=dt.datetime.now(dt.UTC)),
    )


__all__ = ["Tab", "page_context", "page_tabs", "router"]
