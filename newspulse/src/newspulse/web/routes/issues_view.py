"""The issue register and its heatmap, and the buttons DEC-3 locked.

DEC-3 chose option A: the tool proposes, a person opens. The offer sits at the
top of the register with its two buttons and names what the repetition consists
of; nothing anywhere changes until somebody presses one of them — accepting
opens the row with its founding signals attached, dismissing costs one click
and the same repetition stops being offered.

The page itself is DEC-6 option A: the list first, the heatmap beside it. The
list is what is worked with — every row with its age, its last movement and its
signal count, because those three are what "something is growing" is made of.
The heatmap plots the *graded* issues; one missing either value stands in a
named column beside the field and never at its origin, because "not yet set" at
the origin would read as "harmless" — a claim nobody made.

Every write here goes through :mod:`newspulse.issues`, which owns the
disciplines (a reasoned attach, a reasoned close, values set by a person). The
POST endpoints are POST-and-redirect like the crisis buttons, and a stale id
costs nothing rather than the page.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import crisis, issues, stakeholders
from ... import profile as profiles
from ...models import (
    ISSUE_SCALE_MAX,
    ISSUE_SCALE_MIN,
    Analysis,
    Article,
    Client,
    Issue,
    IssueStatus,
)
from ..app import get_db, templates
from ..mandates import mandate_or_404
from ..redirects import local_target
from .profile import pop_stakeholder_note
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_log = logging.getLogger(__name__)

_SEE_OTHER = 303

# Why the last click produced what it produced, per mandate. In memory and not
# a schema change on purpose, the same posture as the crisis page's note: it
# describes one click, and going stale on a restart is correct.
_last_note: dict[int, str] = {}


def _who(request: Request) -> str:
    """The signed-in person, or the token that says a person pressed the button.

    The register's whole point is that values and openings carry the person who
    set them; where sign-in is not configured the tool still knows a human
    submitted the form, so it writes the ``"mensch"`` token rather than
    inventing a name nobody typed.
    """
    return str(request.scope.get("user_email") or crisis.DECLARED_BY_DEFAULT)


# --- What the template renders ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssueView:
    """One register row with the two numbers a stored row cannot say alone."""

    issue: Issue
    #: Full local days since the matter began — the age on the row.
    age_days: int
    signal_count: int
    suggestion: issues.Suggestion


@dataclass(frozen=True, slots=True)
class HeatCell:
    """One cell of the field: its coordinates and the issues standing on it."""

    probability: int
    impact: int
    rows: tuple[Issue, ...]


@dataclass(frozen=True, slots=True)
class Heatmap:
    """The field, highest impact first, plus the named column beside it.

    ``ungraded`` is the load-bearing part: an issue missing either value is not
    plotted low, it is named as unplotted — a dot at the origin would claim a
    grading nobody made.
    """

    #: One row per impact value, highest first; each row one cell per
    #: probability value, lowest first.
    rows: tuple[tuple[HeatCell, ...], ...]
    ungraded: tuple[Issue, ...]
    graded: int


def _heatmap(open_rows: list[Issue]) -> Heatmap:
    """Open issues over probability × impact, the ungraded ones set aside."""
    graded = [
        row for row in open_rows if row.probability is not None and row.impact is not None
    ]
    scale = range(ISSUE_SCALE_MIN, ISSUE_SCALE_MAX + 1)
    rows = tuple(
        tuple(
            HeatCell(
                probability=probability,
                impact=impact,
                rows=tuple(
                    row
                    for row in graded
                    if row.probability == probability and row.impact == impact
                ),
            )
            for probability in scale
        )
        for impact in reversed(scale)
    )
    return Heatmap(
        rows=rows,
        ungraded=tuple(
            row
            for row in open_rows
            if row.probability is None or row.impact is None
        ),
        graded=len(graded),
    )


def _views(open_rows: list[Issue], *, today: dt.date) -> list[IssueView]:
    return [
        IssueView(
            issue=row,
            age_days=max(0, (today - row.opened_at.astimezone(_local_tz()).date()).days),
            signal_count=len(row.signals),
            suggestion=issues.suggest(row),
        )
        for row in open_rows
    ]


@router.get("/client/{client_id}/issues", response_class=HTMLResponse)
def issues_page(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The register: the offer, the open rows, the field, and the record.

    Rendered for every mandate, empty register included — an empty register is
    a statement ("nothing is being carried"), not a missing page. A benchmark
    is a 404 like every other workspace page.
    """
    client = mandate_or_404(session, client_id)
    now = dt.datetime.now(dt.UTC)
    today = now.astimezone(_local_tz()).date()
    open_rows = issues.open_issues(session, client)
    past = [
        row
        for row in issues.history(session, client)
        if row.status is not IssueStatus.OFFEN
    ]
    # The standing map, and the selections hanging on every issue on the page —
    # past ones too: a closed or escalated issue stays readable with its
    # selection, the same rule its signals already keep.
    smap = stakeholders.card(session, client)
    selections = {
        row.id: stakeholders.selection_for(session, issue=row)
        for row in open_rows + past
    }
    return templates.TemplateResponse(
        request,
        "client_issues.html",
        {
            "header_date": today,
            "last_run": _fetch_last_run(session),
            "client": client,
            "offer": issues.propose(session, client, now=now),
            "rows": _views(open_rows, today=today),
            "heatmap": _heatmap(open_rows),
            "past": past,
            "scale_min": ISSUE_SCALE_MIN,
            "scale_max": ISSUE_SCALE_MAX,
            "note": _last_note.pop(client.id, ""),
            "smap": smap,
            "selections": selections,
            # Whether the profile has anything a proposal could rest on: the
            # empty map's sentence says what is missing, with the link there.
            "has_profile": bool(
                profiles.as_prompt_lines(profiles.stored(session, client.id))
            ),
            "stakeholder_note": pop_stakeholder_note(client.id),
            # Compared against, never printed: the page says "Vorschlag" /
            # "Empfehlung" where the column says "modell".
            "by_model": stakeholders.PROPOSED_BY_MODEL,
            "recommended": {
                issue_id: stakeholders.order_is_recommendation(rows)
                for issue_id, rows in selections.items()
            },
        },
    )


# --- DEC-3's two buttons ----------------------------------------------------------


@router.post("/issues/accept")
def accept_proposal(
    request: Request,
    client_id: int = Form(...),
    article_id: int = Form(...),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Turn the standing offer into an issue.

    The guards mirror the crisis declaration's: a stale id, a mandate off the
    roster and a proposal that dissolved while the tab sat open are all no-ops
    — :func:`newspulse.issues.accept` re-derives the repetition and answers
    ``None`` for a click that no longer has one to accept, which is also what
    makes a double click one issue rather than two.
    """
    client = session.get(Client, client_id)
    article = session.get(Article, article_id)
    if (
        client is not None
        and article is not None
        and client.active
        and not client.is_competitor
    ):
        opened = issues.accept(session, client, article, by=_who(request))
        if opened is None:
            _last_note[client.id] = (
                "Der Vorschlag stand nicht mehr: es wurde kein Issue eröffnet."
            )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/dismiss")
def dismiss_proposal(
    request: Request,
    client_id: int = Form(...),
    article_id: int = Form(...),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """The other button: wave the offer off, and it stops being offered.

    The analyzed-for-this-mandate guard is the same one the crisis dismissal
    carries, for the same reason: a dismissal pre-silences the article's whole
    story, so a mis-aimed or forged pair must not be able to suppress a future
    legitimate proposal before it was ever shown.
    """
    client = session.get(Client, client_id)
    article = session.get(Article, article_id)
    analyzed = (
        session.execute(
            select(Analysis.id)
            .where(Analysis.client_id == client_id, Analysis.article_id == article_id)
            .limit(1)
        ).first()
        is not None
    )
    if (
        client is not None
        and article is not None
        and analyzed
        and client.active
        and not client.is_competitor
    ):
        issues.dismiss(session, client, article, by=_who(request))
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


# --- The row's own buttons --------------------------------------------------------


def _issue_for(session: Session, issue_id: int) -> Issue | None:
    """The issue, or ``None`` for a stale id — never a 500 on a button."""
    return session.get(Issue, issue_id)


@router.post("/issues/{issue_id}/grade")
def grade_issue(
    request: Request,
    issue_id: int,
    probability: int | None = Form(None),
    impact: int | None = Form(None),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """A person sets Wahrscheinlichkeit and/or Wirkung; the row records who.

    An out-of-scale value is refused without a trace of a write: the form only
    offers 1-5, so anything else was submitted around it, and the honest answer
    is the unchanged row rather than a clamped number under the person's name.
    """
    standing = _issue_for(session, issue_id)
    if standing is not None:
        try:
            issues.grade(
                session,
                standing,
                by=_who(request),
                probability=probability,
                impact=impact,
            )
        except ValueError:
            _log.info("out-of-scale grade for issue %d refused", issue_id)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/edit")
def edit_issue(
    issue_id: int,
    description: str = Form(""),
    early_indicators: str = Form(""),
    owner: str = Form(""),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """The three free-text fields a person maintains on the row."""
    standing = _issue_for(session, issue_id)
    if standing is not None:
        issues.update_details(
            session,
            standing,
            description=description,
            early_indicators=early_indicators,
            owner=owner,
        )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/close")
def close_issue(
    request: Request,
    issue_id: int,
    reason: str = Form(""),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """End an issue. The reason is required, and required in the module.

    An empty reason returns to the page without closing anything — the field is
    ``required`` in the form, so an empty one means the form was submitted
    around the browser, and the page says why the row is still open rather
    than answering with a 500. An escalated issue refuses the same way: the UI
    shows no close form on one, so the request was submitted around it too.
    """
    standing = _issue_for(session, issue_id)
    if standing is not None:
        if reason.strip():
            try:
                issues.close(session, standing, reason=reason, by=_who(request))
            except ValueError as exc:
                _last_note[standing.client_id] = str(exc)
        else:
            _last_note[standing.client_id] = (
                "Das Issue wurde nicht geschlossen: es fehlt die Begründung."
            )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/escalate")
def escalate_issue(
    request: Request,
    issue_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Declare the crisis this issue became; land on the crisis page.

    The crisis takes the issue's signals and its opening as the beginning of
    its chronology — the handover lives in :func:`newspulse.issues.escalate`
    and its read side in :func:`newspulse.crisis.prehistory`. An issue that
    cannot escalate (closed, or without an article among its signals) says so
    on the register instead of raising.
    """
    standing = _issue_for(session, issue_id)
    if standing is None:
        return RedirectResponse("/", status_code=_SEE_OTHER)
    try:
        issues.escalate(session, standing, by=_who(request))
    except ValueError as exc:
        _last_note[standing.client_id] = str(exc)
        return RedirectResponse(
            f"/client/{standing.client_id}/issues", status_code=_SEE_OTHER
        )
    return RedirectResponse(
        f"/client/{standing.client_id}/krise", status_code=_SEE_OTHER
    )


# --- The stakeholder selection at an issue (RIS-03) --------------------------------


@router.post("/issues/{issue_id}/stakeholder/auswahl")
def select_stakeholders(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Build the issue's selection from the standing map, reasons included.

    Idempotent by construction — an issue that already carries a selection
    keeps it, order and all. An empty map selects nothing and says so: the
    selection is *from* the card, so the card comes first.
    """
    standing = _issue_for(session, issue_id)
    if standing is not None:
        try:
            selected = stakeholders.select_for(session, issue=standing)
        except Exception as exc:  # noqa: BLE001 — a button must answer, not 500
            _log.exception("stakeholder selection for issue %d failed", issue_id)
            _last_note[standing.client_id] = (
                f"Die Auswahl ist fehlgeschlagen: {exc}"
            )
        else:
            if not selected:
                _last_note[standing.client_id] = (
                    "Keine Auswahl entstanden: ohne Karte oder ohne begründbar "
                    "betroffene Gruppe wird nichts gespeichert."
                )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/stakeholder/reihenfolge")
def reorder_stakeholders(
    request: Request,
    issue_id: int,
    sid: list[int] = Form(default_factory=list),
    pos: list[int] = Form(default_factory=list),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """A person sorts the selection, and the person's order is what is kept.

    The form posts one (row id, position) pair per line; the handler sorts by
    the numbers and stores 1..n under the person's name — from then on the
    order is no Empfehlung any more. A form naming the wrong rows (a second
    tab rebuilt the selection underneath it) changes nothing and says so.
    """
    standing = _issue_for(session, issue_id)
    if standing is not None:
        if len(sid) != len(pos):
            _last_note[standing.client_id] = (
                "Die Reihenfolge wurde nicht gespeichert: das Formular war "
                "unvollständig."
            )
        else:
            ordered = [row_id for _p, row_id in sorted(zip(pos, sid, strict=True))]
            try:
                stakeholders.reorder(
                    session, issue=standing, ordered_ids=ordered, by=_who(request)
                )
            except ValueError:
                _last_note[standing.client_id] = (
                    "Die Reihenfolge wurde nicht gespeichert: sie nennt nicht "
                    "genau die Zeilen der Auswahl."
                )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)
